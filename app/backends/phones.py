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


# Never the token column: reads go to the console. `twilio_credentials` is
# derived so the console can say whose Twilio account a number runs on.
_COLS = (
    "id, e164, sid, routing_type, agent_id, project_id, server_url, "
    "server_url_secret, sms_enabled, twilio_account_sid, "
    "CASE WHEN twilio_auth_token_encrypted IS NULL THEN 'platform' ELSE 'number' END "
    "AS twilio_credentials"
)


async def record_number(pool: asyncpg.Pool, number: dict[str, Any]) -> None:
    """Mirror a bind. `number["workspace_id"]` scopes it to its Workspace (#31).

    The Twilio pair is written when given and otherwise kept: an edit that does
    not re-enter the token must not erase it."""
    await pool.execute(
        """INSERT INTO phone_numbers
             (id, e164, sid, routing_type, agent_id, project_id, server_url,
              server_url_secret, sms_enabled, workspace_id,
              twilio_account_sid, twilio_auth_token_encrypted)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
           ON CONFLICT (id) DO UPDATE SET
             e164=$2, sid=$3, routing_type=$4, agent_id=$5, project_id=$6,
             server_url=$7, server_url_secret=$8, sms_enabled=$9,
             workspace_id=$10,
             twilio_account_sid=COALESCE($11, phone_numbers.twilio_account_sid),
             twilio_auth_token_encrypted=COALESCE($12, phone_numbers.twilio_auth_token_encrypted)""",
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
        number.get("twilio_account_sid"),
        number.get("twilio_auth_token_encrypted"),
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


async def twilio_account(pool: asyncpg.Pool, phone_id: str) -> tuple[str, str] | None:
    """The number's own Twilio (account_sid, auth_token), decrypted for a re-send
    to TurnCall. None when it runs on the platform's account."""
    from ..integration_store import BOX

    row = await pool.fetchrow(
        "SELECT twilio_account_sid, twilio_auth_token_encrypted FROM phone_numbers WHERE id = $1",
        phone_id,
    )
    if not row or not row["twilio_auth_token_encrypted"]:
        return None
    return row["twilio_account_sid"], BOX.decrypt(row["twilio_auth_token_encrypted"])
