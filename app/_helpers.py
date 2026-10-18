"""Helpers shared across router modules (no routes, no `app` reference)."""

from __future__ import annotations

import logging
import math
import secrets
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from . import runtime
from .backends import phones, registry
from .pipedream import CATALOG, mid_call_apps

logger = logging.getLogger(__name__)


class Refusal(HTTPException):
    """A refusal a client can tell apart without parsing its sentence. It is
    answered as `{"detail": <sentence>, "reason": <code>}`: `detail` stays the
    plain string the console shows, and `reason` is a stable code beside it."""

    def __init__(self, status_code: int, detail: str, reason: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.reason = reason


class _ToolShape(BaseModel):
    """A custom tool must at least have a string name — the builder indexes
    tools by name in _normalize_config / the mapper. Extra fields pass through."""

    model_config = ConfigDict(extra="allow")
    name: str


def _check_needs_integrations(value: Any) -> None:
    """`needs_integrations` is a request, not an attachment.

    Validated so a typo surfaces as a 422 instead of a console badge naming an
    app nobody can connect. It grants nothing: the mapper never forwards it, and
    only `integrations` puts a tool in front of a model.
    """
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("The apps an agent needs must be a list of app names.")
    unknown = [v for v in value if v not in CATALOG]
    if unknown:
        known = ", ".join(a.name for a in CATALOG.values())
        raise ValueError(
            f"{unknown[0]} is not an app this build can connect. Choose one of: {known}."
        )


def _check_custom_tools_do_not_shadow(config: dict[str, Any]) -> None:
    """Refuse a custom tool that does a Connected app's job.

    A generated handler holds no third-party credential, so one written for work
    an integration already does cannot succeed — and it competes with the real
    tool for the model's attention at call time, usually winning on name. The
    builder is told to use `needs_integrations` instead; this is what happens
    when it does it anyway.
    """
    for tool in config.get("custom_tools") or []:
        if not isinstance(tool, dict):
            continue
        text = f"{tool.get('name', '')} {tool.get('description', '')}".lower()
        for app in CATALOG.values():
            if app.name.lower() not in text:
                continue
            raise ValueError(
                f"The tool {tool.get('name')!r} says it does {app.name}'s work, but a "
                f"generated tool has no {app.name} credentials and cannot. Connect "
                f"{app.name} on the Integrations page and attach it to this agent "
                f"instead."
            )


def _check_integrations(value: Any) -> None:
    """Validate `config.integrations`, in sentences a person can act on.

    Deliberately hand-written rather than a pydantic shape. The field accepts a
    bare slug or an object, and expressing that as `list[str | Model]` made every
    mistake report twice — once for the branch that was never going to match —
    inside a message naming private classes and linking to pydantic's docs. What
    a Workspace needs to read is which app is wrong and what to do about it.
    """
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError("Connected apps must be a list.")

    for position, entry in enumerate(value, start=1):
        # A bare slug is the legacy shape and is left unchecked: validating it
        # now would refuse an edit to an agent that has been working, over a
        # field the edit did not touch.
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"Connected app #{position} is not an app.")

        slug = entry.get("app")
        app = CATALOG.get(slug) if isinstance(slug, str) else None
        if app is None:
            known = ", ".join(a.name for a in CATALOG.values())
            raise ValueError(
                f"{slug or f'Connected app #{position}'} is not an app this build "
                f"can connect. Choose one of: {known}."
            )

        raw = entry.get("call_ended")
        if not entry.get("mid_call") and raw is None:
            raise ValueError(
                f"{app.name} is attached but set to do nothing. Turn on "
                f'"available during calls", pick something for when a call ends, '
                f"or remove it."
            )
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{app.name}: the end-of-call setting is malformed.")

        action = next((a for a in app.actions if a.id == raw.get("action")), None)
        if action is None:
            offered = ", ".join(a.label for a in app.actions)
            raise ValueError(
                f"{app.name} cannot do that when a call ends. "
                + (f"Choose one of: {offered}." if offered else "It offers nothing to run.")
            )
        # Unlike the rest, this one is not a typo a Workspace can see: the
        # Console fills it from the catalog, so an empty one means the form sent
        # something it should not have.
        if not str(raw.get("summary_arg") or "").strip():
            raise ValueError(
                f'{app.name}: "{action.label}" has nowhere to put the call summary.'
            )
        args = raw.get("args") or {}
        missing = [
            i.label
            for i in action.inputs
            if not str((args or {}).get(i.name) or "").strip()
        ]
        if missing:
            raise ValueError(
                f'"{action.label}" needs a {missing[0]} before it can run.'
                if len(missing) == 1
                else f'"{action.label}" still needs: {", ".join(missing)}.'
            )


class _AgentConfigShape(BaseModel):
    """Validates only the structural invariants the BUILDER's own code relies on
    (tool names, object-vs-scalar shapes). Everything else passes through to
    TurnCall, which is the authority on the full agent schema — so this stays
    permissive (extra='allow') and never duplicates that schema."""

    model_config = ConfigDict(extra="allow")
    name: str | None = None
    system_prompt: str | None = None
    first_message: str | None = None
    server_url: str | None = None
    llm: dict[str, Any] | None = None
    tts: dict[str, Any] | None = None
    stt: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    custom_tools: list[_ToolShape] | None = None
    integrations: list[Any] | None = None


def _validate_agent_config(config: dict[str, Any]) -> None:
    """Reject a structurally-broken config with a clean 422 before it reaches
    the fragile helpers (_normalize_config, the mapper), which would otherwise
    raise a raw 500 deep in the stack. Does NOT reshape — the original dict is
    what gets forwarded to TurnCall."""
    try:
        _AgentConfigShape.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_readable(exc)) from exc
    try:
        _check_integrations(config.get("integrations"))
        _check_needs_integrations(config.get("needs_integrations"))
        _check_custom_tools_do_not_shadow(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _readable(exc: ValidationError) -> str:
    """A pydantic ValidationError as one line a person can act on.

    The default rendering names private model classes, prints the offending
    value back, and links to pydantic's documentation — three things a Workspace
    cannot use and should not have to read.
    """
    seen: dict[str, None] = {}
    for err in exc.errors():
        # Drop pydantic's internal path segments (union tags, `function-after[…]`)
        # and keep the field names a reader would recognise from the form.
        path = ".".join(
            str(part)
            for part in err["loc"]
            if not (isinstance(part, str) and ("[" in part or part in {"str", "dict"}))
        )
        seen[f"{path}: {err['msg']}" if path else err["msg"]] = None
    return "; ".join(seen) or "The configuration is not valid."


async def _require_agents_number(
    backend: dict[str, Any], number_id: str, workspace_id: str
) -> None:
    """Refuse a number that cannot dial as this agent: only one bound in the
    agent's project can, and for an agent with mid-call apps only one with
    caller-info routing. A webhook number lives in a shared project the agent is not part
    of, so TurnCall would refuse it anyway, with a less useful message."""
    number = await phones.get_number(runtime.get_pool(), number_id, workspace_id)
    if (
        not number
        or number.get("agent_id") != backend["agent_id"]
        or number.get("project_id") != backend["project_id"]
    ):
        raise HTTPException(422, "Choose one of this agent's own phone numbers to call from.")
    # A mid-call app's tools reach a call only through call-init (a Pipedream
    # token lives an hour), and an outbound call runs call-init only from a
    # number with caller-info routing (turncall#277). Any other number would
    # call without them (#80).
    apps = mid_call_apps((backend.get("config") or {}).get("integrations"))
    if apps and number.get("routing_type") != "agent_call_init":
        raise HTTPException(
            422,
            f"This agent uses {', '.join(apps)} during calls, and only a number set to "
            f"\"Agent + caller info\" gives an outbound call those tools. Switch "
            f"{number.get('e164')} to it, or call from a number that uses it.",
        )


# TurnCall's bounds on a conversation's `{{variables}}` (turncall#281/#282),
# checked here too so a test that crosses them is told which, rather than a
# proxied 4xx whose upstream body is never echoed to the browser.
MAX_VARIABLES = 50
MAX_VARIABLES_CHARS = 4_096


def check_variables(variables: dict[str, Any]) -> dict[str, str]:
    """A test's variables as strings, or ValueError naming what is wrong."""
    if not all(isinstance(v, str | int | float | bool) for v in variables.values()):
        raise ValueError("variables must be strings, numbers or booleans")
    as_text = {str(k): str(v) for k, v in variables.items()}
    if len(as_text) > MAX_VARIABLES:
        raise ValueError(f"at most {MAX_VARIABLES} variables, got {len(as_text)}")
    size = sum(len(k) + len(v) for k, v in as_text.items())
    if size > MAX_VARIABLES_CHARS:
        raise ValueError(
            f"variables total {size} characters, names included; "
            f"the most is {MAX_VARIABLES_CHARS}"
        )
    return as_text


def _turncall_http_error(exc: httpx.HTTPStatusError) -> HTTPException:
    """Translate a TurnCall HTTPStatusError into an HTTPException carrying its
    message, so every proxy endpoint surfaces TurnCall failures identically.
    A limit's 429 keeps its wait as Retry-After (#74, #76)."""
    headers = None
    try:
        body = exc.response.json()
        retry_ms = (body.get("details") or {}).get("retry_after_ms")
        if exc.response.status_code == 429 and isinstance(retry_ms, int | float):
            headers = {"Retry-After": str(math.ceil(retry_ms / 1000))}
        detail = body.get("error") or exc.response.text
        # A schema refusal says only "Validation error" up top; the reason —
        # which field, what is wrong — is in details.errors.
        reasons = [
            f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg')}"
            for e in ((body.get("details") or {}).get("errors") or [])
        ]
        if reasons:
            detail = f"{detail}: " + "; ".join(reasons)
    except Exception:
        detail = exc.response.text
    return HTTPException(status_code=exc.response.status_code, detail=detail, headers=headers)


def _normalize_config(
    config: dict[str, Any], port: int | None = None
) -> dict[str, Any]:
    """Return a NEW config with routing fields visible AND concrete for the
    console's JSON editor: empty fields are filled with the URL actually in
    effect (the generated backend's, once a port exists). Externality is decided
    by where a URL resolves, not by whether the field is set (ADR-0010). Pure —
    never mutates the caller's config."""
    backend_base = registry.backend_url(port) if port else None
    server_url = config.get("server_url") or backend_base
    base = (server_url or "").rstrip("/")
    out = {**config, "server_url": server_url}
    if config.get("custom_tools") is not None:
        out["custom_tools"] = [
            tool
            if tool.get("server_url")
            else {
                **tool,
                "server_url": f"{base}/tools/{tool['name']}" if base else None,
            }
            for tool in config["custom_tools"]
        ]
    # Only the active pipeline's services apply — drop the inert block so the
    # console JSON shows just what's in effect (TurnCall re-defaults either way).
    unused = ("stt", "llm", "tts") if config.get("pipeline_mode") == "s2s" else ("s2s",)
    return {k: v for k, v in out.items() if k not in unused}


def browser_path(agent_id: str) -> str:
    """Where the console reads an agent's backend: through this API, which
    reaches it at backend_url — never the backend's own port, which a browser
    cannot reach once the backend runs on Kubernetes (ADR-0025)."""
    return f"/agents/{agent_id}/backend"


def _backend_view(agent_id: str, port: int, status: str) -> dict[str, Any]:
    return {"port": port, "browser_url": browser_path(agent_id), "status": status}


async def _backend_or_404(
    agent_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """The agent's backend, scoped to its Workspace (#31). An agent in another
    Workspace reads as 404 — the caller can't tell it apart from a missing one."""
    b = await registry.get_backend(runtime.get_pool(), agent_id, workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    return b


KNOWLEDGE_KB_NAME = "builder-knowledge"


def _is_builder_kb(kb: dict[str, Any]) -> bool:
    """One of the agent's document knowledge bases (ADR-0024): the original
    single one, or one made per document since."""
    name = kb.get("name") or ""
    return name == KNOWLEDGE_KB_NAME or name.startswith(f"{KNOWLEDGE_KB_NAME}-")


async def _agent_kbs(api_key: str) -> list[dict[str, Any]]:
    """The agent's document knowledge bases. Its project holds only its own,
    so listing them is the lookup — no registry column (ADR-0009)."""
    kbs = await runtime.get_client().list_knowledge_bases(api_key)
    return [kb for kb in kbs if _is_builder_kb(kb)]


async def _upload_document(
    agent_id: str,
    api_key: str,
    filename: str,
    content: bytes,
    content_type: str,
    *,
    mode: str = "prompt",
    top_k: int | None = None,
    tool_description: str | None = None,
) -> dict[str, Any]:
    """A document in a knowledge base of its own, linked in `mode` (ADR-0024):
    TurnCall sets how an agent reads knowledge per link, so a mode per
    document needs a knowledge base per document. One that fails to upload
    takes its empty knowledge base with it."""
    client = runtime.get_client()
    name = f"{KNOWLEDGE_KB_NAME}-{secrets.token_hex(4)}"
    kb = await client.create_knowledge_base(name, api_key)
    try:
        await client.link_knowledge_base(
            agent_id, kb["id"], api_key, mode, top_k=top_k, tool_description=tool_description
        )
        return await client.upload_document(kb["id"], filename, content, content_type, api_key)
    except Exception:
        await _drop_kb(agent_id, kb["id"], api_key)
        raise


async def _drop_kb(agent_id: str, kb_id: str, api_key: str) -> None:
    """Unlink and delete a document's knowledge base, best-effort: TurnCall
    refuses to delete one an agent still links."""
    client = runtime.get_client()
    try:
        await client.unlink_knowledge_base(agent_id, kb_id, api_key)
        await client.delete_knowledge_base(kb_id, api_key)
    except Exception:  # noqa: BLE001
        logger.warning(
            "knowledge base %s left behind for agent %s", kb_id, agent_id, exc_info=True
        )


def ranged_audio(audio: bytes, request: Request, media_type: str) -> Response:
    """Audio bytes as a response that honors Range requests (206).

    Safari refuses to play <audio> from endpoints that only ever answer 200
    with the full body."""
    total = len(audio)
    range_header = request.headers.get("range", "")
    if range_header.startswith("bytes="):
        start_s, _, end_s = range_header[len("bytes=") :].partition("-")
        try:
            start = int(start_s) if start_s else 0
            end = min(int(end_s), total - 1) if end_s else total - 1
        except ValueError:
            start, end = 0, total - 1
        if start > end or start >= total:
            return Response(
                status_code=416, headers={"Content-Range": f"bytes */{total}"}
            )
        return Response(
            content=audio[start : end + 1],
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
            },
        )
    return Response(
        content=audio, media_type=media_type, headers={"Accept-Ranges": "bytes"}
    )
