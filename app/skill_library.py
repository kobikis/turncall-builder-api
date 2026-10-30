"""A Workspace's Skill library (#131, ADR-0029).

The Builder mints one TurnCall project per agent, and a TurnCall skill belongs
to one project, so two agents of a Workspace could never list the same one.
The library is one more project, per Workspace, that holds its skills. It is
made on the Workspace's first skill, through the platform bootstrap like any agent's project.

Every project of the Workspace is **linked** to it (#132): each agent's, and
the Workspace's webhook project, so each resolves skill names there. A
link is TurnCall's skill library link, which only the platform credential can
set. Linking follows ADR-0023's projection discipline: each project's link is
recorded, a re-run skips projects already linked to this library, and a failed
link is kept with why, shown, and retried.
"""

from __future__ import annotations

import logging
from typing import Any

from . import skill_store
from .backends import phones, registry
from .turncall_client import failure_reason

logger = logging.getLogger("turncall_builder")

LINKED = "linked"
FAILED = "failed"
PENDING = "pending"  # a project no link was ever tried for


async def ensure(pool: Any, client: Any, workspace_id: str) -> tuple[dict[str, Any], bool]:
    """The Workspace's library ({project_id, api_key, created_at}), made on
    first use: a project, then an admin key to it. True when made here.

    It holds no provider keys: a call's speech and model run on the agent
    project's, and a Skill's tools are webhooks."""

    async def create() -> tuple[str, str]:
        project_id = await client.create_project(f"builder-skills-{workspace_id[:8]}")
        return project_id, await client.create_api_key(project_id)

    return await skill_store.ensure(pool, workspace_id, create)


_PAGE = 200  # TurnCall's largest page of skill versions


async def versions(
    client: Any, api_key: str, *, name: str | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    """Every version in the library (of `name`, in `state`). TurnCall's
    refusal is raised as it came."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        body = await client.list_skills(api_key, name=name, state=state, page=page, limit=_PAGE)
        out += body.get("data") or []
        if not body.get("data") or len(out) >= int(body.get("total") or 0):
            return out
        page += 1


async def published(pool: Any, client: Any, workspace_id: str) -> list[dict[str, str]] | None:
    """The Workspace's published Skills, [{name, description}] by name:
    what the Builder may put in an agent's `skills` (#135). [] before the
    library exists. None when they could not be read, which a caller takes as
    "unknown", never as "none"."""
    try:
        library = await skill_store.get(pool, workspace_id)
        if library is None:
            return []
        rows = await versions(client, library["api_key"], state="published")
    except Exception:  # noqa: BLE001 — a builder turn goes on without the list
        logger.exception("reading the published skills of %s failed", workspace_id)
        return None
    return sorted(
        ({"name": r["name"], "description": r.get("description") or ""} for r in rows),
        key=lambda c: c["name"],
    )


async def _targets(pool: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Every project of the Workspace: each agent's, then its webhook project
    once it has one."""
    targets = [
        {
            "project_id": b["project_id"],
            "kind": "agent",
            "agent_id": b["agent_id"],
            "name": (b.get("config") or {}).get("name") or b.get("slug"),
        }
        for b in await registry.list_backends(pool, workspace_id)
        if b.get("project_id")
    ]
    webhook = await phones.workspace_webhook_project(pool, workspace_id)
    if webhook:
        targets.append({
            "project_id": webhook["project_id"], "kind": "webhook_numbers",
            "agent_id": None, "name": None,
        })
    return targets


async def _link(
    pool: Any, client: Any, workspace_id: str, project_id: str, library_project_id: str
) -> dict[str, Any]:
    """One project's link, recorded: `linked`, or `failed` with why."""
    error = None
    try:
        await client.set_skill_library(project_id, library_project_id)
    except Exception as exc:  # noqa: BLE001 — recorded, and retryable
        logger.warning("linking %s to its skill library failed: %s", project_id, exc)
        error = failure_reason(exc)
    state = FAILED if error else LINKED
    await skill_store.record_link(
        pool, workspace_id, project_id,
        library_project_id=library_project_id, state=state, error=error,
    )
    return {"project_id": project_id, "state": state, "error": error}


async def link_all(pool: Any, client: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Link every project of the Workspace not yet linked to its library, and
    return what was written, each with the state it `was` in: an empty list
    when every one already was linked (or there is no library)."""
    library_project_id = await skill_store.library_project(pool, workspace_id)
    if library_project_id is None:
        return []
    links = await skill_store.list_links(pool, workspace_id)
    written = []
    for target in await _targets(pool, workspace_id):
        held = links.get(target["project_id"])
        if held and held["state"] == LINKED and held["library_project_id"] == library_project_id:
            continue
        was = held["state"] if held and held["library_project_id"] == library_project_id else PENDING
        written.append({
            **target,
            "was": was,
            **await _link(pool, client, workspace_id, target["project_id"], library_project_id),
        })
    return written


async def link_new_project(
    pool: Any, client: Any, workspace_id: str, project_id: str
) -> dict[str, Any] | None:
    """Link a project just made (an agent's at Create, the webhook project on
    its first bind) when the Workspace has a library; None when it has none.
    Never raises: the project exists either way, and a failure is recorded for
    the retry."""
    try:
        library_project_id = await skill_store.library_project(pool, workspace_id)
        if library_project_id is None:
            return None
        return await _link(pool, client, workspace_id, project_id, library_project_id)
    except Exception:  # noqa: BLE001 — never costs the caller its project
        logger.exception("linking new project %s to its skill library failed", project_id)
        return None


async def report(pool: Any, workspace_id: str) -> dict[str, Any]:
    """The library, and where each of the Workspace's projects stands: linked,
    failed (with why), or pending (never tried, or linked to an older library)."""
    library_project_id = await skill_store.library_project(pool, workspace_id)
    if library_project_id is None:
        return {"library": None, "links": [], "complete": True}
    links = await skill_store.list_links(pool, workspace_id)
    out = []
    for target in await _targets(pool, workspace_id):
        held = links.get(target["project_id"])
        current = bool(held) and held["library_project_id"] == library_project_id
        out.append({
            **target,
            "state": held["state"] if current else PENDING,
            "error": held["error"] if current else None,
            "updated_at": held["updated_at"] if current else None,
        })
    return {
        "library": {"project_id": library_project_id},
        "links": out,
        "complete": all(link["state"] == LINKED for link in out),
    }
