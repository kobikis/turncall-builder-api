"""Per-agent takeaway (structured-output) console endpoints."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import runtime
from .._helpers import _backend_or_404, _turncall_http_error
from ..backends import registry
from ..deps import AuthContext, require_editor, require_member
from ..mapper import to_create_agent_request

router = APIRouter()


class TakeawayCreate(BaseModel):
    name: str
    schema_: dict[str, Any] = Field(alias="schema")
    description: str | None = None
    prompt: str | None = None
    model: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class TakeawayUpdate(BaseModel):
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    description: str | None = None
    prompt: str | None = None
    model: str | None = None

    model_config = ConfigDict(populate_by_name=True)


async def _set_agent_takeaways(b: dict[str, Any], takeaway_ids: list[str]) -> None:
    """Persist the attachment list on the agent (TurnCall + registry config)."""
    base = b.get("config") or {}
    analysis = {**base.get("analysis", {"enabled": True}), "takeaway_ids": takeaway_ids}
    config = {**base, "analysis": analysis}
    payload = to_create_agent_request(
        config,
        registry.backend_url(b["port"]),
        tools_secret=b.get("webhook_secret") or None,
    )
    await runtime.get_client().update_agent(b["agent_id"], payload, b["api_key"])
    await registry.update_config(runtime.get_pool(), b["agent_id"], config)


def _attached_ids(b: dict[str, Any]) -> list[str]:
    return list(
        ((b.get("config") or {}).get("analysis") or {}).get("takeaway_ids") or []
    )


@router.get("/agents/{agent_id}/takeaways")
async def list_agent_takeaways(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    rows = await runtime.get_client().list_takeaways(b["api_key"])
    return {"success": True, "data": {"takeaways": rows}}


@router.post("/agents/{agent_id}/takeaways")
async def create_agent_takeaway(
    agent_id: str, body: TakeawayCreate, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Create a takeaway in the agent's project and attach it (one agent per
    project, so attach-on-create is the only sensible console behavior)."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    try:
        row = await runtime.get_client().create_takeaway(
            {
                "name": body.name,
                "schema": body.schema_,
                "description": body.description,
                "prompt": body.prompt,
                "model": body.model,
            },
            b["api_key"],
        )
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    await _set_agent_takeaways(b, [*_attached_ids(b), row["id"]])
    return {"success": True, "data": row}


@router.put("/agents/{agent_id}/takeaways/{takeaway_id}")
async def update_agent_takeaway(
    agent_id: str,
    takeaway_id: str,
    body: TakeawayUpdate,
    ctx: AuthContext = Depends(require_editor),
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    payload = {
        k: v
        for k, v in {
            "schema": body.schema_,
            "description": body.description,
            "prompt": body.prompt,
            "model": body.model,
        }.items()
        if v is not None
    }
    try:
        row = await runtime.get_client().update_takeaway(takeaway_id, payload, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": row}


@router.delete("/agents/{agent_id}/takeaways/{takeaway_id}")
async def delete_agent_takeaway(
    agent_id: str, takeaway_id: str, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Detach from the agent first (TurnCall blocks deleting attached takeaways)."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    remaining = [t for t in _attached_ids(b) if t != takeaway_id]
    await _set_agent_takeaways(b, remaining)
    try:
        await runtime.get_client().delete_takeaway(takeaway_id, b["api_key"])
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc
    return {"success": True, "data": {"deleted": takeaway_id}}
