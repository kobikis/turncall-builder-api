"""Workspace compliance settings and each Agent's override (#103, ADR-0028).

Setting either is an admin act: the Workspace's is policy for every Agent, and an
Agent's override can weaken it. An Agent's effective setting is readable by any
member, since it says what happens to that Agent's calls. Every write is
projected at once and answers with each target's state; `POST
/compliance/apply` finishes a projection that failed partway, and `POST
/agents/{id}/compliance/apply` retries one Agent's alone.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from .. import compliance, compliance_store, runtime
from .._helpers import _backend_or_404, _readable
from ..audit import Audit
from ..deps import AuthContext, require_admin, require_member

router = APIRouter()


def _validate(model: type[BaseModel], body: dict[str, Any]) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_readable(exc)) from exc


@router.get("/compliance")
async def get_settings(ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    data = await compliance.report(runtime.get_pool(), runtime.get_client(), ctx.workspace_id, write=False)
    return {"success": True, "data": data}


@router.put("/compliance")
async def set_settings(
    audit: Audit,
    body: dict[str, Any] = Body(...),  # noqa: B008
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """Replace the Workspace's setting and project it into every Agent."""
    settings = _validate(compliance.WorkspaceSettings, body)
    pool = runtime.get_pool()
    before = _setting(await compliance_store.get_workspace(pool, ctx.workspace_id))
    after = {"redaction": settings.redaction.model_dump(), "retention": settings.retention.model_dump()}
    await compliance_store.save_workspace(pool, ctx.workspace_id, **after)
    # Saved: a projection that raises from here is recorded as partial.
    change = audit.begin(
        ctx, "compliance", "workspace", "updated" if before else "created",
        before=before, after=after,
    )
    data = await compliance.report(pool, runtime.get_client(), ctx.workspace_id, write=True)
    await change.record()
    return {"success": True, "data": data}


def _setting(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an audit entry compares: the setting, not its timestamp."""
    if row is None:
        return None
    return {"redaction": row.get("redaction"), "retention": row.get("retention")}


@router.post("/compliance/apply")
async def reapply(audit: Audit, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Retry every target not holding its setting; those that do are skipped."""
    data = await compliance.report(runtime.get_pool(), runtime.get_client(), ctx.workspace_id, write=True)
    await audit.record(ctx, "compliance", "workspace", "reapplied", changes={})
    return {"success": True, "data": data}


@router.get("/agents/{agent_id}/compliance")
async def get_agent_settings(
    agent_id: str, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    data = await compliance.agent_report(
        runtime.get_pool(), runtime.get_client(), ctx.workspace_id, b, write=False
    )
    return {"success": True, "data": data}


@router.put("/agents/{agent_id}/compliance")
async def set_agent_settings(
    agent_id: str,
    audit: Audit,
    body: dict[str, Any] = Body(...),  # noqa: B008
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    """Replace the Agent's override (null blocks follow the Workspace) and
    project what now applies into its project. Both null clears it: the
    Agent follows the Workspace entirely (`override_cleared`)."""
    override = compliance.override_document(_validate(compliance.AgentOverride, body))
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    pool = runtime.get_pool()
    before = _setting(await compliance_store.get_override(pool, ctx.workspace_id, agent_id))
    cleared = override["redaction"] is None and override["retention"] is None
    await compliance_store.save_override(pool, ctx.workspace_id, agent_id, **override)
    if cleared and before is None:
        # No override before or after: the projection below only finishes
        # whatever was owed already, as POST /compliance/apply would.
        audit.unchanged()
        change = None
    else:
        # Saved: a projection that raises from here is recorded as partial.
        change = audit.begin(
            ctx, "compliance", agent_id,
            "override_cleared" if cleared else "updated" if before else "created",
            resource_name=(b.get("config") or {}).get("name"), agent_ids=[agent_id],
            before=before, after=None if cleared else override,
        )
    data = await compliance.agent_report(pool, runtime.get_client(), ctx.workspace_id, b, write=True)
    if change is not None:
        await change.record()
    return {"success": True, "data": data}


@router.post("/agents/{agent_id}/compliance/apply")
async def reapply_agent(
    agent_id: str, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Retry this Agent's projection alone: written when its project does not
    hold what applies to it, skipped when it does. Every other target is left
    as it stands."""
    b = await _backend_or_404(agent_id, ctx.workspace_id)
    # Begun before the write: an entry only if TurnCall accepted something.
    change = audit.begin(
        ctx, "compliance", agent_id, "reapplied", landed=False, changes={},
        resource_name=(b.get("config") or {}).get("name"), agent_ids=[agent_id],
    )
    data = await compliance.agent_report(
        runtime.get_pool(), runtime.get_client(), ctx.workspace_id, b, write=True
    )
    await change.record()
    return {"success": True, "data": data}
