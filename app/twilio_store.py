"""A Workspace's Twilio account (the Integrations page's Twilio card).

Stored like a provider Integration — token sealed under
INTEGRATIONS_ENCRYPTION_KEY, only a last-four ever shown — but kept apart from
`provider_integrations`, because it is never projected into agents' TurnCall
projects: TurnCall holds Twilio credentials per *number*, and the Builder
passes this account's pair on each bind.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .integration_store import BOX

_PUBLIC = "account_sid, last4, validation, created_at, updated_at"


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    if isinstance(d.get("validation"), str):
        d["validation"] = json.loads(d["validation"])
    return d


async def get(pool: asyncpg.Pool, workspace_id: str) -> dict[str, Any] | None:
    return _row(
        await pool.fetchrow(
            f"SELECT {_PUBLIC} FROM twilio_accounts WHERE workspace_id = $1", workspace_id
        )
    )


async def credentials(pool: asyncpg.Pool, workspace_id: str) -> tuple[str, str] | None:
    """(account_sid, auth_token), decrypted — for a bind or a Twilio API call."""
    row = await pool.fetchrow(
        "SELECT account_sid, auth_token_encrypted FROM twilio_accounts WHERE workspace_id = $1",
        workspace_id,
    )
    return (row["account_sid"], BOX.decrypt(row["auth_token_encrypted"])) if row else None


async def save(
    pool: asyncpg.Pool,
    workspace_id: str,
    *,
    account_sid: str,
    auth_token: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    row = await pool.fetchrow(
        f"""
        INSERT INTO twilio_accounts (workspace_id, account_sid, auth_token_encrypted, last4, validation)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (workspace_id) DO UPDATE
            SET account_sid = EXCLUDED.account_sid,
                auth_token_encrypted = EXCLUDED.auth_token_encrypted,
                last4 = EXCLUDED.last4,
                validation = EXCLUDED.validation,
                updated_at = now()
        RETURNING {_PUBLIC}
        """,
        workspace_id,
        account_sid,
        BOX.encrypt(auth_token),
        auth_token[-4:],
        json.dumps(validation),
    )
    return _row(row)  # type: ignore[return-value]


async def delete(pool: asyncpg.Pool, workspace_id: str) -> None:
    await pool.execute("DELETE FROM twilio_accounts WHERE workspace_id = $1", workspace_id)
