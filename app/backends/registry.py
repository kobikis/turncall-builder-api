"""Agent Backend registry: agent_id -> {port, service_dir, status} (ADR-0004).

Sequential host ports from BASE_PORT; the builder reads this to route events,
wire tool webhook_urls, and skip duplicate generation.
"""

from __future__ import annotations

import json
import os
from typing import Any

import asyncpg

from .scaffold import slugify

BASE_PORT = 9001
# Host directory (bind-mounted into the builder) where generated repos are written.
GENERATED_DIR = os.environ.get("GENERATED_DIR", "/generated")


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for col in ("config", "tool_statuses"):
        if col in d:
            d[col] = json.loads(d[col]) if d[col] else None
    return d


_COLS = (
    "agent_id, project_id, api_key, slug, port, service_dir, status, config, "
    "webhook_secret, tool_statuses"
)


async def get_backend(
    pool: asyncpg.Pool, agent_id: str, workspace_id: str | None = None
) -> dict[str, Any] | None:
    """A backend by id. Pass workspace_id to scope to one Workspace (request path);
    omit it for system paths that span all Workspaces (startup reconcile)."""
    q = f"SELECT {_COLS} FROM agent_backends WHERE agent_id = $1"
    args: list[Any] = [agent_id]
    if workspace_id is not None:
        q += " AND workspace_id = $2"
        args.append(workspace_id)
    return _row(await pool.fetchrow(q, *args))


async def list_backends(
    pool: asyncpg.Pool, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    q = f"SELECT {_COLS} FROM agent_backends WHERE NOT deleted"
    args: list[Any] = []
    if workspace_id is not None:
        q += " AND workspace_id = $1"
        args.append(workspace_id)
    rows = await pool.fetch(q + " ORDER BY created_at", *args)
    return [_row(r) for r in rows]  # type: ignore[misc]


async def update_config(pool: asyncpg.Pool, agent_id: str, config: dict) -> None:
    await pool.execute(
        "UPDATE agent_backends SET config = $2::jsonb WHERE agent_id = $1",
        agent_id,
        json.dumps(config),
    )


async def next_port(pool: asyncpg.Pool) -> int:
    # ponytail: max+1, single-tenant dev — no concurrent-create contention worth locking.
    top = await pool.fetchval("SELECT max(port) FROM agent_backends")
    return (top + 1) if top else BASE_PORT


async def record_backend(
    pool: asyncpg.Pool,
    agent_id: str,
    project_id: str,
    api_key: str,
    agent_name: str,
    port: int,
    config: dict | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Insert the registry row once the agent exists. Idempotent. Stores the
    agent's project key (for a WebRTC client) and trimmed config (for edit diffs).
    workspace_id scopes the agent to its owning Workspace (#31)."""
    slug = f"{slugify(agent_name)}-{agent_id[:8]}"
    service_dir = f"{GENERATED_DIR}/turncall-agent-{slug}"
    await pool.execute(
        """INSERT INTO agent_backends
             (agent_id, project_id, api_key, slug, port, service_dir, status, config,
              workspace_id)
           VALUES ($1, $2, $3, $4, $5, $6, 'generating', $7::jsonb, $8)
           ON CONFLICT (agent_id) DO NOTHING""",
        agent_id,
        project_id,
        api_key,
        slug,
        port,
        service_dir,
        json.dumps(config or {}),
        workspace_id,
    )
    return await get_backend(pool, agent_id)  # type: ignore[return-value]


async def set_status(pool: asyncpg.Pool, agent_id: str, status: str) -> None:
    await pool.execute(
        "UPDATE agent_backends SET status = $2 WHERE agent_id = $1", agent_id, status
    )


async def set_webhook_secret(pool: asyncpg.Pool, agent_id: str, secret: str) -> None:
    await pool.execute(
        "UPDATE agent_backends SET webhook_secret = $2 WHERE agent_id = $1",
        agent_id,
        secret,
    )


async def set_tool_statuses(
    pool: asyncpg.Pool, agent_id: str, statuses: dict[str, str]
) -> None:
    """Record per-tool handler provenance: {name: 'generated' | 'stub'}."""
    await pool.execute(
        "UPDATE agent_backends SET tool_statuses = $2::jsonb WHERE agent_id = $1",
        agent_id,
        json.dumps(statuses),
    )


async def mark_deleted(pool: asyncpg.Pool, agent_id: str) -> None:
    """Teardown: hide from lists; the row (and its port) stays reserved."""
    await pool.execute(
        "UPDATE agent_backends SET deleted = true, status = 'deleted' WHERE agent_id = $1",
        agent_id,
    )


def backend_url(port: int) -> str:
    """URL other containers (TurnCall, the builder) use to reach the backend."""
    return f"http://host.docker.internal:{port}"
