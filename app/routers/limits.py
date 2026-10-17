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
from ..audit import Audit, before_unknown
from ..backends import registry
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
    if b.get("limits") != project["limits"]:
        await registry.set_limits(runtime.get_pool(), agent_id, project["limits"])
    return {"success": True, "data": project["limits"]}


@router.put("/agents/{agent_id}/limits")
async def set_limits(
    agent_id: str, body: Limits, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    # What it replaces is the builder's copy (registry `limits`), the project's
    # by construction: the builder holds its only admin key. No copy (never
    # read or set here) is an unknown "before", never one claimed to be null.
    previous = b.get("limits")
    change = audit.begin(
        ctx, "agent", agent_id, "limits_set", landed=False,
        resource_name=(b.get("config") or {}).get("name"), agent_ids=[agent_id],
    )
    try:
        project = await runtime.get_client().set_project_limits(
            b["project_id"], body.model_dump(), b["api_key"]
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    change.landed()
    after = {"limits": project.get("limits")}
    if previous is None:
        change.changes = before_unknown(after)
    else:
        change.before, change.after = {"limits": previous}, after
    await registry.set_limits(runtime.get_pool(), agent_id, project.get("limits"))
    await change.record()
    return {"success": True, "data": project["limits"]}
