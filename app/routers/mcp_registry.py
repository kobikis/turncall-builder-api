"""Workspace MCP servers: register once, opt agents in (ADR-0023, #59).

Registering, editing and removing are admin acts on the Workspace; opting in is
an agent edit (`workspace_mcp_servers` in its config) and goes through the agent
form like any other. Headers are write-only: a read names them, never shows them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import agent_update, integration_store, mcp_registry_store, runtime
from ..backends import registry
from ..deps import AuthContext, require_admin, require_member

router = APIRouter(prefix="/integrations/mcp-servers")


class ServerBody(BaseModel):
    # TurnCall's own MCPServerSchema name pattern, so a registered name never
    # fails at the agent push instead of here.
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z_][a-z0-9_-]*$")
    transport: str = "http"
    url: str = Field(min_length=1, max_length=2048)
    # None on an edit keeps the stored headers (write-only field); {} clears them.
    headers: dict[str, str] | None = None

    @field_validator("transport")
    @classmethod
    def _remote_only(cls, v: str) -> str:
        if v == "stdio":
            raise ValueError(
                "stdio is not accepted: it would run a local process on the TurnCall "
                "host (ADR-0017). Register an http or sse server."
            )
        if v not in ("http", "sse"):
            raise ValueError("transport must be http or sse")
        return v

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("url must be an http(s) URL")
        return v


def _opted_in(backend: dict[str, Any], server_id: str) -> bool:
    return server_id in ((backend.get("config") or {}).get("workspace_mcp_servers") or [])


async def _repush(ctx: AuthContext, server_id: str, *, remove: bool = False) -> list[str]:
    """Push every opted-in agent's config again so an edit (or removal) reaches it.
    Returns the agent ids touched."""
    pool, client = runtime.get_pool(), runtime.get_client()
    touched = []
    for backend in await registry.list_backends(pool, ctx.workspace_id):
        if not _opted_in(backend, server_id):
            continue
        config = dict(backend["config"])
        if remove:
            config["workspace_mcp_servers"] = [
                s for s in config["workspace_mcp_servers"] if s != server_id
            ]
        await agent_update.apply_update(pool, client, backend, config)
        touched.append(backend["agent_id"])
    return touched


def _store_error(exc: integration_store.IntegrationError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("")
async def list_servers(ctx: AuthContext = Depends(require_member)) -> dict[str, Any]:
    pool = runtime.get_pool()
    try:
        servers = await mcp_registry_store.list_servers(pool, ctx.workspace_id)
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    backends = await registry.list_backends(pool, ctx.workspace_id)
    for server in servers:
        server["agents"] = [b["agent_id"] for b in backends if _opted_in(b, server["id"])]
    return {"success": True, "data": servers}


@router.post("", status_code=201)
async def register(body: ServerBody, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    try:
        server = await mcp_registry_store.create_server(
            runtime.get_pool(), ctx.workspace_id,
            name=body.name, transport=body.transport, url=body.url, headers=body.headers,
        )
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    except Exception as exc:  # asyncpg UniqueViolationError
        if type(exc).__name__ == "UniqueViolationError":
            raise HTTPException(status_code=409, detail=f"a server named {body.name!r} is already registered") from exc
        raise
    return {"success": True, "data": server}


@router.put("/{server_id}")
async def edit(server_id: str, body: ServerBody, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """One edit reaches every agent opted in — rotating a token is one action."""
    try:
        server = await mcp_registry_store.update_server(
            runtime.get_pool(), ctx.workspace_id, server_id,
            name=body.name, transport=body.transport, url=body.url, headers=body.headers,
        )
    except integration_store.IntegrationError as exc:
        raise _store_error(exc) from exc
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    server["agents"] = await _repush(ctx, server_id)
    return {"success": True, "data": server}


@router.delete("/{server_id}")
async def remove(server_id: str, ctx: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    """Taken out of every opted-in agent first, then deleted."""
    pool = runtime.get_pool()
    if await mcp_registry_store.get_server(pool, ctx.workspace_id, server_id) is None:
        raise HTTPException(status_code=404, detail="server not found")
    touched = await _repush(ctx, server_id, remove=True)
    await mcp_registry_store.delete_server(pool, ctx.workspace_id, server_id)
    return {"success": True, "data": {"deleted": server_id, "agents": touched}}
