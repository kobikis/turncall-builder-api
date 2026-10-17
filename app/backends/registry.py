"""Agent Backend registry: agent_id -> {port, service_dir, status} (ADR-0004).

Sequential host ports from BASE_PORT; the builder reads this to route events,
wire tool webhook_urls, and skip duplicate generation.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

import asyncpg

from . import kube
from .scaffold import slugify

BASE_PORT = 9001
# Host directory (bind-mounted into the builder) where generated repos are written.
GENERATED_DIR = os.environ.get("GENERATED_DIR", "/generated")


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for col in ("config", "tool_statuses", "limits"):
        if col in d:
            d[col] = json.loads(d[col]) if d[col] else None
    # uuid -> str so callers can compare and serialize it without knowing the
    # column type.
    if d.get("workspace_id") is not None:
        d["workspace_id"] = str(d["workspace_id"])
    return d


_COLS = (
    "agent_id, project_id, api_key, slug, port, service_dir, status, config, "
    "webhook_secret, tool_statuses, workspace_id, builder_token, call_init_secret, limits"
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


async def get_backend_by_builder_token(
    pool: asyncpg.Pool, token: str
) -> dict[str, Any] | None:
    """Which Agent Backend holds this token (ADR-0019). An empty token matches
    nothing — a backend generated before the column existed must not be able to
    authenticate by presenting nothing."""
    if not token:
        return None
    return _row(
        await pool.fetchrow(
            f"SELECT {_COLS} FROM agent_backends WHERE builder_token = $1 AND NOT deleted",
            token,
        )
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
    workspace_id scopes the agent to its owning Workspace (#31); builder_token is
    how the generated backend later proves which Agent it is (ADR-0019)."""
    slug = f"{slugify(agent_name)}-{agent_id[:8]}"
    service_dir = f"{GENERATED_DIR}/turncall-agent-{slug}"
    await pool.execute(
        """INSERT INTO agent_backends
             (agent_id, project_id, api_key, slug, port, service_dir, status, config,
              workspace_id, builder_token, call_init_secret)
           VALUES ($1, $2, $3, $4, $5, $6, 'generating', $7::jsonb, $8, $9, $10)
           ON CONFLICT (agent_id) DO NOTHING""",
        agent_id,
        project_id,
        api_key,
        slug,
        port,
        service_dir,
        json.dumps(config or {}),
        workspace_id,
        # ON CONFLICT DO NOTHING, so a re-registration keeps the token the
        # backend's .env already carries.
        secrets.token_urlsafe(32),
        # Minted here rather than waiting for a phone bind: managed call-init is
        # the only path a credential reaches a call (ADR-0020), and a browser
        # test call has no number to mint one. A later bind replaces it with
        # TurnCall's own and updates this row, so the two never drift.
        secrets.token_urlsafe(32),
    )
    return await get_backend(pool, agent_id)  # type: ignore[return-value]


async def set_call_init_secret(pool: asyncpg.Pool, agent_id: str, secret: str) -> None:
    """Record the secret the backend's /call-init now verifies against.

    A phone bind mints its own in TurnCall and overwrites the backend's .env; the
    builder has to follow, or the WebRTC path keeps signing with a secret the
    backend has already forgotten.
    """
    await pool.execute(
        "UPDATE agent_backends SET call_init_secret = $2 WHERE agent_id = $1",
        agent_id,
        secret,
    )


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


async def set_limits(pool: asyncpg.Pool, agent_id: str, limits: dict[str, Any] | None) -> None:
    """Keep the builder's copy of the Agent's project limits (migration 0022):
    what TurnCall last reported, so a change is audited with what it replaced."""
    await pool.execute(
        "UPDATE agent_backends SET limits = $2::jsonb WHERE agent_id = $1",
        agent_id,
        None if limits is None else json.dumps(limits),
    )


async def mark_deleted(pool: asyncpg.Pool, agent_id: str) -> None:
    """Teardown: hide from lists; the row (and its port) stays reserved."""
    await pool.execute(
        "UPDATE agent_backends SET deleted = true, status = 'deleted' WHERE agent_id = $1",
        agent_id,
    )


# --- the TurnCall projects the builder holds keys for ---------------------------
#
# One per Agent, deleted ones included (an audit entry outlives its agent), and
# the webhook projects (#65). An audit entry's link to the engine (ADR-0028)
# names the project each call was made in, and reading that project's engine
# log needs its key back.


async def projects_for_keys(db: Any, api_keys: list[str]) -> dict[str, str]:
    """The project each of these keys belongs to; a key held nowhere is left out."""
    keys = sorted({k for k in api_keys if k})
    if not keys:
        return {}
    rows = await db.fetch(
        """SELECT project_id, api_key FROM agent_backends WHERE api_key = ANY($1::text[])
           UNION ALL
           SELECT project_id, api_key FROM webhook_project WHERE api_key = ANY($1::text[])""",
        keys,
    )
    return {r["api_key"]: r["project_id"] for r in rows if r["project_id"]}


async def key_for_project(db: Any, project_id: str, workspace_id: str) -> str | None:
    """The key of a project this Workspace holds: one of its Agents' (deleted or
    not), its webhook project, or the shared one numbers bound before #65 use."""
    return await db.fetchval(
        """SELECT api_key FROM agent_backends
            WHERE project_id = $1 AND workspace_id = $2
           UNION ALL
           SELECT api_key FROM webhook_project
            WHERE project_id = $1 AND (workspace_id = $2 OR workspace_id IS NULL)
           LIMIT 1""",
        project_id,
        workspace_id,
    )


def backend_url(port: int) -> str:
    """URL other containers (TurnCall, the builder) use to reach the backend."""
    if kube.ENABLED:
        return kube.service_url(port)
    return f"http://host.docker.internal:{port}"
