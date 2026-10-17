"""Compliance settings at rest (#103): the Workspace's master, each Agent's
override, and the per-target projection log. Nothing here is secret — the
settings are policy, not credentials — so nothing is sealed."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .db import jsonb


async def get_workspace(pool: asyncpg.Pool, workspace_id: str) -> dict[str, Any] | None:
    """{redaction, retention, updated_at}, or None while the Workspace has never
    set one (the engine's defaults apply)."""
    row = await pool.fetchrow(
        "SELECT redaction, retention, updated_at FROM workspace_compliance WHERE workspace_id = $1",
        workspace_id,
    )
    if row is None:
        return None
    return {
        "redaction": jsonb(row["redaction"]),
        "retention": jsonb(row["retention"]),
        "updated_at": row["updated_at"],
    }


async def save_workspace(
    pool: asyncpg.Pool,
    workspace_id: str,
    *,
    redaction: dict[str, Any],
    retention: dict[str, Any],
) -> dict[str, Any]:
    await pool.execute(
        """
        INSERT INTO workspace_compliance (workspace_id, redaction, retention)
        VALUES ($1, $2::jsonb, $3::jsonb)
        ON CONFLICT (workspace_id) DO UPDATE
            SET redaction = EXCLUDED.redaction,
                retention = EXCLUDED.retention,
                updated_at = now()
        """,
        workspace_id,
        json.dumps(redaction),
        json.dumps(retention),
    )
    return await get_workspace(pool, workspace_id)  # type: ignore[return-value]


async def get_override(
    pool: asyncpg.Pool, workspace_id: str, agent_id: str
) -> dict[str, Any] | None:
    """{redaction, retention} — either may be None, meaning the Workspace's."""
    row = await pool.fetchrow(
        "SELECT redaction, retention FROM agent_compliance "
        "WHERE workspace_id = $1 AND agent_id = $2",
        workspace_id,
        agent_id,
    )
    if row is None:
        return None
    return {"redaction": jsonb(row["redaction"]), "retention": jsonb(row["retention"])}


async def list_overrides(pool: asyncpg.Pool, workspace_id: str) -> dict[str, dict[str, Any]]:
    """agent_id -> override, for every Agent in the Workspace that has one."""
    rows = await pool.fetch(
        "SELECT agent_id, redaction, retention FROM agent_compliance WHERE workspace_id = $1",
        workspace_id,
    )
    return {
        r["agent_id"]: {"redaction": jsonb(r["redaction"]), "retention": jsonb(r["retention"])}
        for r in rows
    }


async def save_override(
    pool: asyncpg.Pool,
    workspace_id: str,
    agent_id: str,
    *,
    redaction: dict[str, Any] | None,
    retention: dict[str, Any] | None,
) -> None:
    """Replace the Agent's override. Both None removes it — the Agent then
    follows the Workspace entirely."""
    if redaction is None and retention is None:
        await pool.execute(
            "DELETE FROM agent_compliance WHERE workspace_id = $1 AND agent_id = $2",
            workspace_id,
            agent_id,
        )
        return
    await pool.execute(
        """
        INSERT INTO agent_compliance (agent_id, workspace_id, redaction, retention)
        VALUES ($1, $2, $3::jsonb, $4::jsonb)
        ON CONFLICT (agent_id) DO UPDATE
            SET redaction = EXCLUDED.redaction,
                retention = EXCLUDED.retention,
                updated_at = now()
        """,
        agent_id,
        workspace_id,
        None if redaction is None else json.dumps(redaction),
        None if retention is None else json.dumps(retention),
    )


async def record_projection(
    pool: asyncpg.Pool,
    workspace_id: str,
    agent_id: str,
    *,
    state: str,
    fingerprint: str | None,
    error: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO compliance_projections (workspace_id, agent_id, state, fingerprint, error)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (workspace_id, agent_id) DO UPDATE
            SET state = EXCLUDED.state, fingerprint = EXCLUDED.fingerprint,
                error = EXCLUDED.error, updated_at = now()
        """,
        workspace_id,
        agent_id,
        state,
        fingerprint,
        error,
    )


async def list_projections(pool: asyncpg.Pool, workspace_id: str) -> dict[str, dict[str, Any]]:
    """agent_id -> {state, fingerprint, error, updated_at}."""
    rows = await pool.fetch(
        "SELECT agent_id, state, fingerprint, error, updated_at "
        "FROM compliance_projections WHERE workspace_id = $1",
        workspace_id,
    )
    return {r["agent_id"]: {k: r[k] for k in ("state", "fingerprint", "error", "updated_at")} for r in rows}
