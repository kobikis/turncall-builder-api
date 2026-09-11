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

from .. import runtime, workspace_skills
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
    name: str, body: SkillBody, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    try:
        workspace_skills.validate(name, body.description, body.body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    skill = await workspace_skills.save_skill(
        runtime.get_pool(),
        ctx.workspace_id,
        name=name,
        description=body.description,
        body=body.body,
    )
    return {"success": True, "data": skill}


@router.delete("/skills/{name}")
async def delete_skill(
    name: str, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """Takes effect on the next turn of every open conversation — the mount is
    rebuilt per turn, so there is no cache to invalidate."""
    if not await workspace_skills.delete_skill(runtime.get_pool(), ctx.workspace_id, name):
        raise HTTPException(status_code=404, detail="no such skill")
    return {"success": True, "data": {"deleted": name}}
