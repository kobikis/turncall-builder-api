"""Provider Integrations at rest (ADR-0023, #55).

A Workspace's provider account: written here encrypted, never read back out to a
caller. `get_document` is the only decrypting reader, and only projection and
the picker catalogue call it.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .secret_box import SecretBox


class IntegrationError(RuntimeError):
    """The Integrations key is missing, malformed, or no longer the one used."""


BOX = SecretBox(
    env="INTEGRATIONS_ENCRYPTION_KEY",
    error=IntegrationError,
    disabled="provider Integrations and Workspace MCP servers are disabled",
    changed=(
        "A stored Integration could not be decrypted — INTEGRATIONS_ENCRYPTION_KEY "
        "has changed. Restore the original key, or reconnect the provider."
    ),
)

_PUBLIC = (
    "provider, last4, validation, version::text AS version, disconnecting, "
    "created_at, updated_at"
)


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    if isinstance(d.get("validation"), str):
        d["validation"] = json.loads(d["validation"])
    return d


async def save(
    pool: asyncpg.Pool,
    workspace_id: str,
    provider: str,
    *,
    document: dict[str, Any],
    last4: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Store or replace a credential. A new `version` means every agent needs it
    again; a replace also cancels a half-finished disconnect."""
    row = await pool.fetchrow(
        f"""
        INSERT INTO provider_integrations
            (workspace_id, provider, secret_encrypted, last4, validation)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (workspace_id, provider) DO UPDATE
            SET secret_encrypted = EXCLUDED.secret_encrypted,
                last4            = EXCLUDED.last4,
                validation       = EXCLUDED.validation,
                version          = gen_random_uuid(),
                disconnecting    = false,
                updated_at       = now()
        RETURNING {_PUBLIC}
        """,
        workspace_id,
        provider,
        BOX.encrypt(json.dumps(document)),
        last4,
        json.dumps(validation),
    )
    return _row(row)  # type: ignore[return-value]


async def get(pool: asyncpg.Pool, workspace_id: str, provider: str) -> dict[str, Any] | None:
    return _row(
        await pool.fetchrow(
            f"SELECT {_PUBLIC} FROM provider_integrations "
            "WHERE workspace_id = $1 AND provider = $2",
            workspace_id,
            provider,
        )
    )


async def list_for_workspace(pool: asyncpg.Pool, workspace_id: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"SELECT {_PUBLIC} FROM provider_integrations WHERE workspace_id = $1 "
        "ORDER BY provider",
        workspace_id,
    )
    return [_row(r) for r in rows]  # type: ignore[misc]


async def get_document(
    pool: asyncpg.Pool, workspace_id: str, provider: str
) -> dict[str, Any] | None:
    ciphertext = await pool.fetchval(
        "SELECT secret_encrypted FROM provider_integrations "
        "WHERE workspace_id = $1 AND provider = $2",
        workspace_id,
        provider,
    )
    return json.loads(BOX.decrypt(ciphertext)) if ciphertext else None


async def api_keys(pool: asyncpg.Pool, workspace_id: str) -> dict[str, str]:
    """provider -> api_key for every single-key Integration (the picker's input).

    An unreadable row is skipped rather than raised: the pickers degrade to the
    Builder's own environment, never to an error.
    """
    rows = await pool.fetch(
        "SELECT provider, secret_encrypted FROM provider_integrations "
        "WHERE workspace_id = $1 AND NOT disconnecting",
        workspace_id,
    )
    keys: dict[str, str] = {}
    for row in rows:
        try:
            key = json.loads(BOX.decrypt(row["secret_encrypted"])).get("api_key")
        except IntegrationError:
            continue
        if key:
            keys[row["provider"]] = key
    return keys


async def set_disconnecting(pool: asyncpg.Pool, workspace_id: str, provider: str) -> None:
    await pool.execute(
        "UPDATE provider_integrations SET disconnecting = true, updated_at = now() "
        "WHERE workspace_id = $1 AND provider = $2",
        workspace_id,
        provider,
    )


async def delete(pool: asyncpg.Pool, workspace_id: str, provider: str) -> None:
    """The master record and its projection log. Only after projection removal."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM integration_projections WHERE workspace_id = $1 AND provider = $2",
            workspace_id,
            provider,
        )
        await conn.execute(
            "DELETE FROM provider_integrations WHERE workspace_id = $1 AND provider = $2",
            workspace_id,
            provider,
        )


async def record_projection(
    pool: asyncpg.Pool,
    workspace_id: str,
    provider: str,
    agent_id: str,
    *,
    state: str,
    version: str | None,
    error: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO integration_projections
            (workspace_id, provider, agent_id, state, version, error)
        VALUES ($1, $2, $3, $4, $5::uuid, $6)
        ON CONFLICT (workspace_id, provider, agent_id) DO UPDATE
            SET state = EXCLUDED.state, version = EXCLUDED.version,
                error = EXCLUDED.error, updated_at = now()
        """,
        workspace_id,
        provider,
        agent_id,
        state,
        version,
        error,
    )


async def list_projections(
    pool: asyncpg.Pool, workspace_id: str, provider: str | None = None
) -> list[dict[str, Any]]:
    q = (
        "SELECT provider, agent_id, state, version::text AS version, error, updated_at "
        "FROM integration_projections WHERE workspace_id = $1"
    )
    args: list[Any] = [workspace_id]
    if provider is not None:
        q += " AND provider = $2"
        args.append(provider)
    return [dict(r) for r in await pool.fetch(q + " ORDER BY agent_id", *args)]
