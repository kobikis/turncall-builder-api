"""A Workspace's Skills: TurnCall skills in its Skill library (#131).

Each route proxies TurnCall's skill route on the library's key. A Skill is
named, and each of its versions has an id, as in TurnCall: `/skills`
addresses names, `/skill-versions/{id}` one version. A version is a draft
until published; only a draft is edited; publishing archives the live one, and
rolling back restores an archived one. TurnCall validates every write and its
422 comes back naming the field (`errors`, see `_helpers.field_errors`).

Reads are any member's, writes an editor's, as on agents. Every write is an
audit entry (ADR-0028). The library itself is made on the first create.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from .. import skill_library, skill_store, integration_store, runtime
from .._helpers import Refusal, _turncall_http_error
from ..backends import registry
from ..audit import Audit, AuditTrail
from ..deps import AuthContext, require_editor, require_member

router = APIRouter()

# TurnCall's skill name rule (SKILL_NAME_PATTERN), checked here too so a name
# never reaches a TurnCall path it could leave.
NAME_PATTERN = r"^[a-z_][a-z0-9_]*$"
Name = Annotated[str, Path(max_length=64, pattern=NAME_PATTERN)]


class SkillCreate(BaseModel):
    """A new draft: version 1 of a new name, or the next version of one the
    Workspace has (start it from the live one by sending its content).
    TurnCall checks every field; the Builder only forwards them."""

    name: str
    description: str
    instructions: str
    # Webhook tools, each calling the author's own Tool server by URL: TurnCall's
    # tool shape (name, description, parameters_schema, webhook_url, ...).
    tools: list[dict[str, Any]] = Field(default_factory=list)


class SkillEdit(BaseModel):
    """A draft's content. A field left out (or null) is kept."""

    description: str | None = None
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None


def _no_library() -> HTTPException:
    return HTTPException(status_code=404, detail="this workspace has no skills yet")


async def library_or_none(workspace_id: str) -> dict[str, Any] | None:
    try:
        return await skill_store.get(runtime.get_pool(), workspace_id)
    except integration_store.IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _library(workspace_id: str) -> dict[str, Any]:
    library = await library_or_none(workspace_id)
    if library is None:
        raise _no_library()
    return library


async def _ensure_library(audit: AuditTrail, ctx: AuthContext) -> dict[str, Any]:
    """The Workspace's library, made on its first Skill, every project of
    the Workspace linked to it, and both recorded."""
    pool, client = runtime.get_pool(), runtime.get_client()
    try:
        library, created = await skill_library.ensure(pool, client, ctx.workspace_id)
    except integration_store.IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        # The platform key is wrong or unset (turncall#102): nothing was made.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if created:
        # Begun before linking: the library exists from here, so linking that
        # raises leaves it in the log as partial, never absent.
        change = audit.begin(
            ctx, "skill_library", ctx.workspace_id, "created",
            resource_name="Skill library",
            after={"project_id": library["project_id"], "links": {}},
        )
        # Each link is recorded, and one that failed is retried from the
        # library's page: the Skill is made either way.
        links = await skill_library.link_all(pool, client, ctx.workspace_id)
        await change.record(
            after={"project_id": library["project_id"], "links": _link_states(links)},
            agent_ids=[link["agent_id"] for link in links],
        )
    return library


def _link_states(links: list[dict[str, Any]]) -> dict[str, str]:
    """What an entry says of the links it wrote: project -> linked | failed."""
    return {link["project_id"]: link["state"] for link in links}


async def _call(coro: Any) -> Any:
    """A TurnCall skill call, its refusal answered as TurnCall gave it."""
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        raise _turncall_http_error(exc) from exc


async def all_versions(
    api_key: str, *, name: str | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    """Every version in the library (of `name`, in `state`)."""
    return await _call(
        skill_library.versions(runtime.get_client(), api_key, name=name, state=state)
    )


def _ref(version: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if version is None else {"id": version["id"], "version": version["version"]}


def summaries(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per Skill, by name: its live version, its draft if it
    has one, its latest, and how many versions it has."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for v in versions:
        by_name.setdefault(v["name"], []).append(v)
    out = []
    for name in sorted(by_name):
        vs = sorted(by_name[name], key=lambda v: v["version"], reverse=True)
        live = next((v for v in vs if v["state"] == "published"), None)
        draft = next((v for v in vs if v["state"] == "draft"), None)
        out.append({
            "name": name,
            "description": (live or vs[0])["description"],
            "live": _ref(live),
            "draft": _ref(draft),
            "latest": {**_ref(vs[0]), "state": vs[0]["state"]},  # type: ignore[dict-item]
            "versions": len(vs),
        })
    return out


def _audited(version: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an entry compares: the Skill's content and state, not its
    timestamps. Tool secrets are masked by the trail."""
    if version is None:
        return None
    return {
        k: version.get(k)
        for k in ("name", "version", "state", "description", "instructions", "tools")
    }


# --- by name --------------------------------------------------------------------


@router.get("/skills")
async def list_skills(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """The Workspace's Skills, one entry per name. Empty, and no library,
    before the first one is created."""
    library = await library_or_none(ctx.workspace_id)
    if library is None:
        return {"success": True, "data": {"library": None, "skills": []}}
    versions = await all_versions(library["api_key"])
    return {
        "success": True,
        "data": {
            "library": {"project_id": library["project_id"]},
            "skills": summaries(versions),
        },
    }


@router.post("/skills")
async def create_skill(
    body: SkillCreate, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """A new draft version of the named Skill; the library is made first
    if this is the Workspace's first."""
    library = await _ensure_library(audit, ctx)
    change = audit.begin(
        ctx, "skill", body.name, "created", landed=False, resource_name=body.name,
    )
    row = await _call(runtime.get_client().create_skill(body.model_dump(), library["api_key"]))
    await change.record(resource_id=row["id"], after=_audited(row))
    return {"success": True, "data": row}


@router.get("/skills/{name}")
async def get_skill(name: Name, ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """One Skill's versions, newest first, and which is live."""
    library = await _library(ctx.workspace_id)
    versions = sorted(
        await all_versions(library["api_key"], name=name),
        key=lambda v: v["version"], reverse=True,
    )
    if not versions:
        raise HTTPException(status_code=404, detail=f"no skill named {name!r}")
    live = next((v for v in versions if v["state"] == "published"), None)
    return {
        "success": True,
        "data": {"name": name, "live": _ref(live), "versions": versions},
    }


async def _console_agents(workspace_id: str) -> dict[str, dict[str, Any]]:
    """project_id -> the Console agent it runs: one TurnCall project per agent."""
    return {
        b["project_id"]: b
        for b in await registry.list_backends(runtime.get_pool(), workspace_id)
        if b.get("project_id")
    }


def _as_console_agent(
    row: dict[str, Any], agents: dict[str, dict[str, Any]], *, name_key: str, version_key: str
) -> dict[str, Any]:
    """A TurnCall agent row named as the Console agent of its project. A project
    the registry does not know is kept, as unknown, under TurnCall's name."""
    backend = agents.get(row.get("project_id") or "")
    return {
        "agent_id": backend["agent_id"] if backend else None,
        "name": ((backend.get("config") or {}).get("name") or backend.get("slug"))
        if backend else row.get(name_key),
        "known": backend is not None,
        "project_id": row.get("project_id"),
        "turncall_agent_id": row.get("agent_id"),
        "agent_version": row.get(version_key),
        "agent_state": row.get("agent_state", row.get("state")),
    }


@router.get("/skills/{name}/agents")
async def skill_agents(
    name: Name, ctx: AuthContext = Depends(require_member)
) -> dict[str, Any]:
    """The agents listing the Skill, across every project linked to the
    library, each with the version it resolves to now and whether it is pinned."""
    library = await _library(ctx.workspace_id)
    rows = await _call(runtime.get_client().skill_agents(name, library["api_key"]))
    agents = await _console_agents(ctx.workspace_id)
    return {
        "success": True,
        "data": [
            {
                **_as_console_agent(row, agents, name_key="agent_name", version_key="agent_version"),
                "resolved_skill_id": row.get("resolved_skill_id"),
                "resolved_version": row.get("resolved_version"),
                "pinned": bool(row.get("pinned")),
            }
            for row in rows
        ],
    }


async def _in_use(exc: httpx.HTTPStatusError, name: str, workspace_id: str) -> HTTPException:
    """TurnCall's refusal to delete a Skill an agent lists, naming the
    Console agents; any other refusal as TurnCall gave it."""
    try:
        body = exc.response.json()
        listed = (body.get("details") or {})["agents"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return _turncall_http_error(exc)
    agents = await _console_agents(workspace_id)
    named = [_as_console_agent(a, agents, name_key="name", version_key="version") for a in listed]
    return Refusal(
        409,
        f"{name!r} is used by " + ", ".join(repr(a["name"]) for a in named)
        + "; take it off them first",
        "skill_in_use",
        skill=name,
        agents=named,
    )


# --- one version ----------------------------------------------------------------


async def _version(library: dict[str, Any], version_id: UUID) -> dict[str, Any]:
    return await _call(runtime.get_client().get_skill(str(version_id), library["api_key"]))


@router.get("/skill-versions/{version_id}")
async def get_version(version_id: UUID, ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    library = await _library(ctx.workspace_id)
    return {"success": True, "data": await _version(library, version_id)}


@router.put("/skill-versions/{version_id}")
async def edit_version(
    version_id: UUID, body: SkillEdit, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Edit a draft. A published or archived version is TurnCall's 409:
    create the next version instead."""
    library = await _library(ctx.workspace_id)
    before = await _version(library, version_id)
    change = audit.begin(
        ctx, "skill", str(version_id), "updated", landed=False,
        resource_name=before["name"], before=_audited(before),
    )
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    row = await _call(runtime.get_client().update_skill(str(version_id), fields, library["api_key"]))
    await change.record(after=_audited(row))
    return {"success": True, "data": row}


async def _transition(
    version_id: UUID, action: str, audit: AuditTrail, ctx: AuthContext
) -> dict[str, Any]:
    library = await _library(ctx.workspace_id)
    before = await _version(library, version_id)
    change = audit.begin(
        ctx, "skill", str(version_id), action, landed=False,
        resource_name=before["name"], before=_audited(before),
    )
    client = runtime.get_client()
    move = client.publish_skill if action == "published" else client.rollback_skill
    row = await _call(move(str(version_id), library["api_key"]))
    await change.record(after=_audited(row))
    return {"success": True, "data": row}


@router.post("/skill-versions/{version_id}/publish")
async def publish_version(
    version_id: UUID, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Make a draft the live version: every agent following the Skill
    gets it on its next call. The previous live version is archived."""
    return await _transition(version_id, "published", audit, ctx)


@router.post("/skill-versions/{version_id}/rollback")
async def rollback_version(
    version_id: UUID, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Restore an archived version as the live one, for every agent at once."""
    return await _transition(version_id, "rolled_back", audit, ctx)


@router.delete("/skill-versions/{version_id}")
async def delete_version(
    version_id: UUID, audit: Audit, ctx: AuthContext = Depends(require_editor)
) -> dict[str, Any]:
    """Delete one version. Refused (409) while any agent lists the Skill."""
    library = await _library(ctx.workspace_id)
    before = await _version(library, version_id)
    change = audit.begin(
        ctx, "skill", str(version_id), "deleted", landed=False,
        resource_name=before["name"], before=_audited(before), after=None,
    )
    try:
        await runtime.get_client().delete_skill(str(version_id), library["api_key"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            raise await _in_use(exc, before["name"], ctx.workspace_id) from exc
        raise _turncall_http_error(exc) from exc
    await change.record()
    return {"success": True, "data": {"deleted": str(version_id)}}


# --- the library and its links (#132) ---------------------------------------------


@router.get("/skill-library")
async def get_library(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    """The Workspace's library, and each of its projects' link to it: `linked`,
    `failed` (with why), or `pending`. `library` is null before the first
    Skill."""
    return {
        "success": True,
        "data": await skill_library.report(runtime.get_pool(), ctx.workspace_id),
    }


@router.post("/skill-library/links")
async def relink(audit: Audit, ctx: AuthContext = Depends(require_editor)) -> dict[str, Any]:
    """Link every project not yet linked: a failed link again, or a project
    made while the library was. Projects already linked are left alone, so
    running it again writes nothing."""
    pool = runtime.get_pool()
    if await skill_store.library_project(pool, ctx.workspace_id) is None:
        raise _no_library()
    links = await skill_library.link_all(pool, runtime.get_client(), ctx.workspace_id)
    if links:
        await audit.record(
            ctx, "skill_library", ctx.workspace_id, "linked",
            resource_name="Skill library",
            changes={
                f"/links/{link['project_id']}": {"before": link["was"], "after": link["state"]}
                for link in links
            },
            agent_ids=[link["agent_id"] for link in links],
        )
    else:
        audit.unchanged()
    return {"success": True, "data": await skill_library.report(pool, ctx.workspace_id)}
