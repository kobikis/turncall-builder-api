"""`audit_entries` persistence (ADR-0028). The only module that names the table.

Four operations and no more: append one entry, read a Workspace's entries
(a page of them, or one), and the retention janitor's purge. There is no update and no delete of a
single entry, and the database refuses both anyway (migration 0020).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .db import jsonb

# The janitor's mark. The append-only trigger lets a DELETE through only in a
# transaction that set it, so nothing else that connects as the app can.
RETENTION_GUC = "builder.audit_retention"

_COLUMNS = (
    "id, workspace_id, user_id, user_email, role, resource_type, resource_id, "
    "resource_name, action, agent_ids, changes, request_id, engine_links, outcome, error, "
    "created_at"
)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def insert(db: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Append one entry. `db` is a pool or a connection in the change's own
    transaction, so a change that rolls back takes its entry with it."""
    row = await db.fetchrow(
        f"""INSERT INTO audit_entries
               (id, workspace_id, user_id, user_email, role, resource_type,
                resource_id, resource_name, action, agent_ids, changes, request_id,
                engine_links, outcome, error)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13::jsonb,
                    $14, $15)
            RETURNING {_COLUMNS}""",
        uuid.UUID(entry["id"]),
        uuid.UUID(entry["workspace_id"]),
        uuid.UUID(entry["user_id"]),
        entry["user_email"],
        entry["role"],
        entry["resource_type"],
        entry["resource_id"],
        entry.get("resource_name"),
        entry["action"],
        list(entry.get("agent_ids") or []),
        json.dumps(entry.get("changes") or {}),
        uuid.UUID(entry["request_id"]),
        json.dumps(entry.get("engine_links") or []),
        entry.get("outcome") or "completed",
        entry.get("error"),
    )
    return _view(row)


def _view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "user_id": str(row["user_id"]),
        "user_email": row["user_email"],
        "role": row["role"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "resource_name": row["resource_name"],
        "action": row["action"],
        "agent_ids": list(row["agent_ids"]),
        "changes": jsonb(row["changes"]),
        "request_id": str(row["request_id"]),
        # [{project_id, engine_audit_id}]: each TurnCall request the change made
        # that TurnCall audited; empty when it never reached TurnCall (#102).
        "engine_links": jsonb(row["engine_links"]),
        # "completed", or "partial" for a change that failed after some of it
        # had landed, with `error` saying why (app/audit.py, Change).
        "outcome": row["outcome"],
        "error": row["error"],
        "created_at": row["created_at"].isoformat(),
    }


async def get_entry(db: Any, workspace_id: str, entry_id: str) -> dict[str, Any] | None:
    """One of a Workspace's entries; another Workspace's, or no such id, is None."""
    entry = _uuid(entry_id)
    if entry is None:
        return None
    row = await db.fetchrow(
        f"SELECT {_COLUMNS} FROM audit_entries WHERE id = $1 AND workspace_id = $2",
        entry,
        uuid.UUID(workspace_id),
    )
    return _view(row) if row else None


async def list_entries(
    db: Any,
    workspace_id: str,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """One page of a Workspace's entries, newest first, and how many match.

    `since` is inclusive and `until` exclusive, so consecutive windows never
    count an entry twice. A `user_id` that is not a uuid matches nothing."""
    where = ["workspace_id = $1"]
    args: list[Any] = [uuid.UUID(workspace_id)]
    if user_id is not None:
        args.append(_uuid(user_id))
        where.append(f"user_id = ${len(args)}")
    if agent_id is not None:
        # Containment, not `= ANY(...)`: only @>, <@ and && can use the GIN
        # index on agent_ids, and this is the lookup it exists for.
        args.append(agent_id)
        where.append(f"agent_ids @> ARRAY[${len(args)}]::text[]")
    if since is not None:
        args.append(since)
        where.append(f"created_at >= ${len(args)}")
    if until is not None:
        args.append(until)
        where.append(f"created_at < ${len(args)}")
    clause = " AND ".join(where)
    total = await db.fetchval(f"SELECT count(*) FROM audit_entries WHERE {clause}", *args)
    rows = await db.fetch(
        f"""SELECT {_COLUMNS} FROM audit_entries WHERE {clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""",
        *args,
        limit,
        (page - 1) * limit,
    )
    return [_view(r) for r in rows], int(total)


async def purge_older_than(pool: Any, days: int, *, batch: int = 1000) -> int:
    """The retention janitor: delete entries older than `days`, every
    Workspace's, in batches. Returns how many went.

    Each batch is its own short transaction carrying the janitor's mark, and
    skips rows another server's sweep has locked rather than waiting on them."""
    removed = 0
    while True:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL {RETENTION_GUC} = 'on'")
                status = await conn.execute(
                    """DELETE FROM audit_entries WHERE id IN (
                           SELECT id FROM audit_entries
                            WHERE created_at < now() - make_interval(days => $1)
                            LIMIT $2
                            FOR UPDATE SKIP LOCKED)""",
                    days,
                    batch,
                )
        count = int(status.split()[-1])
        removed += count
        if count < batch:
            return removed
