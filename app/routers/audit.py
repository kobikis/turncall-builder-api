"""Reading the audit log of people (ADR-0028).

Admin-only: it shows what each colleague did, which is an admin's business, so
an editor or viewer is refused at the gate. Workspace-scoped like every other
read. Newest first, paged the way every other list here is (`page`, `limit`,
`total`). Nothing here writes: an entry is only ever appended by the change it
records (app/audit.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import audit_store, runtime
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
