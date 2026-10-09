"""An agent's campaigns (#75): outbound calls to a list (turncall#229-#234).

One agent per campaign. TurnCall's weighted campaigns split targets between
agents of one project, and every builder agent is its own project, so they are
not offered. TurnCall validates the rest (targets, calling window, retry
policy) and its refusals name the field, so they pass through as is.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Literal, TypeVar

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from .. import runtime
from .._helpers import _require_agents_number, _backend_or_404, _turncall_http_error
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()
T = TypeVar("T")
CampaignAction = Literal["pause", "resume", "cancel"]


class CampaignBody(BaseModel):
    # forbid: `agents` (weights) or `agent_id` cannot ride along — the agent
    # is the one in the path.
    model_config = ConfigDict(extra="forbid")

    name: str
    from_number_id: str
    timezone: str | None = None
    calling_window: dict[str, Any] | None = None
    retry_policy: dict[str, Any] | None = None
    targets: list[dict[str, Any]]


async def _from_turncall(coro: Awaitable[T]) -> T:
    """Await a TurnCall call; its refusal becomes ours, its message intact."""
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc


@router.post("/agents/{agent_id}/campaigns", status_code=201)
async def create_campaign(
    agent_id: str, body: CampaignBody, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    await _require_agents_number(b, body.from_number_id, ctx.workspace_id)
    payload = {**body.model_dump(exclude_none=True), "agent_id": b["agent_id"]}
    data = await _from_turncall(runtime.get_client().create_campaign(payload, b["api_key"]))
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/campaigns")
async def list_campaigns(
    agent_id: str, page: int = 1, limit: int = 20, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    return await _from_turncall(runtime.get_client().list_campaigns(b["api_key"], page=page, limit=limit))


@router.get("/agents/{agent_id}/campaigns/{campaign_id}")
async def get_campaign(
    agent_id: str, campaign_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    data = await _from_turncall(runtime.get_client().get_campaign(campaign_id, b["api_key"]))
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/campaigns/{campaign_id}/targets")
async def list_campaign_targets(
    agent_id: str,
    campaign_id: str,
    status: str | None = None,
    page: int = 1,
    limit: int = 100,
    ctx: AuthContext = Depends(require_member),
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    return await _from_turncall(
        runtime.get_client().list_campaign_targets(
            campaign_id, b["api_key"], status=status, page=page, limit=limit
        )
    )


@router.post("/agents/{agent_id}/campaigns/{campaign_id}/{action}")
async def control_campaign(
    agent_id: str,
    campaign_id: str,
    action: CampaignAction,
    ctx: AuthContext = Depends(require_editor),
) -> dict[str, Any]:
    """Pause stops new dials (live calls stay up); cancel is final."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    data = await _from_turncall(runtime.get_client().control_campaign(campaign_id, action, b["api_key"]))
    return {"success": True, "data": data}
