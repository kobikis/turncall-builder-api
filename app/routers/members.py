"""Team management: invites + members (#34).

Admin-only within a Workspace (invite / list / change-role / remove), scoped to
the active Workspace from X-Workspace-Id via `require_admin`. Accepting an invite
is login-gated instead (the invitee isn't a Member yet) and is invite-only: the
token is the capability and the accepting user's email must be the invited one.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import auth_store, runtime
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
    body: InviteBody, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    invite = await auth_store.create_invite(
        runtime.get_pool(),
        workspace_id=ctx.workspace_id,
        email=body.email,
        role=body.role,
        invited_by=ctx.user["id"],
    )
    return {"success": True, "data": invite}


@router.post("/invites/accept")
async def accept_invite(
    body: AcceptBody, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    """Redeem an invite for the logged-in user (login-gated, not Workspace-gated —
    they join here). 404 unknown/used/expired token · 403 invited email doesn't
    match."""
    try:
        result = await auth_store.accept_invite(
            runtime.get_pool(),
            token=body.token,
            user_id=user["id"],
            user_email=user["email"],
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
    user_id: UUID, body: RoleBody, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    # user_id typed as UUID so a malformed path segment is a 422 at the boundary,
    # not a ValueError → 500 inside the store.
    try:
        changed = await auth_store.change_member_role(
            runtime.get_pool(), ctx.workspace_id, str(user_id), body.role
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
    user_id: UUID, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    try:
        removed = await auth_store.remove_member(
            runtime.get_pool(), ctx.workspace_id, str(user_id)
        )
    except auth_store.LastAdmin as exc:
        raise HTTPException(
            status_code=409, detail="cannot remove the last admin of this workspace"
        ) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="member not found")
    return {"success": True, "data": {"removed": str(user_id)}}
