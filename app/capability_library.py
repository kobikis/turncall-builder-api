"""A Workspace's Capability library (#131, ADR-0029).

The Builder mints one TurnCall project per agent, and a TurnCall skill belongs
to one project, so two agents of a Workspace could never list the same one.
The library is one more project, per Workspace, that holds its Capabilities
(the Console's name for TurnCall skills). It is made on the Workspace's first
Capability, through the platform bootstrap like any agent's project.
"""

from __future__ import annotations

from typing import Any

from . import capability_store


async def ensure(pool: Any, client: Any, workspace_id: str) -> tuple[dict[str, Any], bool]:
    """The Workspace's library ({project_id, api_key, created_at}), made on
    first use: a project, then an admin key to it. True when made here.

    It holds no provider keys: a call's speech and model run on the agent
    project's, and a Capability's tools are webhooks."""

    async def create() -> tuple[str, str]:
        project_id = await client.create_project(f"builder-capabilities-{workspace_id[:8]}")
        return project_id, await client.create_api_key(project_id)

    return await capability_store.ensure(pool, workspace_id, create)
