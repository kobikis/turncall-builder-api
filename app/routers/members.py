"""Team management: invites + members (#34).

Admin-only within a Workspace (invite / list / change-role / remove), scoped to
the active Workspace from X-Workspace-Id via `require_admin`. Accepting an invite
is login-gated instead (the invitee isn't a Member yet) and is invite-only: the
token is the capability and the accepting user's email must be the invited one.

Each change is audited in its own transaction (ADR-0028): the entry and the
membership row commit together or not at all.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import auth_store, db, runtime
from ..audit import Audit
from ..deps import AuthContext, require_admin
from .auth import current_user

router = APIRouter()

Role = Literal["admin", "editor", "viewer"]


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Role

    @field_validator("email")
    @classmethod
    def _has_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("invalid email")
        return v


class RoleBody(BaseModel):
    role: Role


class AcceptBody(BaseModel):
    # Token in the body, not the URL path — keeps the invite secret out of access
    # logs, browser history, and Referer headers.
    token: str = Field(min_length=1, max_length=256)


@router.post("/members/invites")
async def create_invite(
    body: InviteBody, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    async with db.transaction(runtime.get_pool()) as conn:
        invite = await auth_store.create_invite(
            conn,
            workspace_id=ctx.workspace_id,
            email=body.email,
            role=body.role,
            invited_by=ctx.user["id"],
        )
        # The token is the invite's secret: masked by name, like any other.
        await audit.record(
            ctx, "invite", invite["id"], "created",
            resource_name=invite["email"], after=invite, db=conn,
        )
    return {"success": True, "data": invite}


@router.post("/invites/accept")
async def accept_invite(
    body: AcceptBody, audit: Audit, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    """Redeem an invite for the logged-in user (login-gated, not Workspace-gated —
    they join here). 404 unknown/used/expired token · 403 invited email doesn't
    match.

    Audited as the joining member, under the role they joined with."""
    try:
        async with db.transaction(runtime.get_pool()) as conn:
            result = await auth_store.accept_invite(
                conn,
                token=body.token,
                user_id=user["id"],
                user_email=user["email"],
            )
            joined = AuthContext(
                user=user, workspace_id=result["workspace_id"], role=result["role"]
            )
            await audit.record(
                joined, "member", user["id"],
                # Already a member: the invite is used up and nothing else moves.
                "invite_accepted" if result["already_member"] else "joined",
                resource_name=user["email"],
                changes={} if result["already_member"] else None,
                after={"role": result["role"]},
                db=conn,
            )
    except auth_store.InviteNotFound as exc:
        raise HTTPException(status_code=404, detail="invite not found or already used") from exc
    except auth_store.InviteEmailMismatch as exc:
        raise HTTPException(
            status_code=403, detail="this invite was sent to a different email"
        ) from exc
    return {"success": True, "data": result}


@router.get("/members")
async def list_members(ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    members = await auth_store.list_members(runtime.get_pool(), ctx.workspace_id)
    return {"success": True, "data": {"members": members}}


@router.put("/members/{user_id}")
async def change_member_role(
    user_id: UUID, body: RoleBody, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    # user_id typed as UUID so a malformed path segment is a 422 at the boundary,
    # not a ValueError → 500 inside the store.
    try:
        async with db.transaction(runtime.get_pool()) as conn:
            before = await auth_store.resolve_membership(conn, str(user_id), ctx.workspace_id)
            changed = await auth_store.change_member_role(
                conn, ctx.workspace_id, str(user_id), body.role
            )
            if changed:
                await audit.record(
                    ctx, "member", str(user_id), "role_changed",
                    resource_name=await auth_store.user_email(conn, str(user_id)),
                    before={"role": before}, after={"role": body.role}, db=conn,
                )
    except auth_store.LastAdmin as exc:
        raise HTTPException(
            status_code=409, detail="cannot demote the last admin of this workspace"
        ) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="member not found")
    return {"success": True, "data": {"user_id": str(user_id), "role": body.role}}


@router.delete("/members/{user_id}")
async def remove_member(
    user_id: UUID, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    try:
        async with db.transaction(runtime.get_pool()) as conn:
            before = await auth_store.resolve_membership(conn, str(user_id), ctx.workspace_id)
            removed = await auth_store.remove_member(conn, ctx.workspace_id, str(user_id))
            if removed:
                await audit.record(
                    ctx, "member", str(user_id), "removed",
                    resource_name=await auth_store.user_email(conn, str(user_id)),
                    before={"role": before}, after=None, db=conn,
                )
    except auth_store.LastAdmin as exc:
        raise HTTPException(
            status_code=409, detail="cannot remove the last admin of this workspace"
        ) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="member not found")
    return {"success": True, "data": {"removed": str(user_id)}}
