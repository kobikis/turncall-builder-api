"""Reading the audit log of people (ADR-0028).

Admin-only: it shows what each colleague did, which is an admin's business, so
an editor or viewer is refused at the gate. Workspace-scoped like every other
read. Newest first, paged the way every other list here is (`page`, `limit`,
`total`). Nothing here writes: an entry is only ever appended by the change it
records (app/audit.py).

An entry's `engine_links` name what TurnCall recorded of the same change (#102);
`GET /audit/{id}/engine` reads those engine entries back, each with its own
project's key, which never leaves the builder.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import audit_store, runtime
from ..backends import registry
from ..deps import AuthContext, require_admin

router = APIRouter()


def _utc(value: datetime | None) -> datetime | None:
    """A time without a zone is UTC, as in TurnCall's audit log."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


@router.get("/audit")
async def list_audit_entries(
    member: str | None = Query(default=None, description="A member's user id"),
    agent: str | None = Query(default=None, description="An agent id"),
    since: datetime | None = Query(default=None, description="At or after (ISO 8601)"),
    until: datetime | None = Query(default=None, description="Before (ISO 8601)"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_admin),
) -> dict[str, Any]:
    since, until = _utc(since), _utc(until)
    if since and until and since >= until:
        raise HTTPException(status_code=422, detail="since must be before until")
    entries, total = await audit_store.list_entries(
        runtime.get_pool(),
        ctx.workspace_id,
        user_id=member,
        agent_id=agent,
        since=since,
        until=until,
        page=page,
        limit=limit,
    )
    return {"success": True, "data": entries, "total": total, "page": page, "limit": limit}


# One engine request writes a handful of entries (a publish: the version, the
# one it archived, the numbers it re-pointed). A bound, not a page size.
_MAX_PAGES = 20


@router.get("/audit/{entry_id}/engine")
async def engine_entries(
    entry_id: str, ctx: AuthContext = Depends(require_admin)
) -> dict[str, Any]:
    """What TurnCall recorded of one entry's change: for each of its
    `engine_links`, every engine entry that request wrote
    (`GET /v1/audit-log?request_id=`), read with the project's key.

    A link whose project the builder holds no key for, or that TurnCall no
    longer serves (a torn-down Agent's project is deleted, and its keys with
    it), comes back with `entries: null` and why, not as a failure of the rest."""
    pool = runtime.get_pool()
    entry = await audit_store.get_entry(pool, ctx.workspace_id, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="audit entry not found")
    client = runtime.get_client()
    links = await asyncio.gather(
        *(_read_link(pool, client, ctx.workspace_id, link) for link in entry["engine_links"])
    )
    return {"success": True, "data": list(links)}


async def _read_link(
    pool: Any, client: Any, workspace_id: str, link: dict[str, Any]
) -> dict[str, Any]:
    out = {**link, "entries": None, "error": None}
    project_id = link.get("project_id")
    key = await registry.key_for_project(pool, project_id, workspace_id) if project_id else None
    if not key:
        out["error"] = "the builder holds no key for this TurnCall project"
        return out
    entries: list[dict[str, Any]] = []
    cursor = None
    try:
        for _ in range(_MAX_PAGES):
            page = await client.list_audit_log(
                key, request_id=link["engine_audit_id"], cursor=cursor
            )
            entries.extend(page.get("data") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        out["error"] = (
            "TurnCall no longer accepts this project's key (the project was deleted)"
            if status == 401
            else f"TurnCall refused the read ({status})"
        )
        return out
    except httpx.HTTPError:
        out["error"] = "TurnCall is unreachable — retry in a moment"
        return out
    out["entries"] = entries
    return out
