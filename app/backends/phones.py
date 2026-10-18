"""Phone number mirror + the webhook projects (ADR-0006).

The builder mirrors every bind in `phone_numbers` so the console lists numbers
with one query. Webhook-routed numbers (a customer's own call-init server) don't
belong to any one agent's project, so they live in a builder-owned project: one
per Workspace (#65), so the Workspace's provider keys can be projected into it
without lending them to anyone else. The original shared project (no workspace)
stays for numbers already bound in it and for platform-wide reads.
"""

from __future__ import annotations

from typing import Any

import asyncpg


def _project(row: asyncpg.Record) -> dict[str, str]:
    return {"project_id": row["project_id"], "api_key": row["api_key"]}


async def ensure_webhook_project(pool: asyncpg.Pool, client: Any) -> dict[str, str]:
    """Return the shared webhook project + key, creating it once if needed.

    Platform-wide reads only (`GET /v1/providers`) and numbers bound before
    #65. A new webhook bind goes to its Workspace's project instead."""
    row = await pool.fetchrow("SELECT project_id, api_key FROM webhook_project WHERE id = 1")
    if row:
        return _project(row)
    project_id = await client.create_project("builder-webhook-numbers")
    api_key = await client.create_api_key(project_id)
    await pool.execute(
        "INSERT INTO webhook_project (id, project_id, api_key) VALUES (1, $1, $2) "
        "ON CONFLICT (id) DO NOTHING",
        project_id,
        api_key,
    )
    return {"project_id": project_id, "api_key": api_key}


async def workspace_webhook_project(
    pool: asyncpg.Pool, workspace_id: str
) -> dict[str, str] | None:
    """The Workspace's own webhook project, if it has bound a webhook number."""
    row = await pool.fetchrow(
        "SELECT project_id, api_key FROM webhook_project WHERE workspace_id = $1",
        workspace_id,
    )
    return _project(row) if row else None


async def ensure_workspace_webhook_project(
    pool: asyncpg.Pool, client: Any, workspace_id: str
) -> tuple[dict[str, str], bool]:
    """The Workspace's webhook project, created on first use. True when it was
    created here, so the caller can project the Workspace's keys into it.

    Serialized per Workspace with a transaction-scoped advisory lock: two first
    binds racing would otherwise each create a TurnCall project, and the loser's
    would be orphaned with its key.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"webhook:{workspace_id}")
        row = await conn.fetchrow(
            "SELECT project_id, api_key FROM webhook_project WHERE workspace_id = $1",
            workspace_id,
        )
        if row:
            return _project(row), False
        project_id = await client.create_project(f"builder-webhook-numbers-{workspace_id[:8]}")
        api_key = await client.create_api_key(project_id)
        await conn.execute(
            "INSERT INTO webhook_project (project_id, api_key, workspace_id) VALUES ($1, $2, $3)",
            project_id,
            api_key,
            workspace_id,
        )
    return {"project_id": project_id, "api_key": api_key}, True


async def shared_webhook_numbers(pool: asyncpg.Pool, workspace_id: str) -> int:
    """How many of the Workspace's numbers are still bound in the shared
    project — they run on the platform's keys until rebound (#65)."""
    return await pool.fetchval(
        """SELECT count(*) FROM phone_numbers n
           JOIN webhook_project w ON w.project_id = n.project_id AND w.workspace_id IS NULL
           WHERE n.workspace_id = $1""",
        workspace_id,
    )


async def key_for_project(pool: asyncpg.Pool, project_id: str) -> str | None:
    """The key of the webhook project a number was bound in, shared or not."""
    return await pool.fetchval(
        "SELECT api_key FROM webhook_project WHERE project_id = $1", project_id
    )


def as_projection_target(workspace_id: str, project: dict[str, str]) -> dict[str, Any]:
    """The Workspace's webhook project in the shape Integrations project into.

    Its id is not an agent's, so the Integrations card (which lists projections
    of live agents only) never shows it as one."""
    return {
        "agent_id": f"webhook-numbers:{workspace_id}",
        "api_key": project["api_key"],
        "config": None,
    }


# Never the token column: reads go to the console. `twilio_credentials` is
# derived so the console can say whose Twilio account a number runs on.
_COLS = (
    "id, e164, sid, routing_type, agent_id, project_id, server_url, "
    "server_url_secret, sms_enabled, label, carrier, twilio_account_sid, "
    "purchase_status, telnyx_order_id, "
    "CASE WHEN twilio_source = 'workspace' THEN 'workspace' "
    "WHEN twilio_auth_token_encrypted IS NULL THEN 'platform' ELSE 'number' END "
    "AS twilio_credentials"
)


async def record_number(pool: asyncpg.Pool, number: dict[str, Any]) -> None:
    """Mirror a bind. `number["workspace_id"]` scopes it to its Workspace (#31).

    The Twilio pair is written when given and otherwise kept: an edit that does
    not re-enter the token must not erase it. A bought number's purchase state
    (`purchase_status`, `telnyx_order_id`) is written as given: absent is null,
    which is what a bind leaves."""
    await pool.execute(
        """INSERT INTO phone_numbers
             (id, e164, sid, routing_type, agent_id, project_id, server_url,
              server_url_secret, sms_enabled, workspace_id,
              twilio_account_sid, twilio_auth_token_encrypted, twilio_source, label,
              carrier, purchase_status, telnyx_order_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                   $16, $17)
           ON CONFLICT (id) DO UPDATE SET
             e164=$2, sid=$3, routing_type=$4, agent_id=$5, project_id=$6,
             server_url=$7, server_url_secret=$8, sms_enabled=$9,
             workspace_id=$10,
             twilio_account_sid=COALESCE($11, phone_numbers.twilio_account_sid),
             twilio_auth_token_encrypted=COALESCE($12, phone_numbers.twilio_auth_token_encrypted),
             twilio_source=COALESCE($13, phone_numbers.twilio_source),
             label=$14, carrier=$15, purchase_status=$16, telnyx_order_id=$17""",
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
        number.get("twilio_source"),
        number.get("label"),
        number.get("carrier", "twilio"),
        number.get("purchase_status"),
        number.get("telnyx_order_id"),
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
    """The number's Twilio (account_sid, auth_token), decrypted for a re-send to
    TurnCall: the Workspace's account for a number bound with it, else the
    number's own sealed pair. None when it runs on the platform's account."""
    from .. import twilio_store
    from ..integration_store import BOX

    row = await pool.fetchrow(
        "SELECT twilio_account_sid, twilio_auth_token_encrypted, twilio_source, workspace_id::text "
        "FROM phone_numbers WHERE id = $1",
        phone_id,
    )
    if not row:
        return None
    if row["twilio_source"] == "workspace":
        return await twilio_store.credentials(pool, row["workspace_id"])
    if not row["twilio_auth_token_encrypted"]:
        return None
    return row["twilio_account_sid"], BOX.decrypt(row["twilio_auth_token_encrypted"])


async def on_workspace_twilio(pool: asyncpg.Pool, workspace_id: str) -> list[dict[str, Any]]:
    """Numbers bound with the Workspace's Twilio account — what a rotation reaches."""
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM phone_numbers WHERE workspace_id = $1 AND twilio_source = 'workspace' "
        "ORDER BY created_at",
        workspace_id,
    )
    return [dict(r) for r in rows]
