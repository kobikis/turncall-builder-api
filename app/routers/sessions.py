"""Builder session endpoints + the Create saga (project + agent + backend)."""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import (
    agent_update,
    builder,
    provider_catalog,
    runtime,
    session_docs,
    store,
    workspace_skills,
)
from .._helpers import (
    _agent_kb,
    _backend_view,
    _normalize_config,
    _validate_agent_config,
)
from ..backends import generator, registry, scaffold, toolgen
from ..deps import AuthContext, require_editor, require_member
from ..builder import ComposeResult, BuilderError, stream
from ..mapper import external_tool_names, to_create_agent_request
from ..settings import load_settings
from ..tasks import spawn

logger = logging.getLogger("turncall_builder")
router = APIRouter()

SUBSCRIBED_EVENTS = ["call.started", "call.ended", "call.initializing"]


class TurnRequest(BaseModel):
    message: str
    # Legacy: filenames the browser held but had not uploaded. Documents are now
    # staged on the Session (POST /sessions/{sid}/documents) and the server reads
    # the list itself; still accepted so an older console keeps working.
    doc_names: list[str] = []


class ConfigEdit(BaseModel):
    config: dict[str, Any]


class SessionCreate(BaseModel):
    # Seed a chat session from an existing agent to edit it; omit for a fresh build.
    agent_id: str | None = None
    # The Session's Builder model — both-or-neither; omit for the deployment default.
    builder_provider: str | None = None
    builder_model: str | None = None


def _validate_builder_choice(body: SessionCreate | None) -> tuple[str | None, str | None]:
    """Enforce both-or-neither and that the picked provider is keyed (Q2-Q4)."""
    if body is None or (body.builder_provider is None and body.builder_model is None):
        return None, None
    if body.builder_provider is None or body.builder_model is None:
        raise HTTPException(
            status_code=400,
            detail="builder_provider and builder_model come together or not at all",
        )
    available = builder.provider_availability()
    if body.builder_provider not in available:
        raise HTTPException(
            status_code=400,
            detail=f"unknown builder provider: {body.builder_provider}",
        )
    if not available[body.builder_provider]:
        raise HTTPException(
            status_code=400,
            detail=f"builder provider {body.builder_provider} has no API key configured",
        )
    return body.builder_provider, body.builder_model


async def _session_or_404(sid: str, workspace_id: str) -> dict[str, Any]:
    sess = await store.get_session(runtime.get_pool(), sid, workspace_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess


async def _agent_kb_doc_names(agent_id: str, workspace_id: str) -> list[str]:
    """Filenames in the agent's knowledge base, so the builder knows the agent
    already has that content. Best-effort — never blocks a chat turn."""
    from .knowledge import _agent_kb

    try:
        b = await registry.get_backend(runtime.get_pool(), agent_id, workspace_id)
        if not b:
            return []
        kb = await _agent_kb(b)
        if not kb:
            return []
        docs = await runtime.get_client().list_documents(kb["id"], b["api_key"])
        return [d["filename"] for d in docs if d.get("filename")]
    except Exception:
        return []


def _result_payload(r: ComposeResult) -> dict[str, Any]:
    config = _normalize_config(r.agent_config) if r.agent_config else r.agent_config
    return {
        "action": r.action,
        "question": r.question,
        "summary": r.summary,
        "agent_config": config,
    }


@router.get("/meta")
def meta() -> dict[str, Any]:
    """Console hints — e.g. the Twilio voice webhook URL to verify/paste (A)."""
    base = load_settings().turncall_public_url
    return {
        "success": True,
        "data": {
            "turncall_public_url": base,
            "twilio_voice_webhook": f"{base}/webhooks/twilio/voice/inbound" if base else "/webhooks/twilio/voice/inbound",
            "twilio_status_webhook": f"{base}/webhooks/twilio/status" if base else "/webhooks/twilio/status",
        },
    }


# What the console is told for each BuilderError kind. 503 means "this may work
# if you try again"; 502 means "an operator has to change something first", so
# the split matters more than the wording.
BUILDER_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    "credit": (
        502,
        "The builder's LLM provider reports no remaining credit. Top up the "
        "account behind the builder's API key — retrying will not help.",
    ),
    "auth": (
        502,
        "The builder's LLM provider rejected its API key. Set a valid "
        "ANTHROPIC_API_KEY (or OPENAI_API_KEY) for the builder API and restart it.",
    ),
    "rate_limit": (
        503,
        "The builder's LLM provider is rate-limiting requests — please retry in "
        "a moment.",
    ),
    "protocol": (
        502,
        "The builder model returned an unusable response. Please retry; if it "
        "persists, try a different builder model.",
    ),
    "timeout": (
        504,
        "The builder took too long on that turn — please retry. If it keeps "
        "happening, try a smaller attached document or a different builder model.",
    ),
    "upstream": (503, "The builder is temporarily unavailable — please retry."),
}

# The providers the builder supports (static). Models + voices are fetched live
# per provider — see /providers/llm/{p}/models and /providers/tts/{p}/voices.
_LLM_PROVIDERS = ["openai", "anthropic", "openrouter", "ollama", "custom_openai", "bedrock"]
_TTS_PROVIDERS = ["deepgram", "openai", "elevenlabs", "cartesia"]
_STT_PROVIDERS = ["deepgram", "openai", "elevenlabs", "cartesia"]
_S2S_PROVIDERS = ["openai", "openai_live", "google", "aws"]


@router.get("/providers")
def providers() -> dict[str, Any]:
    """STT/LLM/TTS/S2S provider options for the config form's provider dropdowns."""
    return {
        "success": True,
        "data": {"stt": _STT_PROVIDERS, "llm": _LLM_PROVIDERS, "tts": _TTS_PROVIDERS, "s2s": _S2S_PROVIDERS},
    }


@router.get("/providers/stt/{provider}/models")
async def stt_models(provider: str) -> dict[str, Any]:
    """Model ids for an STT provider (cached; Deepgram fetched live)."""
    return {"success": True, "data": {"models": await provider_catalog.stt_models(provider)}}


@router.get("/providers/s2s/{provider}/models")
async def s2s_models(provider: str) -> dict[str, Any]:
    """Model ids for an S2S/realtime provider (openai live, google fixed)."""
    return {"success": True, "data": {"models": await provider_catalog.s2s_models(provider)}}


@router.get("/providers/s2s/{provider}/voices")
async def s2s_voices(provider: str) -> dict[str, Any]:
    """Voices for an S2S provider (openai fixed set; google is free text)."""
    return {"success": True, "data": {"voices": await provider_catalog.s2s_voices(provider)}}


@router.get("/providers/llm/{provider}/models")
async def llm_models(provider: str) -> dict[str, Any]:
    """Model ids for an LLM provider, fetched live from its API (cached)."""
    return {"success": True, "data": {"models": await provider_catalog.llm_models(provider)}}


@router.get("/providers/tts/{provider}/voices")
async def tts_voices(provider: str) -> dict[str, Any]:
    """Voices for a TTS provider, fetched live from its API (cached)."""
    return {"success": True, "data": {"voices": await provider_catalog.tts_voices(provider)}}


@router.get("/providers/builder")
def builder_providers() -> dict[str, Any]:
    """Builder-model picker data: which providers are keyed, and the default.
    Models come from the existing /providers/llm/{p}/models catalog."""
    default_provider, default_model = builder.default_choice()
    return {
        "success": True,
        "data": {
            "providers": [
                {"name": name, "available": ok}
                for name, ok in builder.provider_availability().items()
            ],
            "default": {"provider": default_provider, "model": default_model},
        },
    }


@router.post("/sessions")
async def create_session(
    body: SessionCreate | None = None, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    pool = runtime.get_pool()
    ws = ctx.workspace_id
    provider, model = _validate_builder_choice(body)
    if body and body.agent_id:
        # Open an existing agent for chat editing: seed the session with its
        # current (trimmed) config so the builder edits rather than rebuilds.
        b = await registry.get_backend(pool, body.agent_id, ws)
        if not b:
            raise HTTPException(status_code=404, detail="agent not found")
        name = (b.get("config") or {}).get("name") or b["slug"]
        seed = [{"role": "assistant", "content": f"Editing '{name}'. What would you like to change?"}]
        # No explicit pick → the edit session inherits the Builder model that
        # created the agent (continuity), if that provider is still keyed.
        if provider is None:
            c_provider, c_model = await store.get_creation_builder_choice(
                pool, body.agent_id
            )
            if c_provider and c_model and builder.provider_availability().get(c_provider):
                provider, model = c_provider, c_model
        sid = await store.create_session(
            pool, workspace_id=ws, config=b.get("config"), history=seed, agent_id=body.agent_id,
            builder_provider=provider, builder_model=model,
        )
        return {
            "success": True,
            "data": {
                "session_id": sid,
                "agent_id": body.agent_id,
                "config": b.get("config"),
                "builder_provider": provider,
                "builder_model": model,
            },
        }
    sid = await store.create_session(
        pool, workspace_id=ws, builder_provider=provider, builder_model=model
    )
    return {"success": True, "data": {"session_id": sid}}


async def _turn_events(
    sid: str, sess: dict[str, Any], ctx: AuthContext, message: str, legacy_docs: list[str]
) -> AsyncIterator[dict[str, Any]]:
    """One turn as events: what it is doing, then what it decided.

    Shared by both message endpoints so the history is written in exactly one
    place — a turn that persists differently depending on how you asked for it
    is a bug waiting for a slow afternoon.
    """
    # New list — never mutate the loaded session's history in place (coding-style).
    history = [*sess["history"], {"role": "user", "content": message}]

    # Tell the builder which documents the agent has, so it builds capabilities
    # that use them instead of asking for pasted content.
    doc_names = [*legacy_docs, *session_docs.names(sid)]
    if sess["agent_id"]:
        doc_names.extend(await _agent_kb_doc_names(sess["agent_id"], ctx.workspace_id))
    doc_names = list(dict.fromkeys(doc_names))  # de-dupe, keep order

    result: ComposeResult | None = None
    # Ground on the session's current config (if any) so the builder edits it
    # instead of rebuilding — refine a draft, or change a generated agent.
    async for event in stream(
        history,
        provider=sess.get("builder_provider"),
        model=sess.get("builder_model"),
        current_config=sess["config"],
        knowledge_docs=doc_names or None,
        documents_dir=session_docs.text_dir(sid),
        # Rebuilt every turn, so an edited skill lands on the next message of a
        # conversation already in progress.
        workspace_skills_dir=await workspace_skills.materialize(
            runtime.get_pool(), ctx.workspace_id
        ),
    ):
        if isinstance(event, ComposeResult):
            result = event
        else:
            yield {"type": "progress", "text": event.text, "skill": event.skill}

    if result is None:
        raise BuilderError("the builder finished without a result", kind="protocol")

    if result.action in ("ask", "confirm"):
        # A confirm is still just something the builder said — it goes into the
        # history so the next turn can see it was already offered and accepted.
        history.append(
            {"role": "assistant", "content": result.question or result.summary or ""}
        )
    else:
        history.append(
            {"role": "assistant", "content": "Updated the agent configuration."}
        )
        await store.save_config(runtime.get_pool(), sid, result.agent_config or {})
    await store.save_history(runtime.get_pool(), sid, history)

    payload = _result_payload(result)
    # For a generated agent, a finalize is a *proposed* edit — confirm-first: the
    # console shows it and calls /apply to push it live (never auto-mutated here).
    if result.action == "finalize" and sess["agent_id"]:
        payload["pending_apply"] = True
    yield {"type": "result", **payload}


def _builder_failure(sid: str, exc: BuilderError) -> tuple[int, str]:
    """The vendor's raw message never reaches the browser (it carries request
    ids and account detail), but the *class* of failure does — telling someone
    to retry a request that can never succeed until they top up an account is
    worse than saying nothing."""
    kind = getattr(exc, "kind", "upstream")
    logger.warning("builder turn failed (session %s, kind=%s): %s", sid, kind, exc)
    return BUILDER_ERROR_RESPONSES.get(kind, BUILDER_ERROR_RESPONSES["upstream"])


@router.post("/sessions/{sid}/messages")
async def post_message(
    sid: str, body: TurnRequest, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """One turn, answered when it is finished. The streaming sibling is what the
    console uses; this stays for anything that just wants the answer."""
    sess = await _session_or_404(sid, ctx.workspace_id)
    try:
        async for event in _turn_events(sid, sess, ctx, body.message, body.doc_names):
            if event["type"] == "result":
                return {
                    "success": True,
                    "data": {k: v for k, v in event.items() if k != "type"},
                }
    except BuilderError as exc:
        status, detail = _builder_failure(sid, exc)
        raise HTTPException(status_code=status, detail=detail) from exc
    status, detail = BUILDER_ERROR_RESPONSES["protocol"]
    raise HTTPException(status_code=status, detail=detail)


@router.post("/sessions/{sid}/messages/stream")
async def post_message_stream(
    sid: str, body: TurnRequest, ctx: AuthContext = Depends(require_editor)
) -> StreamingResponse:
    """The same turn, narrated.

    The wait is not the model writing slowly — the compose result comes last,
    after the reading. So what streams is what it is doing: the documents it
    opens and the skills it loads, which is also the only way a skill's author
    learns their description worked.
    """
    sess = await _session_or_404(sid, ctx.workspace_id)

    async def events() -> AsyncIterator[str]:
        try:
            async for event in _turn_events(sid, sess, ctx, body.message, body.doc_names):
                yield f"data: {json.dumps(event)}\n\n"
        except BuilderError as exc:
            # The response has already begun, so the status code is spent. The
            # failure travels as an event or it does not travel at all.
            status, detail = _builder_failure(sid, exc)
            yield f"data: {json.dumps({'type': 'error', 'status': status, 'detail': detail})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # no-cache for proxies, X-Accel-Buffering for nginx — a buffered stream
        # is a slower version of not streaming at all.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{sid}/documents")
async def upload_session_document(
    sid: str, file: UploadFile, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Stage a document on the Session, before any Agent exists.

    The console used to hold these in the browser until Create. Server-side, the
    Builder is told what it actually has, and the forward into Agent Knowledge
    at Create becomes retryable.
    """
    await _session_or_404(sid, ctx.workspace_id)
    try:
        doc = session_docs.save(sid, file.filename or "document", await file.read())
    except ValueError as exc:
        # Size, type and filename are all rejected here — this is the boundary.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"success": True, "data": doc}


@router.get("/sessions/{sid}/documents")
async def list_session_documents(
    sid: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """What this Session has staged. Read-only, so a viewer may see it."""
    await _session_or_404(sid, ctx.workspace_id)
    return {"success": True, "data": {"documents": session_docs.list_documents(sid)}}


@router.delete("/sessions/{sid}/documents/{name}")
async def delete_session_document(
    sid: str, name: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    await _session_or_404(sid, ctx.workspace_id)
    if not session_docs.delete(sid, name):
        raise HTTPException(status_code=404, detail="document not found")
    return {"success": True, "data": {"deleted": name}}


@router.put("/sessions/{sid}/config")
async def edit_config(
    sid: str, body: ConfigEdit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    await _session_or_404(sid, ctx.workspace_id)
    _validate_agent_config(body.config)  # fail fast on a broken hand-edited config
    await store.save_config(runtime.get_pool(), sid, body.config)
    return {"success": True, "data": {"config": body.config}}


@router.post("/sessions/{sid}/apply")
async def apply_edit(
    sid: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Confirm-first push: apply the session's edited config to the already-
    generated agent (update TurnCall + regenerate the backend iff tools changed).
    Before generation there's nothing to apply — use /create instead."""
    sess = await _session_or_404(sid, ctx.workspace_id)
    if not sess["agent_id"]:
        raise HTTPException(status_code=400, detail="agent not created yet — use create")
    if not sess["config"]:
        raise HTTPException(status_code=400, detail="no config to apply")
    _validate_agent_config(sess["config"])
    pool = runtime.get_pool()
    b = await registry.get_backend(pool, sess["agent_id"], ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent backend not found")
    result = await agent_update.apply_update(pool, runtime.get_client(), b, sess["config"])
    return {"success": True, "data": {"agent_id": sess["agent_id"], **result}}


@router.post("/sessions/{sid}/create")
async def create_agent(
    sid: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Create the agent's own project + agent + generated backend (event-owning)."""
    sess = await _session_or_404(sid, ctx.workspace_id)
    pool = runtime.get_pool()

    if sess["agent_id"]:
        b = await registry.get_backend(pool, sess["agent_id"], ctx.workspace_id)
        # Documents left staged mean a previous forward failed part-way. This is
        # the retry, and it is only possible because the bytes are still here.
        knowledge = (
            await _forward_staged_documents(sid, sess["agent_id"], b["api_key"])
            if b
            else None
        )
        return {
            "success": True,
            "data": {
                "agent_id": sess["agent_id"],
                "created": False,
                "backend": _backend_view(b["port"], b["status"]) if b else None,
                "knowledge": knowledge,
                "turncall": {"project_id": b["project_id"], "api_key": b["api_key"]}
                if b
                else None,
            },
        }
    if not sess["config"]:
        raise HTTPException(status_code=400, detail="config not finalized")
    _validate_agent_config(sess["config"])  # guard before _normalize_config / mapper

    client = runtime.get_client()
    name = sess["config"].get("name", "agent")

    # Reserve the backend port up front so the agent's tools + webhook point at it.
    port = await registry.next_port(pool)
    tools_base_url = registry.backend_url(port)
    config = _normalize_config(sess["config"], port)

    # Dedicated project per agent so its webhook is scoped to just its events.
    # Everything after this point is a remote saga — on failure we roll back
    # what's cleanly reversible (the agent) and fail with a clean 502 rather than
    # a raw 500 mid-provision.
    try:
        project_id = await client.create_project(f"builder-{scaffold.slugify(name)}")
    except RuntimeError as exc:
        # Platform-key misconfig (turncall#102) — nothing was created, so no
        # rollback; surface the actionable message as a 502, not a bare 500.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    agent_id: str | None = None
    key = ""
    try:
        key = await client.create_api_key(project_id)

        # Register the webhook BEFORE the agent so the same secret signs its tool
        # calls from the first call (events + tools share one backend secret).
        # Best-effort: a webhook failure degrades (no secret), it doesn't abort.
        try:
            sub = await client.register_webhook(
                f"{tools_base_url}/events", SUBSCRIBED_EVENTS, key
            )
            secret = sub["secret"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook registration failed for project %s: %s", project_id, exc)
            secret = ""

        data = await client.create_agent(
            to_create_agent_request(config, tools_base_url, tools_secret=secret or None),
            key,
        )
        agent_id = data["id"]
        await store.set_agent_id(pool, sid, agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent provisioning failed for project %s", project_id)
        await _rollback_provisioning(client, project_id, agent_id, key)
        raise HTTPException(
            status_code=502,
            detail="Agent provisioning failed in TurnCall — rolled back; please retry.",
        ) from exc

    # Local + docker; best-effort, records status for the UI and never raises.
    backend = await _generate_backend(
        pool, agent_id, project_id, key, config, port, secret, ctx.workspace_id
    )
    knowledge = await _forward_staged_documents(sid, agent_id, key)
    return {
        "success": True,
        "data": {
            "agent_id": agent_id,
            "created": True,
            "backend": backend,
            "knowledge": knowledge,
            # For a WebRTC/test client: authenticate with this agent's project key.
            "turncall": {"project_id": project_id, "api_key": key},
        },
    }


async def _forward_staged_documents(
    sid: str, agent_id: str, api_key: str
) -> dict[str, list[str]]:
    """Move the Session's staged documents into the Agent's Knowledge base.

    Best-effort, like the browser upload it replaces: the Agent exists and is
    never rolled back over a document. What is new is that anything failing
    stays staged, so Create can be called again and actually retry it — the
    browser could only tell the user to go and do it by hand.
    """
    docs = session_docs.list_documents(sid)
    if not docs:
        return {"uploaded": [], "failed": []}

    try:
        kb = await _agent_kb(agent_id, api_key, create=True)
    except Exception:  # noqa: BLE001
        logger.exception("staged docs: no knowledge base for agent %s", agent_id)
        return {"uploaded": [], "failed": [d["name"] for d in docs]}
    if kb is None:  # create=True always returns one; belt and braces
        return {"uploaded": [], "failed": [d["name"] for d in docs]}

    client = runtime.get_client()
    uploaded: list[str] = []
    failed: list[str] = []
    for doc in docs:
        name = doc["name"]
        try:
            await client.upload_document(
                kb["id"],
                name,
                session_docs.read_raw(sid, name),
                mimetypes.guess_type(name)[0] or "application/octet-stream",
                api_key,
            )
            uploaded.append(name)
        except Exception:  # noqa: BLE001
            logger.exception("staged doc %s failed to upload for agent %s", name, agent_id)
            failed.append(name)

    if not failed:
        session_docs.discard(sid)
    return {"uploaded": uploaded, "failed": failed}


async def _rollback_provisioning(
    client: Any, project_id: str, agent_id: str | None, key: str
) -> None:
    """Compensate a failed create saga by soft-deleting the whole dedicated
    project (ADR-0011) — one call removes the project + its agent/key. Needs the
    project key: if key creation itself failed there's nothing to authenticate
    with, so the empty project is logged as an inert orphan. Best-effort: never
    masks the original error."""
    if not key:
        logger.warning(
            "orphaned TurnCall project %s after failed create (no key to delete it)",
            project_id,
        )
        return
    try:
        await client.delete_project(project_id, key)
    except Exception:  # noqa: BLE001
        logger.exception("rollback: delete_project failed for %s", project_id)


async def _generate_backend(
    pool: Any,
    agent_id: str,
    project_id: str,
    api_key: str,
    config: dict[str, Any],
    port: int,
    secret: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Record the backend row (status 'generating') and kick off the docker
    build in the background — the build takes tens of seconds, so returning
    immediately keeps Create snappy. The console polls /agents/{id} for the
    'generating' -> running/degraded/failed transition."""
    row = await registry.record_backend(
        pool,
        agent_id,
        project_id,
        api_key,
        config.get("name", "agent"),
        port,
        config,
        workspace_id,
    )
    await registry.set_webhook_secret(pool, agent_id, secret)
    spawn(
        _run_build(
            pool, agent_id, row["slug"], row["service_dir"], config, port, secret,
            row.get("builder_token") or "",
            row.get("call_init_secret") or "",
        )
    )
    return _backend_view(port, "generating")


async def _run_build(
    pool: Any,
    agent_id: str,
    slug: str,
    service_dir: str,
    config: dict[str, Any],
    port: int,
    secret: str,
    builder_token: str = "",
    call_init_secret: str = "",
) -> None:
    """Background worker: render the repo, docker-build it, record the outcome.
    Best-effort — a failure is recorded as 'failed' for the UI, never raised."""
    custom_tools = config.get("custom_tools") or []
    external = external_tool_names(config, registry.backend_url(port))
    internal_tools = [t for t in custom_tools if t["name"] not in external]
    try:
        bodies = await toolgen.generate_tool_bodies(internal_tools)
        tool_statuses = {
            t["name"]: (
                "external"
                if t["name"] in external
                else "generated"
                if t["name"] in bodies
                else "stub"
            )
            for t in custom_tools
        }
        files = scaffold.render(
            slug=slug,
            port=port,
            tools=internal_tools,
            tool_bodies=bodies,
            integrations=config.get("integrations"),
            agent_id=agent_id,
            webhook_secret=secret,
            call_init_secret=call_init_secret,
            builder_token=builder_token,
        )
        ok, _ = await generator.materialize_and_run(service_dir, files)
        # Degraded: container is up, but with no signing secret every verified
        # endpoint fails closed — the console badges it so it never looks idle.
        status = "degraded" if ok and not secret else "running" if ok else "failed"
        await registry.set_status(pool, agent_id, status)
        await registry.set_tool_statuses(pool, agent_id, tool_statuses)
    except Exception:  # noqa: BLE001
        logger.exception("backend generation failed")
        await registry.set_status(pool, agent_id, "failed")
