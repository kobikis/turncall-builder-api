"""A Workspace's own Pipedream project — the card above Connected apps.

Without one, the Workspace's Connected apps live in the deployment's project
(PIPEDREAM_* in the env), as they always did. With one, they live in its own:
its billing, its limits. Admin-only to change; any member sees which is in use.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from .. import db, pipedream, pipedream_store, runtime
from ..audit import Audit
from ..deps import AuthContext, require_admin, require_member
from ..integration_store import IntegrationError

router = APIRouter(prefix="/integrations/pipedream")


class ProjectBody(BaseModel):
    client_id: str
    client_secret: str
    project_id: str
    environment: str = "production"

    @field_validator("client_id", "client_secret", "project_id")
    @classmethod
    def _required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("project_id")
    @classmethod
    def _project(cls, v: str) -> str:
        if not v.startswith("proj_"):
            raise ValueError("a Pipedream project id looks like proj_…")
        return v

    @field_validator("environment")
    @classmethod
    def _env(cls, v: str) -> str:
        if v not in ("production", "development"):
            raise ValueError("environment is production or development")
        return v


@router.get("")
async def status(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    pool = runtime.get_pool()
    own = await pipedream_store.workspace_status(pool, ctx.workspace_id)
    return {
        "success": True,
        "data": {
            "connected": own is not None,
            **(own or {}),
            # Whether Connected apps work at all without a project of its own.
            "platform_project": await pipedream_store.has_deployment_credential(pool),
        },
    }


@router.put("")
async def connect(
    body: ProjectBody, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Checked by minting an access token with it; a refused pair is not saved."""
    cred = pipedream.Credential(
        client_id=body.client_id,
        client_secret=body.client_secret,
        project_id=body.project_id,
        environment=body.environment,
    )
    try:
        await pipedream.access_token(cred)
        await pipedream.list_accounts(cred, workspace_id=ctx.workspace_id)
    except pipedream.PipedreamError as exc:
        raise HTTPException(status_code=400, detail=f"{exc} It was not saved.") from exc
    pool = runtime.get_pool()
    try:
        async with db.transaction(pool) as conn:
            before = await pipedream_store.workspace_status(conn, ctx.workspace_id)
            await pipedream_store.save_workspace_credential(
                conn, ctx.workspace_id,
                client_id=body.client_id, client_secret=body.client_secret,
                project_id=body.project_id, environment=body.environment,
            )
            await audit.record(
                ctx, "pipedream_project", "workspace", "updated" if before else "created",
                resource_name=body.project_id,
                before=_audited(before),
                after={
                    "client_id": body.client_id, "client_secret": body.client_secret,
                    "project_id": body.project_id, "environment": body.environment,
                },
                db=conn,
            )
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return await status(ctx)


@router.delete("")
async def disconnect(audit: Audit, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Refused while apps are connected in this project: they would vanish from
    the Workspace (it falls back to the deployment's project, which does not
    hold them) while every agent kept them attached."""
    pool = runtime.get_pool()
    try:
        own = await pipedream_store.workspace_credential(pool, ctx.workspace_id)
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if own is None:
        raise HTTPException(status_code=404, detail="No Pipedream project is connected")
    try:
        accounts = await pipedream.list_accounts(own, workspace_id=ctx.workspace_id)
    except pipedream.PipedreamError:
        accounts = []  # unreadable project: nothing reachable to strand
    if accounts:
        names = ", ".join(a.get("name") or a["app"] for a in accounts)
        raise HTTPException(
            status_code=409,
            detail=f"{len(accounts)} app(s) are connected in this project ({names}) — "
            "disconnect them first",
        )
    async with db.transaction(pool) as conn:
        before = await pipedream_store.workspace_status(conn, ctx.workspace_id)
        await pipedream_store.delete_workspace_credential(conn, ctx.workspace_id)
        await audit.record(
            ctx, "pipedream_project", "workspace", "deleted",
            resource_name=own.project_id, before=_audited(before), after=None, db=conn,
        )
    return await status(ctx)


def _audited(status: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stored project as an entry compares it; the client secret is never
    read back, so it is held as present (and masked by name)."""
    if status is None:
        return None
    return {
        "client_id": status.get("client_id"), "client_secret": "(stored)",
        "project_id": status.get("project_id"), "environment": status.get("environment"),
    }
