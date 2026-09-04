"""running_container_names memoizes docker ps within its TTL."""

from unittest.mock import AsyncMock, patch

import pytest

from app.backends import generator


@pytest.mark.asyncio
async def test_second_call_within_ttl_is_cached():
    generator._names_cache = None
    with patch.object(generator, "_run", new=AsyncMock(return_value=(0, "a\nb"))) as run:
        first = await generator.running_container_names()
        second = await generator.running_container_names()
    assert first == {"a", "b"} == second
    assert run.await_count == 1  # docker ps forked once, not twice


@pytest.mark.asyncio
async def test_docker_unreachable_returns_none():
    generator._names_cache = None
    with patch.object(generator, "_run", new=AsyncMock(return_value=(1, "err"))):
        assert await generator.running_container_names() is None
    generator._names_cache = None


@pytest.mark.asyncio
async def test_restart_does_not_force_rebuild():
    """restart() is for env/config changes — it must NOT pass --build (which
    forces a multi-second image rebuild); compose builds only if the image is
    missing."""
    with patch.object(generator, "_run", new=AsyncMock(return_value=(0, "up"))) as run:
        ok, _ = await generator.restart("/srv/agent")
    assert ok is True
    cmd = run.await_args.args[0]
    assert cmd == ["docker", "compose", "up", "-d"]
    assert "--build" not in cmd
