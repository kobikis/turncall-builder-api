"""An agent's limits (#76): its TurnCall project's (turncall#206).

One project per agent, so a project's limits are the agent's. TurnCall's PUT
replaces all three, and a null one is the platform default, so the body is
always sent whole.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import runtime
from .._helpers import _backend_or_404, _turncall_http_error
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()


class Limits(BaseModel):
    """Null = the platform default. TurnCall validates the values (>= 1)."""

    live_calls: int | None = None
    outbound_calls_per_second: int | None = None
    messages_per_minute: int | None = None


@router.get("/agents/{agent_id}/limits")
async def get_limits(agent_id: str, ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        project = await runtime.get_client().get_project(b["project_id"], b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": project["limits"]}


@router.put("/agents/{agent_id}/limits")
async def set_limits(
    agent_id: str, body: Limits, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        project = await runtime.get_client().set_project_limits(
            b["project_id"], body.model_dump(), b["api_key"]
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": project["limits"]}
