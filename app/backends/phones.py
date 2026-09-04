"""Phone number mirror + the shared webhook project (ADR-0006).

The builder mirrors every bind in `phone_numbers` so the console lists numbers
with one query. Webhook-routed (call-init) numbers live in a single shared
builder-owned project, since they don't belong to any one agent's project.
"""

from __future__ import annotations

from typing import Any

import asyncpg


async def ensure_webhook_project(pool: asyncpg.Pool, client: Any) -> dict[str, str]:
    """Return the shared webhook project + key, creating it once if needed."""
    row = await pool.fetchrow("SELECT project_id, api_key FROM webhook_project WHERE id = 1")
    if row:
        return {"project_id": row["project_id"], "api_key": row["api_key"]}
    project_id = await client.create_project("builder-webhook-numbers")
    api_key = await client.create_api_key(project_id)
    await pool.execute(
        "INSERT INTO webhook_project (id, project_id, api_key) VALUES (1, $1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        project_id,
        api_key,
    )
    return {"project_id": project_id, "api_key": api_key}


_COLS = (
    "id, e164, sid, routing_type, agent_id, project_id, server_url, "
    "server_url_secret, sms_enabled"
)


async def record_number(pool: asyncpg.Pool, number: dict[str, Any]) -> None:
    """Mirror a bind. `number["workspace_id"]` scopes it to its Workspace (#31)."""
    await pool.execute(
        """INSERT INTO phone_numbers
             (id, e164, sid, routing_type, agent_id, project_id, server_url,
              server_url_secret, sms_enabled, workspace_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           ON CONFLICT (id) DO UPDATE SET
             e164=$2, sid=$3, routing_type=$4, agent_id=$5, project_id=$6,
             server_url=$7, server_url_secret=$8, sms_enabled=$9,
             workspace_id=$10""",
        number["id"],
        number["e164"],
        number["sid"],
        number["routing_type"],
        number.get("agent_id"),
        number["project_id"],
        number.get("server_url"),
        number.get("server_url_secret"),
        number.get("sms_enabled", False),
        number.get("workspace_id"),
    )


async def list_numbers(
    pool: asyncpg.Pool, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    q = f"SELECT {_COLS} FROM phone_numbers"
    args: list[Any] = []
    if workspace_id is not None:
        q += " WHERE workspace_id = $1"
        args.append(workspace_id)
    rows = await pool.fetch(q + " ORDER BY created_at", *args)
    return [dict(r) for r in rows]


async def get_number(
    pool: asyncpg.Pool, phone_id: str, workspace_id: str | None = None
) -> dict[str, Any] | None:
    q = f"SELECT {_COLS} FROM phone_numbers WHERE id = $1"
    args: list[Any] = [phone_id]
    if workspace_id is not None:
        q += " AND workspace_id = $2"
        args.append(workspace_id)
    row = await pool.fetchrow(q, *args)
    return dict(row) if row else None


async def delete_number(pool: asyncpg.Pool, phone_id: str) -> None:
    await pool.execute("DELETE FROM phone_numbers WHERE id = $1", phone_id)
