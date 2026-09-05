"""Persistence for GitHub connections and linked repos (ADR-0013).

A connection is per-User and holds an encrypted token; a linked repo lives on the
Agent Backend it pushes. All SQL lives here so the router stays about HTTP.

The plaintext token never crosses this boundary in either direction as a stored
value: callers hand in a token to save and get a token back to use, but what
lands in Postgres is always ciphertext.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from . import github

_CONNECTION_COLS = "id, user_id, github_login, expires_at, created_at"


def _connection(row: asyncpg.Record | None) -> dict[str, Any] | None:
    """Public shape of a connection — never includes the token."""
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "github_login": row["github_login"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "created_at": row["created_at"].isoformat(),
    }


async def save_connection(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    login: str,
    token: str,
    expires_at: datetime | None,
) -> dict[str, Any]:
    """Store (or replace) this user's connection. Reconnecting overwrites."""
    row = await pool.fetchrow(
        f"""
        INSERT INTO github_connections (id, user_id, github_login, token_encrypted, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id) DO UPDATE
            SET github_login    = EXCLUDED.github_login,
                token_encrypted = EXCLUDED.token_encrypted,
                expires_at      = EXCLUDED.expires_at,
                created_at      = now()
        RETURNING {_CONNECTION_COLS}
        """,
        uuid4(),
        UUID(user_id),
        login,
        github.encrypt_token(token),
        expires_at,
    )
    return _connection(row)  # type: ignore[return-value]


async def get_connection(pool: asyncpg.Pool, user_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        f"SELECT {_CONNECTION_COLS} FROM github_connections WHERE user_id = $1",
        UUID(user_id),
    )
    return _connection(row)


async def get_token(pool: asyncpg.Pool, connection_id: str) -> str | None:
    """Decrypt a stored token for use. Raises if the encryption key changed."""
    ciphertext = await pool.fetchval(
        "SELECT token_encrypted FROM github_connections WHERE id = $1",
        UUID(connection_id),
    )
    return github.decrypt_token(ciphertext) if ciphertext else None


async def delete_connection(pool: asyncpg.Pool, user_id: str) -> None:
    """Disconnect. Linked repos survive with a null connection — pushing stops,
    another user can take them over, nothing is deleted on GitHub."""
    await pool.execute("DELETE FROM github_connections WHERE user_id = $1", UUID(user_id))


# --- linked repo (on the Agent Backend) --------------------------------------

_LINK_COLS = (
    "github_owner, github_repo, github_branch, github_path, "
    "github_connection_id, github_pushed_at, github_push_error, github_tree_hash"
)


def _link(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None or not row["github_owner"]:
        return None
    return {
        "owner": row["github_owner"],
        "repo": row["github_repo"],
        "branch": row["github_branch"],
        "path": row["github_path"] or "",
        "connection_id": (
            str(row["github_connection_id"]) if row["github_connection_id"] else None
        ),
        "pushed_at": (
            row["github_pushed_at"].isoformat() if row["github_pushed_at"] else None
        ),
        "push_error": row["github_push_error"],
    }


async def get_link(pool: asyncpg.Pool, agent_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        f"SELECT {_LINK_COLS} FROM agent_backends WHERE agent_id = $1", agent_id
    )
    return _link(row)


async def set_link(
    pool: asyncpg.Pool,
    agent_id: str,
    *,
    owner: str,
    repo: str,
    branch: str,
    path: str,
    connection_id: str,
) -> None:
    """Point an Agent Backend at a repo. Clears any previous push state so a
    relink does not inherit the old divergence hash or error."""
    await pool.execute(
        """
        UPDATE agent_backends
           SET github_owner = $2, github_repo = $3, github_branch = $4,
               github_path = $5, github_connection_id = $6,
               github_tree_hash = NULL, github_push_error = NULL,
               github_pushed_at = NULL
         WHERE agent_id = $1
        """,
        agent_id,
        owner,
        repo,
        branch,
        path,
        UUID(connection_id),
    )


async def clear_link(pool: asyncpg.Pool, agent_id: str) -> None:
    await pool.execute(
        """
        UPDATE agent_backends
           SET github_owner = NULL, github_repo = NULL, github_branch = NULL,
               github_path = NULL, github_connection_id = NULL,
               github_tree_hash = NULL, github_pushed_at = NULL,
               github_push_error = NULL
         WHERE agent_id = $1
        """,
        agent_id,
    )


async def get_tree_hash(pool: asyncpg.Pool, agent_id: str) -> str | None:
    return await pool.fetchval(
        "SELECT github_tree_hash FROM agent_backends WHERE agent_id = $1", agent_id
    )


async def record_push(
    pool: asyncpg.Pool, agent_id: str, *, tree_hash: str | None, error: str | None
) -> None:
    """Record the outcome. A failure keeps the previous tree hash: the remote is
    unchanged, so the next attempt must compare against the same baseline."""
    if error is None:
        await pool.execute(
            """
            UPDATE agent_backends
               SET github_tree_hash = $2, github_pushed_at = now(),
                   github_push_error = NULL
             WHERE agent_id = $1
            """,
            agent_id,
            tree_hash,
        )
    else:
        await pool.execute(
            "UPDATE agent_backends SET github_push_error = $2 WHERE agent_id = $1",
            agent_id,
            error[:500],
        )
