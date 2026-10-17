"""Every route under an Agent answers 404 for another Workspace's Agent (#106).

An agent id is not a secret: it shows up in URLs, logs and the audit log. So
every route with an `{agent_id}` path parameter must resolve the Agent within
the caller's Workspace before reading or writing anything, and answer an Agent
of another Workspace exactly as a missing one. The GitHub link routes once
looked the agent up without the Workspace, so an editor could re-point, unlink
or push another Workspace's backend, and the audit entry landed in their own.

This walks every such route the app serves rather than a list, so a route added
later is covered the day it is added. The foreign Agent exists (an unscoped
lookup finds it), and the test fails on anything the route reads or writes
besides the scoped lookup itself: a store call, a TurnCall call, an entry.
"""

from __future__ import annotations

import re
from unittest.mock import create_autospec

import pytest

from app import github, github_store, main
from app.audit import api_routes
from app.routers.auth import current_user
from tests.conftest import TEST_WORKSPACE_ID, backend_row

FOREIGN_WORKSPACE_ID = "d0000000-0000-0000-0000-0000000000ff"
AGENT = "agent-of-another-workspace"

# A body each route accepts, so the request reaches the handler: a 422 would
# pass for the wrong reason. A route with a body and no sample here fails the
# test, which names it.
BODIES: dict[tuple[str, str], dict] = {
    ("PUT", "/agents/{agent_id}"): {"json": {"config": {"name": "x", "system_prompt": "y"}}},
    ("POST", "/agents/{agent_id}/chat"): {"json": {"message": "hi"}},
    ("POST", "/agents/{agent_id}/calls/outbound"): {
        "json": {"from_number_id": "pn1", "to_number": "+15555550100"}
    },
    ("POST", "/agents/{agent_id}/do-not-call"): {"json": {"number": "+15555550100"}},
    ("PUT", "/agents/{agent_id}/limits"): {"json": {"live_calls": 2}},
    ("POST", "/agents/{agent_id}/campaigns"): {
        "json": {"name": "c", "from_number_id": "pn1", "targets": [{"phone_number": "+15555550100"}]}
    },
    ("POST", "/agents/{agent_id}/eval-scenarios"): {
        "json": {"name": "s", "caller_prompt": "p", "criteria": ["c"]}
    },
    ("PUT", "/agents/{agent_id}/eval-scenarios/{scenario_id}"): {
        "json": {"name": "s", "caller_prompt": "p", "criteria": ["c"]}
    },
    ("POST", "/agents/{agent_id}/eval-runs"): {"json": {}},
    ("POST", "/agents/{agent_id}/eval-scenarios/from-call"): {"json": {"call_id": "c1"}},
    ("POST", "/agents/{agent_id}/eval-scenarios/from-session"): {"json": {"session_id": "s1"}},
    ("PUT", "/agents/{agent_id}/compliance"): {"json": {"redaction": None, "retention": None}},
    ("POST", "/agents/{agent_id}/knowledge/documents"): {
        "files": {"file": ("menu.txt", b"soup", "text/plain")}
    },
    ("PUT", "/agents/{agent_id}/knowledge/documents/{doc_id}/retrieval"): {"json": {"mode": "auto"}},
    ("POST", "/agents/{agent_id}/knowledge/search"): {"json": {"query": "soup"}},
    ("POST", "/agents/{agent_id}/takeaways"): {
        "json": {"name": "t", "schema": {"type": "object", "properties": {}}}
    },
    ("PUT", "/agents/{agent_id}/takeaways/{takeaway_id}"): {"json": {"name": "t"}},
    ("POST", "/agents/{agent_id}/ambience/sounds"): {
        "files": {"file": ("rain.wav", b"RIFF", "audio/wav")}
    },
    ("PUT", "/agents/{agent_id}/github"): {
        "json": {"owner": "acme", "repo": "agents", "branch": "main"}
    },
    ("PUT", "/integrations/agents/{agent_id}"): {"json": {"apps": []}},
    ("POST", "/agents/{agent_id}/webrtc/connect"): {"json": {"sdp": "v=0", "type": "offer"}},
}

# Path parameters other than the agent's, given values each route accepts.
OTHER_PARAMS = {"feed": "events", "action": "pause"}


def _agent_routes() -> list[tuple[str, str, object]]:
    routes = []
    for path, route in api_routes(main.app):
        if "{agent_id}" not in path:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            routes.append((method, path, route))
    return routes


ROUTES = _agent_routes()


def test_the_walk_finds_the_agent_routes():
    """A guard that walks nothing guards nothing."""
    paths = {(m, p) for m, p, _ in ROUTES}
    assert ("PUT", "/agents/{agent_id}/github") in paths
    assert ("POST", "/agents/{agent_id}/github/push") in paths
    assert ("POST", "/agents/{agent_id}/compliance/apply") in paths
    assert len(paths) > 40


@pytest.fixture
def foreign(mocks, monkeypatch):
    """An Agent that exists, but in another Workspace: an unscoped lookup (or
    one scoped to its own Workspace) finds it, the caller's does not."""

    def get_backend(pool, agent_id, workspace_id=None):
        if workspace_id in (None, FOREIGN_WORKSPACE_ID):
            return backend_row(agent_id=agent_id)
        return None

    mocks.registry.get_backend.side_effect = get_backend
    # The GitHub link store is not in the shared harness: mock it here, with
    # a link and a connection there to be found, so a route that skipped the
    # Workspace check would find something to act on.
    gh = create_autospec(github_store)
    gh.get_link.return_value = {
        "owner": "victim", "repo": "agents", "branch": "main", "path": "", "connection_id": "c1",
    }
    gh.get_connection.return_value = {"id": "c1", "login": "attacker"}
    gh.get_token.return_value = "ghp"
    for name in vars(gh):
        if not name.startswith("_"):
            monkeypatch.setattr(github_store, name, getattr(gh, name), raising=False)
    gh_api = create_autospec(github)
    for name in ("assert_writable", "push_backend"):
        monkeypatch.setattr(github, name, getattr(gh_api, name))
    main.app.dependency_overrides[current_user] = lambda: {"id": "u-test", "email": "admin@test"}
    return {"github_store": gh, "github": gh_api}


def _touched(mocks, extra: dict) -> list[str]:
    """Every call any mocked module received, but the scoped agent lookup."""
    calls = []
    sources = {**{k: v for k, v in vars(mocks).items() if k != "client"}, **extra}
    for name, mod in sources.items():
        for call in mod.mock_calls:
            fn = call[0]
            if (name, fn) == ("registry", "get_backend"):
                continue
            if (name, fn) in {("registry", "backend_url"), ("phones", "as_projection_target")}:
                continue  # pure helpers, no I/O
            calls.append(f"{name}.{fn}")
    calls += [f"client.{c[0]}" for c in mocks.client.mock_calls]
    return calls


@pytest.mark.parametrize(
    ("method", "path", "route"), ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES]
)
def test_another_workspaces_agent_is_not_found(client, mocks, foreign, method, path, route):
    has_body = bool(route.dependant.body_params)
    sample = BODIES.get((method, path))
    if has_body and sample is None:
        pytest.fail(f"add a valid body for {method} {path} to BODIES, so this test reaches it")

    url = re.sub(
        r"\{(\w+)\}",
        lambda m: AGENT if m.group(1) == "agent_id" else OTHER_PARAMS.get(m.group(1), "x1"),
        path,
    )
    r = client.request(method, url, headers={"X-Workspace-Id": TEST_WORKSPACE_ID}, **(sample or {}))

    assert r.status_code == 404, f"{method} {path}: {r.status_code} {r.text}"
    lookups = mocks.registry.get_backend.call_args_list
    assert lookups, f"{method} {path} never looked the agent up in the caller's Workspace"
    for call in lookups:
        scope = call.args[2] if len(call.args) > 2 else call.kwargs.get("workspace_id")
        assert scope == TEST_WORKSPACE_ID, f"{method} {path} looked the agent up unscoped: {call}"
    assert _touched(mocks, foreign) == [], f"{method} {path} acted before refusing"
