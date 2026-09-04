"""Session + event persistence. jsonb is passed as text with an explicit ::jsonb
cast and json.loads'd on read (asyncpg returns jsonb as str by default)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg


async def create_session(
    pool: asyncpg.Pool,
    *,
    workspace_id: str,
    config: dict | None = None,
    history: list[dict] | None = None,
    agent_id: str | None = None,
    builder_provider: str | None = None,
    builder_model: str | None = None,
) -> str:
    """Create a session in a Workspace (#31). Seed config/history/agent_id to open
    an existing agent for chat editing; omit them for a fresh build session.
    builder_provider/builder_model pin the Session's Builder model (immutable;
    NULL means the deployment default)."""
    sid = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO sessions
               (id, history, config, agent_id, workspace_id,
                builder_provider, builder_model)
           VALUES ($1, $2::jsonb, $3::jsonb, $4, $5, $6, $7)""",
        uuid.UUID(sid),
        json.dumps(history or []),
        json.dumps(config) if config is not None else None,
        agent_id,
        workspace_id,
        builder_provider,
        builder_model,
    )
    return sid


async def get_session(
    pool: asyncpg.Pool, sid: str, workspace_id: str | None = None
) -> dict[str, Any] | None:
    try:
        key = uuid.UUID(sid)
    except ValueError:
        return None
    q = (
        "SELECT id, history, config, agent_id, builder_provider, builder_model "
        "FROM sessions WHERE id = $1"
    )
    args: list[Any] = [key]
    if workspace_id is not None:
        q += " AND workspace_id = $2"
        args.append(workspace_id)
    row = await pool.fetchrow(q, *args)
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "history": json.loads(row["history"]),
        "config": _load_config(row["config"]),
        "agent_id": row["agent_id"],
        "builder_provider": row["builder_provider"],
        "builder_model": row["builder_model"],
    }


def _load_config(raw: str | None) -> dict | None:
    """Decode config, unwrapping legacy double-encoded rows. Returns None if the
    stored value can't be decoded to an object (corrupt one-off dev rows)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
        for _ in range(3):  # unwrap double/triple-encoded strings
            if not isinstance(value, str):
                break
            value = json.loads(value)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def save_history(pool: asyncpg.Pool, sid: str, history: list[dict]) -> None:
    await pool.execute(
        "UPDATE sessions SET history = $2::jsonb WHERE id = $1",
        uuid.UUID(sid),
        json.dumps(history),
    )


async def save_config(pool: asyncpg.Pool, sid: str, config: dict | str) -> None:
    # Normalize: never store a JSON string as jsonb (that double-encodes it).
    if isinstance(config, str):
        config = json.loads(config)
    await pool.execute(
        "UPDATE sessions SET config = $2::jsonb WHERE id = $1",
        uuid.UUID(sid),
        json.dumps(config),
    )


async def set_agent_id(pool: asyncpg.Pool, sid: str, agent_id: str) -> None:
    await pool.execute(
        "UPDATE sessions SET agent_id = $2 WHERE id = $1", uuid.UUID(sid), agent_id
    )


async def get_creation_builder_choice(
    pool: asyncpg.Pool, agent_id: str
) -> tuple[str | None, str | None]:
    """The Builder model that created the agent: the earliest session linked to
    it is the creating one (edit sessions link later). (None, None) when the
    creation used the deployment default."""
    row = await pool.fetchrow(
        """SELECT builder_provider, builder_model FROM sessions
           WHERE agent_id = $1 ORDER BY created_at LIMIT 1""",
        agent_id,
    )
    if row is None:
        return None, None
    return row["builder_provider"], row["builder_model"]

# Events are no longer stored by the builder — they're routed to each agent's
# Agent Backend and read back via proxy (ADR-0005).
