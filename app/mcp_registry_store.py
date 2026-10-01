"""Workspace MCP servers: registered once, opted into per agent (ADR-0023, #59).

Headers carry bearer tokens, so they are encrypted with the Integrations key and
never read back to a caller — a read names the header keys, not their values.
An agent opts in by listing ids under `workspace_mcp_servers` in its own config;
nothing is inherited, because TurnCall connects every configured server under
one discovery budget and one slow server would disarm every agent.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .integration_store import BOX

_COLS = "id::text AS id, name, transport, url, headers_encrypted, created_at, updated_at"


def _public(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    ciphertext = d.pop("headers_encrypted")
    d["header_names"] = sorted(json.loads(BOX.decrypt(ciphertext))) if ciphertext else []
    return d


def _encrypt(headers: dict[str, str] | None) -> str | None:
    return BOX.encrypt(json.dumps(headers)) if headers else None


async def list_servers(pool: asyncpg.Pool, workspace_id: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM workspace_mcp_servers WHERE workspace_id = $1 ORDER BY name",
        workspace_id,
    )
    return [_public(r) for r in rows]


async def get_server(pool: asyncpg.Pool, workspace_id: str, server_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        f"SELECT {_COLS} FROM workspace_mcp_servers WHERE workspace_id = $1 AND id = $2::uuid",
        workspace_id,
        server_id,
    )
    return _public(row) if row else None


async def create_server(
    pool: asyncpg.Pool,
    workspace_id: str,
    *,
    name: str,
    transport: str,
    url: str,
    headers: dict[str, str] | None,
) -> dict[str, Any]:
    row = await pool.fetchrow(
        f"""
        INSERT INTO workspace_mcp_servers (workspace_id, name, transport, url, headers_encrypted)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_COLS}
        """,
        workspace_id, name, transport, url, _encrypt(headers),
    )
    return _public(row)


async def update_server(
    pool: asyncpg.Pool,
    workspace_id: str,
    server_id: str,
    *,
    name: str,
    transport: str,
    url: str,
    headers: dict[str, str] | None,
) -> dict[str, Any] | None:
    """`headers=None` keeps the stored ones — the field is write-only, so an edit
    that does not re-enter them must not erase them. `{}` clears them."""
    row = await pool.fetchrow(
        f"""
        UPDATE workspace_mcp_servers
           SET name = $3, transport = $4, url = $5,
               headers_encrypted = CASE WHEN $6::boolean THEN $7 ELSE headers_encrypted END,
               updated_at = now()
         WHERE workspace_id = $1 AND id = $2::uuid
        RETURNING {_COLS}
        """,
        workspace_id, server_id, name, transport, url, headers is not None, _encrypt(headers),
    )
    return _public(row) if row else None


async def delete_server(pool: asyncpg.Pool, workspace_id: str, server_id: str) -> bool:
    result = await pool.execute(
        "DELETE FROM workspace_mcp_servers WHERE workspace_id = $1 AND id = $2::uuid",
        workspace_id,
        server_id,
    )
    return result.endswith(" 1")


async def resolve(
    pool: asyncpg.Pool, workspace_id: str | None, server_ids: list[str] | None
) -> list[dict[str, Any]]:
    """The `mcp_servers` entries an agent opted into, headers decrypted, in the
    order the agent listed them. Unknown ids (a removed server) drop out."""
    if not workspace_id or not server_ids:
        return []
    rows = await pool.fetch(
        f"SELECT {_COLS} FROM workspace_mcp_servers "
        "WHERE workspace_id = $1 AND id::text = ANY($2::text[])",
        workspace_id,
        list(server_ids),
    )
    by_id = {r["id"]: r for r in rows}
    servers = []
    for server_id in server_ids:
        row = by_id.get(server_id)
        if row is None:
            continue
        entry: dict[str, Any] = {"name": row["name"], "transport": row["transport"], "url": row["url"]}
        if row["headers_encrypted"]:
            entry["headers"] = json.loads(BOX.decrypt(row["headers_encrypted"]))
        servers.append(entry)
    return servers
