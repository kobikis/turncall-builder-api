"""Apply a config change to an already-generated agent.

Pushes the new config to TurnCall and regenerates the backend iff its custom
tools changed (a prompt/voice tweak needs no rebuild). Shared by the form edit
(PUT /agents/{id}) and the chat edit (POST /sessions/{id}/apply) so both take
exactly the same path.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ._helpers import _normalize_config
from .backends import generator, registry, scaffold, toolgen
from .backends.diff import tools_changed
from .mapper import external_tool_names, to_create_agent_request
from .tasks import spawn

logger = logging.getLogger("turncall_builder")


async def apply_update(
    pool: Any, client: Any, backend: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Push `config` (trimmed shape) to the live agent + regenerate iff tools
    changed. Returns {"backend_regenerated": bool}."""
    port = backend["port"]
    new_config = _normalize_config(config, port)
    payload = to_create_agent_request(
        new_config,
        registry.backend_url(port),
        tools_secret=backend.get("webhook_secret") or None,
    )
    await client.update_agent(backend["agent_id"], payload, backend["api_key"])

    # Both sides normalized with the same port, so a legacy config (nulls/missing
    # fields) vs its filled form never reads as a change. Regenerate on tool
    # changes — and whenever the backend never came up (retry a failed build).
    old_config = _normalize_config(backend["config"], port) if backend.get("config") else None
    regenerated = tools_changed(old_config, new_config) or backend["status"] == "failed"
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
            agent_id=backend["agent_id"],
            webhook_secret=backend.get("webhook_secret") or "",
        )
        if os.path.exists(os.path.join(backend["service_dir"], ".env")):
            # Never clobber a live .env — a bind may have added CALL_INIT_SECRET.
            files.pop(".env", None)
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
