"""Outbound calls and the do-not-call list, per agent (#74).

The do-not-call list is TurnCall's suppression list (turncall#227) for the
agent's project: every outbound dial checks it, and the `do_not_call` tool adds
to it during calls. Refusals are TurnCall's, passed through rather than retried.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import runtime
from .._helpers import _require_agents_number, _backend_or_404, _turncall_http_error
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


class OutboundCall(BaseModel):
    from_number_id: str
    to_number: str


class DoNotCallEntry(BaseModel):
    number: str


def _outbound_error(exc: httpx.HTTPStatusError, to_number: str) -> HTTPException:
    """TurnCall's refusal, in the console's words where it has its own."""
    try:
        body = exc.response.json()
    except ValueError:
        body = {}
    if body.get("code") == "number_suppressed":
        return HTTPException(409, f"{to_number} is on this agent's do-not-call list.")
    return _turncall_http_error(exc)


@router.post("/agents/{agent_id}/calls/outbound", status_code=201)
async def place_outbound_call(
    agent_id: str, body: OutboundCall, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Call `to_number` from one of the agent's own numbers. The call then
    shows in the agent's Calls, like any other."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    await _require_agents_number(b, body.from_number_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().place_outbound_call(
            agent_id=b["agent_id"],
            from_number_id=body.from_number_id,
            to_number=body.to_number,
            api_key=b["api_key"],
        )
    except httpx.HTTPStatusError as exc:
        raise _outbound_error(exc, body.to_number) from exc
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/do-not-call")
async def list_do_not_call(
    agent_id: str, page: int = 1, limit: int = 100, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        return await runtime.get_client().list_suppressed(b["api_key"], page=page, limit=limit)
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc


@router.post("/agents/{agent_id}/do-not-call", status_code=201)
async def add_do_not_call(
    agent_id: str, body: DoNotCallEntry, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        data = await runtime.get_client().suppress_number(body.number, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": data}


@router.delete("/agents/{agent_id}/do-not-call/{entry_id}")
async def remove_do_not_call(
    agent_id: str, entry_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        await runtime.get_client().unsuppress_number(entry_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": {"deleted": True}}
