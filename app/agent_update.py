"""Apply a config change to an already-generated agent.

Pushes the new config to TurnCall and regenerates the backend iff its custom
tools changed (a prompt/voice tweak needs no rebuild). Shared by the form edit
(PUT /agents/{id}) and the chat edit (POST /sessions/{id}/apply) so both take
exactly the same path.
"""

from __future__ import annotations

import logging
from typing import Any

from . import mcp_registry_store, pipedream_store
from ._helpers import _normalize_config
from .backends import generator, registry, scaffold, toolgen
from .backends.diff import automations_changed, tools_changed
from .mapper import external_tool_names, to_create_agent_request
from .tasks import spawn

logger = logging.getLogger("turncall_builder")


async def build_payload(
    pool: Any, backend: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """The TurnCall agent body for `config`, with everything resolved per
    Workspace: the Connected-apps target and the registered MCP servers the agent
    opted into. Every path that pushes a config goes through here, so none of
    them can drop either."""
    workspace_id = backend.get("workspace_id")
    return to_create_agent_request(
        config,
        registry.backend_url(backend["port"]),
        tools_secret=backend.get("webhook_secret") or None,
        pipedream_target=await pipedream_store.mcp_target(pool, workspace_id),
        registered_mcp_servers=await mcp_registry_store.resolve(
            pool, workspace_id, config.get("workspace_mcp_servers")
        ),
    )


async def apply_update(
    pool: Any, client: Any, backend: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Push `config` (trimmed shape) to the live agent + regenerate iff tools
    changed. Returns {"backend_regenerated": bool}."""
    port = backend["port"]
    new_config = _normalize_config(config, port)
    payload = await build_payload(pool, backend, new_config)
    await client.update_agent(backend["agent_id"], payload, backend["api_key"])

    # Both sides normalized with the same port, so a legacy config (nulls/missing
    # fields) vs its filled form never reads as a change. Regenerate on tool
    # changes, on Automation changes (ADR-0021 — they are generated code too),
    # and whenever the backend never came up (retry a failed build).
    old_config = _normalize_config(backend["config"], port) if backend.get("config") else None
    regenerated = (
        tools_changed(old_config, new_config)
        or automations_changed(old_config, new_config)
        or backend["status"] == "failed"
    )
    await registry.update_config(pool, backend["agent_id"], new_config)
    if regenerated:
        await _regenerate_backend(pool, backend, new_config)
    return {"backend_regenerated": regenerated}


async def _regenerate_backend(pool: Any, backend: dict, config: dict) -> None:
    """Mark 'generating' and rebuild in the background — the docker build blocks
    for tens of seconds, so the caller returns immediately and the console polls."""
    await registry.set_status(pool, backend["agent_id"], "generating")
    spawn(_run_regen(pool, backend, config))


async def _run_regen(pool: Any, backend: dict, config: dict) -> None:
    """Background worker: re-render app.py for changed tools and rebuild. User
    edits are git-committed first (generator); .env (secrets) is never rewritten."""
    tools = config.get("custom_tools") or []
    external = external_tool_names(config, registry.backend_url(backend["port"]))
    internal_tools = [t for t in tools if t["name"] not in external]
    try:
        bodies = await toolgen.generate_tool_bodies(internal_tools)
        files = scaffold.render(
            slug=backend["slug"],
            port=backend["port"],
            tools=internal_tools,
            tool_bodies=bodies,
            integrations=config.get("integrations"),
            # Carried through regeneration so a backend older than the column can
            # acquire one — merge_env only fills what the render actually gives.
            call_init_secret=backend.get("call_init_secret") or "",
            agent_id=backend["agent_id"],
            webhook_secret=backend.get("webhook_secret") or "",
            builder_token=backend.get("builder_token") or "",
        )
        # A live .env keeps every value it has — a bind may have added
        # CALL_INIT_SECRET — but gains any key the scaffold has grown since, so a
        # backend generated before a credential existed can still acquire it.
        gained = generator.merge_env(backend["service_dir"], files.pop(".env", ""))
        if gained:
            logger.info(
                "added %s to %s's .env", ", ".join(gained), backend["agent_id"]
            )
        ok, _ = await generator.materialize_and_run(backend["service_dir"], files)
        secret = backend.get("webhook_secret")
        status = "degraded" if ok and not secret else "running" if ok else "failed"
        await registry.set_status(pool, backend["agent_id"], status)
        await registry.set_tool_statuses(
            pool,
            backend["agent_id"],
            {
                t["name"]: (
                    "external"
                    if t["name"] in external
                    else "generated"
                    if t["name"] in bodies
                    else "stub"
                )
                for t in tools
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("backend regeneration failed")
        await registry.set_status(pool, backend["agent_id"], "failed")
