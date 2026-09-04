"""Multi-workspace list + create (#33).

Login-gated (not workspace-gated): listing spans every Workspace the caller
belongs to, so it depends on `current_user` rather than the per-Workspace RBAC
gate. Switching is client-driven — the client just sends a different
`X-Workspace-Id`; a freshly created Workspace passes the gate because create
also inserts the caller's admin Membership.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from .. import auth_store, runtime
from .auth import current_user

router = APIRouter(prefix="/workspaces")


class CreateWorkspaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        # Strip before length validation so a whitespace-only name ("   ") is
        # rejected by min_length rather than stored as an empty string.
        return v.strip() if isinstance(v, str) else v


@router.get("")
async def list_workspaces(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    workspaces = await auth_store.list_workspaces_for_user(runtime.get_pool(), user["id"])
    return {"success": True, "data": {"workspaces": workspaces}}


@router.post("")
async def create_workspace(
    body: CreateWorkspaceBody, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    workspace = await auth_store.create_workspace(
        runtime.get_pool(), user["id"], body.name
    )
    return {"success": True, "data": workspace}
