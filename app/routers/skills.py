"""The Workspace's own Builder skills (ADR-0015).

Reading is any member's; writing is admin-only. A skill is prompt text injected
into a Builder that holds the deployment's LLM key and provisions real TurnCall
projects — a narrower trust boundary than editing one agent, so it gets a
narrower role.

Addressed by name rather than id: the name is the identity deepagents uses, so
PUT /skills/{name} creating-or-replacing is the operation that actually exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, runtime, workspace_skills
from ..audit import Audit
from ..deps import AuthContext, require_admin, require_member

router = APIRouter()


class SkillBody(BaseModel):
    # The only part always in front of the model. Everything else is read on
    # demand, so this field decides whether the skill is ever opened at all.
    description: str = Field(max_length=workspace_skills.MAX_DESCRIPTION)
    body: str = Field(max_length=workspace_skills.MAX_BODY)


@router.get("/skills")
async def list_skills(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """This workspace's skills, plus the built-in names — the console needs both
    to explain why a name is refused."""
    return {
        "success": True,
        "data": {
            "skills": await workspace_skills.list_skills(
                runtime.get_pool(), ctx.workspace_id
            ),
            "reserved": sorted(workspace_skills.builtin_names()),
        },
    }


@router.put("/skills/{name}")
async def save_skill(
    name: str, body: SkillBody, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    try:
        workspace_skills.validate(name, body.description, body.body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with db.transaction(runtime.get_pool()) as conn:
        before = await workspace_skills.get_skill(conn, ctx.workspace_id, name)
        skill = await workspace_skills.save_skill(
            conn,
            ctx.workspace_id,
            name=name,
            description=body.description,
            body=body.body,
        )
        await audit.record(
            ctx, "skill", name, "updated" if before else "created",
            resource_name=name, before=_audited(before), after=_audited(skill), db=conn,
        )
    return {"success": True, "data": skill}


def _audited(skill: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an entry compares: the text, not the row's id or timestamps."""
    if skill is None:
        return None
    return {"description": skill.get("description"), "body": skill.get("body")}


@router.delete("/skills/{name}")
async def delete_skill(
    name: str, audit: Audit, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Takes effect on the next turn of every open conversation — the mount is
    rebuilt per turn, so there is no cache to invalidate."""
    async with db.transaction(runtime.get_pool()) as conn:
        before = await workspace_skills.get_skill(conn, ctx.workspace_id, name)
        deleted = await workspace_skills.delete_skill(conn, ctx.workspace_id, name)
        if deleted:
            await audit.record(
                ctx, "skill", name, "deleted",
                resource_name=name, before=_audited(before), after=None, db=conn,
            )
    if not deleted:
        raise HTTPException(status_code=404, detail="no such skill")
    return {"success": True, "data": {"deleted": name}}
