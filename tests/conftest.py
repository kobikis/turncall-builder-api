"""Endpoint-test harness.

The main.py handlers call the store/registry/phones/generator modules and the
TurnCallClient directly (no DI), and the lifespan builds a real DB pool + client.
These fixtures swap all of that for autospecced mocks (async funcs -> AsyncMock,
sync funcs -> MagicMock) and hand back a TestClient that never touches Docker,
Postgres, or TurnCall. This is the coverage the main.py split needs first.
"""

from __future__ import annotations

import inspect
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Settings are process-cached and read at app import, so deployment config the
# tests depend on has to be in the environment before the imports below. The
# encryption key also signs the connect state, which the callback tests round
# trip for real — set here rather than in one test module, so any module can run
# on its own.
os.environ.setdefault("PIPEDREAM_REDIRECT_URI", "https://api.test/integrations/callback")
os.environ.setdefault("PIPEDREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("INTEGRATIONS_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app import (
    audit_store,
    auth_store,
    capability_store,
    compliance_store,
    db,
    deps,
    integration_store,
    main,
    mcp_registry_store,
    pipedream,
    pipedream_store,
    runtime,
    store,
    twilio_store,
    workspace_skills,
)
from app.backends import generator, phones, registry, scaffold, toolgen

from app.turncall_client import TurnCallClient

# Captured before the fixture patches the module: the mock delegates to the real
# function, so the tests exercise it rather than a copy of it.
_REAL_AS_PROJECTION_TARGET = phones.as_projection_target

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
        workspace_skills=create_autospec(workspace_skills),
        pipedream=create_autospec(pipedream),
        pipedream_store=create_autospec(pipedream_store),
        integration_store=create_autospec(integration_store),
        mcp_registry_store=create_autospec(mcp_registry_store),
        twilio_store=create_autospec(twilio_store),
        compliance_store=create_autospec(compliance_store),
        audit_store=create_autospec(audit_store),
        capability_store=create_autospec(capability_store),
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
        (workspace_skills, m.workspace_skills),
        (pipedream_store, m.pipedream_store),
        (integration_store, m.integration_store),
        (mcp_registry_store, m.mcp_registry_store),
        (twilio_store, m.twilio_store),
        (compliance_store, m.compliance_store),
        (audit_store, m.audit_store),
        (capability_store, m.capability_store),
    ]:
        for attr in vars(mock_mod):
            if not attr.startswith("_"):
                monkeypatch.setattr(real_mod, attr, getattr(mock_mod, attr), raising=False)
    # pipedream: every call that talks to Pipedream is async and every pure helper
    # is sync, so mocking the coroutines is exactly the network boundary — and a
    # function added later is covered without editing this list. The crypto, the
    # signed connect state and the catalog stay real: the state is what the callback
    # trusts, so a test that forged it against a mock would prove nothing.
    for attr, value in vars(pipedream).items():
        if not attr.startswith("_") and inspect.iscoroutinefunction(value):
            monkeypatch.setattr(pipedream, attr, getattr(m.pipedream, attr))
    # No Pipedream credential unless a test says otherwise — integrations off.
    m.pipedream_store.get_credential.return_value = None
    # No Connected-app flow under way unless a test starts one (#101).
    m.pipedream_store.take_connect_flow.return_value = None
    # No Integrations or registered servers unless a test says otherwise.
    m.integration_store.list_for_workspace.return_value = []
    m.integration_store.list_projections.return_value = []
    m.integration_store.api_keys.return_value = {}
    m.mcp_registry_store.resolve.return_value = []
    m.mcp_registry_store.list_servers.return_value = []
    # Numbers run on the platform's Twilio account unless a test says otherwise.
    m.phones.twilio_account.return_value = None
    m.phones.on_workspace_twilio.return_value = []
    # No Workspace has bound a webhook number unless a test says so (#65).
    m.phones.workspace_webhook_project.return_value = None
    m.phones.key_for_project.return_value = None
    m.phones.as_projection_target.side_effect = _REAL_AS_PROJECTION_TARGET
    m.phones.shared_webhook_numbers.return_value = 0
    m.twilio_store.get.return_value = None
    m.twilio_store.credentials.return_value = None
    # The audit log: every entry a request appends lands in m.audit_store.insert
    # (await_args_list), and a builder-side change's transaction is the pool
    # itself — there is no database under these tests.
    m.audit_store.list_entries.return_value = ([], 0)
    m.audit_store.get_entry.return_value = None
    # An engine link's project, from the key a TurnCall call was made with (#102):
    # none known unless a test says so.
    m.registry.projects_for_keys.return_value = {}
    m.registry.key_for_project.return_value = None

    @asynccontextmanager
    async def _transaction(pool):
        yield pool

    monkeypatch.setattr(db, "transaction", _transaction)
    # What an entry's "before" is read from: nothing there unless a test says so
    # (an unconfigured async mock would hand back a mock, not a row).
    m.workspace_skills.get_skill.return_value = None
    m.auth_store.user_email.return_value = None
    m.mcp_registry_store.get_server.return_value = None
    m.pipedream_store.workspace_status.return_value = None
    # No compliance settings, overrides or projections unless a test says so.
    m.compliance_store.get_workspace.return_value = None
    m.compliance_store.get_override.return_value = None
    m.compliance_store.list_overrides.return_value = {}
    m.compliance_store.list_projections.return_value = {}
    m.compliance_store.get_projection.return_value = None
    # No Capability library until a test makes one (#131).
    m.capability_store.get.return_value = None
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
    # No workspace skills unless a test says otherwise — materialize returns the
    # directory to mount, and None means "this workspace has none".
    m.workspace_skills.materialize.return_value = None
    runtime.set_runtime(object(), m.client)
    yield m
    runtime.clear_runtime()


def audit_entries(mocks) -> list[dict]:
    """Every audit entry the request(s) appended, in order."""
    return [c.args[1] for c in mocks.audit_store.insert.await_args_list]


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


class FakeStream:
    """Stand-in for `builder.stream`, which is an async generator.

    Records the call and yields what it was given: ComposeResult / Progress
    values to emit, or an exception to raise mid-stream.
    """

    def __init__(self, *events):
        self.events = events
        self.args: tuple = ()
        self.kwargs: dict = {}

    def __call__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        return self._events()

    async def _events(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


@pytest.fixture
def telnyx_clock(monkeypatch):
    """A fake clock for telnyx_api's deadlines (#117): sleeping advances it at
    once, and `advance` moves it as a slow request would. `waits` lists every
    sleep asked for."""
    from app import telnyx_api

    clock = SimpleNamespace(now=1000.0, waits=[])

    async def sleep(seconds):
        clock.waits.append(seconds)
        clock.now += seconds

    def advance(seconds):
        clock.now += seconds

    clock.advance = advance
    monkeypatch.setattr(telnyx_api, "_clock", lambda: clock.now)
    monkeypatch.setattr(telnyx_api, "_sleep", sleep)
    return clock
