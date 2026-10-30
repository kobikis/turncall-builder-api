"""A Workspace's Capability library at rest (#131, ADR-0029).

One TurnCall project per Workspace, holding its Capabilities, and an admin key
to it sealed like a provider key. `get` is the only decrypting reader.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from .integration_store import BOX


def _library(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "api_key": BOX.decrypt(row["api_key_encrypted"]),
        "created_at": row["created_at"],
    }


async def get(pool: asyncpg.Pool, workspace_id: str) -> dict[str, Any] | None:
    """{project_id, api_key, created_at}, or None before the first Capability."""
    row = await pool.fetchrow(
        "SELECT project_id, api_key_encrypted, created_at FROM capability_libraries "
        "WHERE workspace_id = $1",
        workspace_id,
    )
    return _library(row) if row else None


async def ensure(
    pool: asyncpg.Pool,
    workspace_id: str,
    create: Callable[[], Awaitable[tuple[str, str]]],
) -> tuple[dict[str, Any], bool]:
    """The Workspace's library, made with `create` (-> project_id, api_key) on
    first use. True when it was made here.

    Serialized per Workspace with a transaction-scoped advisory lock, held while
    `create` runs: two first Capability creates racing would otherwise each make
    a TurnCall project, and the loser's would be orphaned with its key. The
    primary key stops a second row whatever happens to the lock. Raises
    IntegrationError before anything is made when the key cannot be sealed.
    """
    BOX.encrypt("")  # no project is made whose key could not be kept
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"capability-library:{workspace_id}"
        )
        row = await conn.fetchrow(
            "SELECT project_id, api_key_encrypted, created_at FROM capability_libraries "
            "WHERE workspace_id = $1",
            workspace_id,
        )
        if row:
            return _library(row), False
        project_id, api_key = await create()
        row = await conn.fetchrow(
            "INSERT INTO capability_libraries (workspace_id, project_id, api_key_encrypted) "
            "VALUES ($1, $2, $3) RETURNING project_id, api_key_encrypted, created_at",
            workspace_id,
            project_id,
            BOX.encrypt(api_key),
        )
    return _library(row), True


async def library_project(pool: asyncpg.Pool, workspace_id: str) -> str | None:
    """The library's project, without its key: linking needs only this (it is
    the platform credential's write), so it never depends on the key unsealing."""
    return await pool.fetchval(
        "SELECT project_id FROM capability_libraries WHERE workspace_id = $1", workspace_id
    )


# --- each project's link to the library (#132) ------------------------------------


async def record_link(
    pool: asyncpg.Pool,
    workspace_id: str,
    project_id: str,
    *,
    library_project_id: str,
    state: str,
    error: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO capability_links
            (workspace_id, project_id, library_project_id, state, error)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (workspace_id, project_id) DO UPDATE
            SET library_project_id = EXCLUDED.library_project_id,
                state = EXCLUDED.state, error = EXCLUDED.error, updated_at = now()
        """,
        workspace_id,
        project_id,
        library_project_id,
        state,
        error,
    )


async def list_links(pool: asyncpg.Pool, workspace_id: str) -> dict[str, dict[str, Any]]:
    """project_id -> {library_project_id, state, error, updated_at}."""
    rows = await pool.fetch(
        "SELECT project_id, library_project_id, state, error, updated_at "
        "FROM capability_links WHERE workspace_id = $1",
        workspace_id,
    )
    return {r["project_id"]: {k: r[k] for k in r.keys() if k != "project_id"} for r in rows}
