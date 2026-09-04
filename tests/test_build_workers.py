"""Background build workers — the docker-build outcomes that Create/Update/Start
now record asynchronously (status transitions from 'generating')."""

import pytest

from app import agent_update
from app.routers import agents, sessions
from tests.conftest import backend_row


def _last_status(mocks):
    return mocks.registry.set_status.await_args.args[2]


@pytest.mark.asyncio
async def test_run_build_success_running(mocks):
    mocks.toolgen.generate_tool_bodies.return_value = {}
    mocks.scaffold.render.return_value = {"app.py": "..."}
    mocks.generator.materialize_and_run.return_value = (True, "up")
    await sessions._run_build(object(), "a1", "slug", "/srv", {"name": "A"}, 9001, "secret")
    assert _last_status(mocks) == "running"
    mocks.registry.set_tool_statuses.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_build_no_secret_degraded(mocks):
    mocks.toolgen.generate_tool_bodies.return_value = {}
    mocks.scaffold.render.return_value = {"app.py": "..."}
    mocks.generator.materialize_and_run.return_value = (True, "up")
    await sessions._run_build(object(), "a1", "slug", "/srv", {"name": "A"}, 9001, "")
    assert _last_status(mocks) == "degraded"


@pytest.mark.asyncio
async def test_run_build_compose_failure_failed(mocks):
    mocks.toolgen.generate_tool_bodies.return_value = {}
    mocks.scaffold.render.return_value = {"app.py": "..."}
    mocks.generator.materialize_and_run.return_value = (False, "compose error")
    await sessions._run_build(object(), "a1", "slug", "/srv", {"name": "A"}, 9001, "secret")
    assert _last_status(mocks) == "failed"


@pytest.mark.asyncio
async def test_run_build_exception_failed(mocks):
    mocks.toolgen.generate_tool_bodies.side_effect = RuntimeError("llm down")
    await sessions._run_build(object(), "a1", "slug", "/srv", {"name": "A"}, 9001, "secret")
    assert _last_status(mocks) == "failed"


@pytest.mark.asyncio
async def test_run_restart_success_running(mocks):
    mocks.generator.restart.return_value = (True, "ok")
    await agents._run_restart(object(), "a1", "/srv", "secret")
    assert _last_status(mocks) == "running"


@pytest.mark.asyncio
async def test_run_restart_failure_failed(mocks):
    mocks.generator.restart.return_value = (False, "boom")
    await agents._run_restart(object(), "a1", "/srv", "secret")
    assert _last_status(mocks) == "failed"


@pytest.mark.asyncio
async def test_run_regen_success_running(mocks):
    mocks.toolgen.generate_tool_bodies.return_value = {}
    mocks.scaffold.render.return_value = {"app.py": "..."}
    mocks.generator.materialize_and_run.return_value = (True, "up")
    await agent_update._run_regen(object(), backend_row(secret="s" * 16), {"name": "A"})
    assert _last_status(mocks) == "running"
