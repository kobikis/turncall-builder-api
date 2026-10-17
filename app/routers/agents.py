"""Agent console endpoints: list, get, update, start, delete, webrtc-connect proxy."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import agent_update, compliance_store, db, runtime
from .._helpers import (
    _backend_view,
    _normalize_config,
    _turncall_http_error,
    _validate_agent_config,
    browser_path,
    check_variables,
)
from ..audit import Audit
from ..backends import generator, phones, registry
from ..deps import AuthContext, require_editor, require_member
from ..tasks import spawn

logger = logging.getLogger("turncall_builder")
router = APIRouter()


class AgentUpdate(BaseModel):
    config: dict[str, Any]


def _live_status(backend: dict[str, Any], running_names: set[str] | None) -> str:
    """Registry status, corrected by what docker actually runs: a backend the
    registry believes up but whose container is gone shows as 'stopped'."""
    status = backend["status"]
    if running_names is None or status not in ("running", "degraded"):
        return status
    prefix = f"turncall-agent-{backend['slug']}-"
    return status if any(n.startswith(prefix) for n in running_names) else "stopped"


@router.get("/agents")
async def list_agents(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """List this Workspace's agents from the registry (ADR-0006), with live status."""
    rows = await registry.list_backends(runtime.get_pool(), ctx.workspace_id)
    names = await generator.running_container_names()
    agents = [
        {
            "agent_id": r["agent_id"],
            "name": (r.get("config") or {}).get("name") or r["slug"],
            "port": r["port"],
            "status": _live_status(r, names),
            "browser_url": browser_path(r["agent_id"]),
        }
        for r in rows
    ]
    return {"success": True, "data": {"agents": agents}}


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await registry.get_backend(runtime.get_pool(), agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    # Live fetch is best-effort — a stale key (TurnCall reset) shouldn't 500 the
    # detail view; the stored config is still editable.
    try:
        live = await runtime.get_client().get_agent(agent_id, b["api_key"])
    except Exception:
        live = None
    names = await generator.running_container_names()
    return {
        "success": True,
        "data": {
            "agent_id": agent_id,
            # Normalized so optional routing fields show as null (editable).
            "config": _normalize_config(b["config"], b["port"]) if b.get("config") else None,
            "turncall_agent": live,  # live TurnCall agent (null if key is stale)
            "backend": _backend_view(agent_id, b["port"], _live_status(b, names)),
            "tool_statuses": b.get("tool_statuses"),  # {name: generated|stub|external}
            # Shown by the console when tools route externally, so the user's
            # server can verify X-TurnCall-Signature (trusted local console).
            "tool_signing_secret": b.get("webhook_secret"),
        },
    }


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: str, body: AgentUpdate, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Overwrite the agent config; regenerate the backend iff custom_tools changed."""
    pool = runtime.get_pool()
    b = await registry.get_backend(pool, agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")

    _validate_agent_config(body.config)  # guard before _normalize_config / mapper
    change = begin_config_change(audit, ctx, b, body.config)
    try:
        result = await agent_update.apply_update(
            pool, runtime.get_client(), b, body.config, on_landed=change.landed
        )
    except httpx.HTTPStatusError as exc:
        # TurnCall refused the config (e.g. a 422 naming the field): pass its
        # status and reason through rather than a bare 500.
        raise _turncall_http_error(exc) from exc
    await change.record()
    return {"success": True, "data": {"agent_id": agent_id, **result}}


def begin_config_change(
    audit: Any, ctx: AuthContext, backend: dict[str, Any], config: dict[str, Any]
) -> Any:
    """The audit entry for applying a config, begun before apply_update: both
    sides normalized the way apply_update compares them, so a legacy config's
    filled-in defaults never read as a change. It lands when TurnCall accepts
    the agent (a refusal records nothing; a failure after it, partial). Shared
    by the form edit, the chat edit's Apply and attaching Connected apps."""
    port = backend["port"]
    before = _normalize_config(backend["config"], port) if backend.get("config") else None
    after = _normalize_config(config, port)
    return audit.begin(
        ctx, "agent", backend["agent_id"], "updated", landed=False,
        resource_name=after.get("name"), agent_ids=[backend["agent_id"]],
        before=before, after=after,
    )


@router.post("/agents/{agent_id}/start")
async def start_agent(
    agent_id: str, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Start (or restart) the agent's backend container in the background."""
    pool = runtime.get_pool()
    b = await registry.get_backend(pool, agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    await registry.set_status(pool, agent_id, "generating")
    spawn(
        _run_restart(pool, agent_id, b["service_dir"], b["port"], b.get("webhook_secret"))
    )
    await audit.record(
        ctx, "agent", agent_id, "restarted",
        resource_name=(b.get("config") or {}).get("name"), agent_ids=[agent_id], changes={},
    )
    return {"success": True, "data": _backend_view(agent_id, b["port"], "generating")}


async def _run_restart(
    pool: Any, agent_id: str, service_dir: str, port: int, secret: str | None
) -> None:
    """Background worker: recreate the container and record the outcome."""
    try:
        ok, _ = await generator.restart(service_dir, port)
        status = "degraded" if ok and not secret else "running" if ok else "failed"
    except Exception:  # noqa: BLE001
        logger.exception("backend restart failed for %s", agent_id)
        status = "failed"
    await registry.set_status(pool, agent_id, status)


@router.get("/agents/{agent_id}/backend/{feed}")
async def backend_feed(
    agent_id: str, feed: Literal["events", "automations"],
    call_id: str | None = None,
    ctx: AuthContext = Depends(require_member),
) -> JSONResponse:
    """The backend's own read endpoints, relayed for the console (browser_path).
    The backend serves them unauthenticated; here they take a member's login.

    `call_id` keeps one call's events — the console's live feed is per call.
    Filtered here, so every backend already generated serves it unchanged."""
    b = await registry.get_backend(runtime.get_pool(), agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{registry.backend_url(b['port'])}/{feed}")
        body = r.json()
        if call_id and feed == "events" and r.is_success:
            events = body["data"]["events"]
            body["data"]["events"] = [e for e in events if e.get("call_id") == call_id]
        return JSONResponse(body, status_code=r.status_code)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"backend unreachable: {exc}") from exc


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Teardown: container down, TurnCall agent archived, registry row hidden.
    The generated repo stays on disk — it's the user's code. Refuses while phone
    numbers still route to the agent."""
    pool = runtime.get_pool()
    b = await registry.get_backend(pool, agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    bound = [
        n
        for n in await phones.list_numbers(pool, ctx.workspace_id)
        if n.get("agent_id") == agent_id and n["routing_type"] != "none"
    ]
    if bound:
        raise HTTPException(
            status_code=409,
            detail=f"{len(bound)} phone number(s) still route to this agent — unbind them first",
        )
    config = b.get("config") or None
    change = audit.begin(
        ctx, "agent", agent_id, "deleted", landed=False,
        resource_name=(config or {}).get("name"), agent_ids=[agent_id],
        before=config, after=None,
    )
    await generator.teardown(b["service_dir"], b["port"])
    change.landed()  # its container is down
    # Each agent has its own dedicated project (1:1), so delete the whole
    # project (ADR-0011) — one call removes the agent + key + KB and leaves no
    # orphaned project. Best-effort: the registry teardown proceeds regardless.
    try:
        await runtime.get_client().delete_project(b["project_id"], b["api_key"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("turncall project delete failed for %s: %s", agent_id, exc)
    # The registry row is hidden and the Agent's compliance override and
    # projection record go with it, together: they describe its project.
    async with db.transaction(pool) as conn:
        await registry.mark_deleted(conn, agent_id)
        await compliance_store.forget_agent(conn, ctx.workspace_id, agent_id)
    await change.record()
    return {"success": True, "data": {"deleted": agent_id}}


# Fields the browser must not control on a proxied offer: agent_id is fixed to the
# (workspace-scoped, validated) path param, and server_url/secret would let the
# caller point TurnCall's leg at an arbitrary target instead of this agent.
_CLIENT_CONTROLLED = ("agent_id", "server_url", "server_url_secret")

# The browser's transport sends the offer from a prebuilt Request whose body it
# replaces with the SDP alone, so a test call's `{{variables}}` ride in a
# header: URL-encoded JSON, since a header is ASCII and a value may not be.
VARIABLES_HEADER = "X-Call-Variables"


def _header_variables(request: Request) -> dict[str, str] | None:
    """The test call's variables, or None when the header is absent. One that
    is not a flat, bounded JSON object is the caller's to fix: 400."""
    raw = request.headers.get(VARIABLES_HEADER)
    if raw is None:
        return None
    try:
        variables = json.loads(unquote(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{VARIABLES_HEADER} must be URL-encoded JSON"
        ) from exc
    if not isinstance(variables, dict):
        raise HTTPException(
            status_code=400, detail=f"{VARIABLES_HEADER} must be a JSON object"
        )
    try:
        return check_variables(variables)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _pipeline_start_failure(response: httpx.Response) -> tuple[str, list[str]] | None:
    """TurnCall's reason when the agent's pipeline could not start, plus the
    provider keys it found missing; None for any other upstream error."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("code") != "pipeline_start_failed":
        return None
    reason = str(body.get("error") or "")
    if not reason:
        return None
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    missing = details.get("missing_keys")
    keys = [str(k) for k in missing] if isinstance(missing, list) else []
    return reason, keys


@router.post("/agents/{agent_id}/webrtc/connect")
async def agent_webrtc_connect(
    agent_id: str, request: Request, ctx: AuthContext = Depends(require_editor)
) -> JSONResponse:
    """WebRTC test-call signaling proxy (#35). The browser POSTs its SDP offer here
    instead of calling TurnCall directly; the builder forwards it with the agent's
    project key and returns the SDP answer. The TurnCall key never reaches the
    browser, and the leg is pinned to this agent (server_url/agent_id from the
    client are ignored). Editor+ only — a viewer is refused by the gate before any
    key is touched. Media still flows browser<->TurnCall directly; only signaling
    is proxied."""
    b = await registry.get_backend(runtime.get_pool(), agent_id, ctx.workspace_id)
    if not b:
        # Scoped lookup → an agent in another Workspace reads as 404 (no IDOR).
        raise HTTPException(status_code=404, detail="agent backend not found")
    try:
        offer = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="body must be valid JSON") from exc
    if not isinstance(offer, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    # Pin the leg to this agent. TurnCall reads agent_id/server_url from BOTH the
    # top-level body AND a nested request_data dict (request_data wins), so strip
    # the client's routing from both places and set agent_id in both — otherwise a
    # nested {"request_data": {"server_url": ...}} would retarget past a flat strip.
    body = {k: v for k, v in offer.items() if k not in _CLIENT_CONTROLLED}
    rd = offer.get("request_data")
    rd = {k: v for k, v in rd.items() if k not in _CLIENT_CONTROLLED} if isinstance(rd, dict) else {}
    # Routed through the backend's own /call-init when we hold its secret, so a
    # test call resolves the way a phone call does — and an agent with attached
    # apps gets the inline config carrying a freshly minted credential. Resolving
    # by agent_id loads the STORED config instead, whose mcp_servers deliberately
    # carry none (ADR-0018/0020): Connected apps were unreachable from the Test
    # tab, and silently, because the agent simply had no tool to call.
    #
    # Still server-chosen, never the client's: _CLIENT_CONTROLLED is stripped from
    # both places first, so this pins the leg exactly as agent_id did.
    call_init_secret = b.get("call_init_secret")
    if call_init_secret:
        routing = {
            # The full endpoint, not the base: TurnCall POSTs to server_url as
            # given and does not append a path. Same convention the phone bind
            # uses (routers/phones.py) — a bare base URL lands on `POST /` and
            # 404s, which surfaces as "server_url did not return a valid agent".
            "server_url": f"{registry.backend_url(b['port'])}/call-init",
            "server_url_secret": call_init_secret,
        }
    else:
        # An agent older than the secret column, or one whose .env has not been
        # rewritten since. Resolving by id still works; it just cannot carry
        # credentials, which is what it did before this existed.
        routing = {"agent_id": agent_id}
    rd.update(routing)
    variables = _header_variables(request)
    if variables:
        rd["variables"] = variables
    body["request_data"] = rd
    body.update(routing)
    try:
        answer = await runtime.get_client().webrtc_connect(body, b["api_key"])
    except httpx.HTTPStatusError as exc:
        # Log TurnCall's detail server-side; never echo its raw body to the browser
        # (it can carry internal codes/ids). A malformed SDP (4xx, not an auth
        # failure) is the caller's to fix → 422. A stale agent key (401/403) or a
        # TurnCall 5xx is an upstream problem → 502, not the caller's session, so it
        # isn't mistaken for the builder login expiring.
        status = exc.response.status_code
        logger.warning(
            "turncall webrtc connect failed agent=%s status=%s body=%s",
            agent_id, status, exc.response.text[:500],
        )
        if status == 429:
            # A limit (#76): its message is written for the caller and names
            # the limit, so it is the one upstream body passed through.
            raise _turncall_http_error(exc) from exc
        if 400 <= status < 500 and status not in (401, 403):
            raise HTTPException(
                status_code=422, detail="offer rejected by upstream — check the SDP"
            ) from exc
        failure = _pipeline_start_failure(exc.response)
        if failure:
            # The one upstream message written for the person placing the call:
            # without it the test call just timed out, saying nothing. The
            # missing keys ride along as a list so the console can name them.
            reason, missing_keys = failure
            return JSONResponse(
                status_code=502,
                content={
                    "detail": f"{reason} — check the agent's provider keys in Integrations.",
                    "missing_keys": missing_keys,
                },
            )
        raise HTTPException(status_code=502, detail="TurnCall upstream error") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="TurnCall is unreachable — retry in a moment"
        ) from exc
    # Raw SDP answer passthrough (not enveloped) — the browser feeds it straight to
    # its WebRTC transport, so this is a drop-in swap for calling TurnCall directly.
    return JSONResponse(answer)
