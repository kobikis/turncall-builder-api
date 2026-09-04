"""Agent console endpoints: list, get, update, start, delete, webrtc-connect proxy."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import agent_update, runtime
from .._helpers import _backend_view, _normalize_config, _validate_agent_config
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
            "browser_url": f"http://localhost:{r['port']}",
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
            "backend": _backend_view(b["port"], _live_status(b, names)),
            "tool_statuses": b.get("tool_statuses"),  # {name: generated|stub|external}
            # Shown by the console when tools route externally, so the user's
            # server can verify X-TurnCall-Signature (trusted local console).
            "tool_signing_secret": b.get("webhook_secret"),
        },
    }


@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: str, body: AgentUpdate, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Overwrite the agent config; regenerate the backend iff custom_tools changed."""
    pool = runtime.get_pool()
    b = await registry.get_backend(pool, agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")

    _validate_agent_config(body.config)  # guard before _normalize_config / mapper
    result = await agent_update.apply_update(pool, runtime.get_client(), b, body.config)
    return {"success": True, "data": {"agent_id": agent_id, **result}}


@router.post("/agents/{agent_id}/start")
async def start_agent(
    agent_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Start (or restart) the agent's backend container in the background."""
    pool = runtime.get_pool()
    b = await registry.get_backend(pool, agent_id, ctx.workspace_id)
    if not b:
        raise HTTPException(status_code=404, detail="agent not found")
    await registry.set_status(pool, agent_id, "generating")
    spawn(_run_restart(pool, agent_id, b["service_dir"], b.get("webhook_secret")))
    return {"success": True, "data": _backend_view(b["port"], "generating")}


async def _run_restart(pool: Any, agent_id: str, service_dir: str, secret: str | None) -> None:
    """Background worker: recreate the container and record the outcome."""
    try:
        ok, _ = await generator.restart(service_dir)
        status = "degraded" if ok and not secret else "running" if ok else "failed"
    except Exception:  # noqa: BLE001
        logger.exception("backend restart failed for %s", agent_id)
        status = "failed"
    await registry.set_status(pool, agent_id, status)


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str, ctx: AuthContext = Depends(require_editor)
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
    await generator.teardown(b["service_dir"])
    # Each agent has its own dedicated project (1:1), so delete the whole
    # project (ADR-0011) — one call removes the agent + key + KB and leaves no
    # orphaned project. Best-effort: the registry teardown proceeds regardless.
    try:
        await runtime.get_client().delete_project(b["project_id"], b["api_key"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("turncall project delete failed for %s: %s", agent_id, exc)
    await registry.mark_deleted(pool, agent_id)
    return {"success": True, "data": {"deleted": agent_id}}


# Fields the browser must not control on a proxied offer: agent_id is fixed to the
# (workspace-scoped, validated) path param, and server_url/secret would let the
# caller point TurnCall's leg at an arbitrary target instead of this agent.
_CLIENT_CONTROLLED = ("agent_id", "server_url", "server_url_secret")


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
    rd["agent_id"] = agent_id
    body["request_data"] = rd
    body["agent_id"] = agent_id
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
        if 400 <= status < 500 and status not in (401, 403):
            raise HTTPException(
                status_code=422, detail="offer rejected by upstream — check the SDP"
            ) from exc
        raise HTTPException(status_code=502, detail="TurnCall upstream error") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="TurnCall is unreachable — retry in a moment"
        ) from exc
    # Raw SDP answer passthrough (not enveloped) — the browser feeds it straight to
    # its WebRTC transport, so this is a drop-in swap for calling TurnCall directly.
    return JSONResponse(answer)
