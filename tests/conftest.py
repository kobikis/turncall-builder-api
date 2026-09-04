"""Endpoint-test harness.

The main.py handlers call the store/registry/phones/generator modules and the
TurnCallClient directly (no DI), and the lifespan builds a real DB pool + client.
These fixtures swap all of that for autospecced mocks (async funcs -> AsyncMock,
sync funcs -> MagicMock) and hand back a TestClient that never touches Docker,
Postgres, or TurnCall. This is the coverage the main.py split needs first.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest
from fastapi.testclient import TestClient

from app import auth_store, deps, main, runtime, store
from app.backends import generator, phones, registry, scaffold, toolgen
from app.turncall_client import TurnCallClient

# The Workspace every existing endpoint test acts within (see `client` fixture).
TEST_WORKSPACE_ID = "d0000000-0000-0000-0000-000000000001"
_ADMIN_CTX = deps.AuthContext(
    user={"id": "u-test", "email": "admin@test"},
    workspace_id=TEST_WORKSPACE_ID,
    role="admin",
)


@pytest.fixture
def mocks(monkeypatch):
    """Replace main.py's module deps + app.state with mocks. Autospec preserves
    each function's sync/async nature so awaits behave correctly."""
    m = SimpleNamespace(
        store=create_autospec(store),
        registry=create_autospec(registry),
        phones=create_autospec(phones),
        generator=create_autospec(generator),
        scaffold=create_autospec(scaffold),
        toolgen=create_autospec(toolgen),
        auth_store=create_autospec(auth_store),
        client=AsyncMock(spec=TurnCallClient),
    )
    # Patch each function ON THE SOURCE MODULE so every importer sees the mock —
    # this survives moving handlers out of main.py into router modules (which
    # import registry/phones/... themselves).
    for real_mod, mock_mod in [
        (store, m.store),
        (registry, m.registry),
        (phones, m.phones),
        (generator, m.generator),
        (scaffold, m.scaffold),
        (toolgen, m.toolgen),
    ]:
        for attr in vars(mock_mod):
            if not attr.startswith("_"):
                monkeypatch.setattr(real_mod, attr, getattr(mock_mod, attr), raising=False)
    # auth_store: mock the DB/hash functions but keep the real exception classes
    # (an `except` clause / a mock side_effect needs a real class) and the
    # SESSION_TTL timedelta (the cookie max-age calls .total_seconds() on it).
    _keep_real = {
        "DuplicateEmail",
        "InviteNotFound",
        "InviteEmailMismatch",
        "LastAdmin",
        "SESSION_TTL",
    }
    for attr in vars(m.auth_store):
        if attr.startswith("_") or attr in _keep_real:
            continue
        monkeypatch.setattr(auth_store, attr, getattr(m.auth_store, attr), raising=False)
    # sync helpers used to build URLs/slugs — give them real-ish values.
    m.registry.backend_url.side_effect = lambda port: f"http://host.docker.internal:{port}"
    m.scaffold.slugify.side_effect = lambda name: name.lower().replace(" ", "-")
    runtime.set_runtime(object(), m.client)
    yield m
    runtime.clear_runtime()


@pytest.fixture
def client(mocks):
    # Existing endpoint tests predate RBAC (#31) and don't send auth. Override the
    # gate so they run as an admin in the test Workspace; the gate itself is tested
    # separately via `gate_client` (no override). raise_server_exceptions=False so a
    # 500 is asserted as a response, not raised.
    main.app.dependency_overrides[deps.require_member] = lambda: _ADMIN_CTX
    main.app.dependency_overrides[deps.require_editor] = lambda: _ADMIN_CTX
    main.app.dependency_overrides[deps.require_admin] = lambda: _ADMIN_CTX
    try:
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        main.app.dependency_overrides.clear()


@pytest.fixture
def gate_client(mocks):
    """TestClient with the real RBAC gate in place (no dependency override), for
    testing 401/403/role enforcement. auth_store is still mocked (via `mocks`)."""
    return TestClient(main.app, raise_server_exceptions=False)


def backend_row(**over):
    """A registry backend row with sensible defaults; override per test."""
    row = {
        "agent_id": "a1",
        "project_id": "p1",
        "api_key": "tc_key",
        "slug": "agent",
        "port": 9001,
        "status": "running",
        "config": {"name": "Agent", "system_prompt": "hi"},
        "webhook_secret": "s" * 16,
        "service_dir": "/srv/agent",
    }
    row.update(over)
    return row
