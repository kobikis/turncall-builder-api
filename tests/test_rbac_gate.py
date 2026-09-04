"""RBAC gate (#31): the require_member/require_editor dependencies on real routes.

Uses `gate_client` (conftest) — no dependency override, so the real gate runs
with auth_store mocked. Drives resolve_session / resolve_membership to exercise
401 (no/invalid session), 400 (missing workspace header), 403 (not a member /
role too low), and the scoping passthrough (workspace_id reaches the store)."""

from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID

WS_HEADER = {"X-Workspace-Id": TEST_WORKSPACE_ID}


def _login_as(mocks, role: str) -> None:
    """Make the mocked auth_store resolve a live session + the given role."""
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role


# --- 401: no / invalid session ------------------------------------------------


def test_no_session_cookie_401(gate_client, mocks):
    r = gate_client.get("/agents", headers=WS_HEADER)
    assert r.status_code == 401


def test_invalid_session_401(gate_client, mocks):
    mocks.auth_store.resolve_session.return_value = None
    gate_client.cookies.set(COOKIE, "stale")
    r = gate_client.get("/agents", headers=WS_HEADER)
    assert r.status_code == 401


# --- 400 / 403: workspace resolution ------------------------------------------


def test_missing_workspace_header_400(gate_client, mocks):
    _login_as(mocks, "admin")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/agents")  # no X-Workspace-Id
    assert r.status_code == 400


def test_not_a_member_403(gate_client, mocks):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = None  # no membership
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/agents", headers=WS_HEADER)
    assert r.status_code == 403


# --- role enforcement ---------------------------------------------------------


def test_viewer_denied_on_write(gate_client, mocks):
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.delete("/agents/a1", headers=WS_HEADER)
    assert r.status_code == 403  # require_editor rejects a viewer
    mocks.registry.get_backend.assert_not_called()


def test_viewer_allowed_on_read(gate_client, mocks):
    _login_as(mocks, "viewer")
    mocks.registry.list_backends.return_value = []
    mocks.generator.running_container_names.return_value = set()
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/agents", headers=WS_HEADER)
    assert r.status_code == 200


def test_editor_allowed_on_write(gate_client, mocks):
    _login_as(mocks, "editor")
    mocks.registry.get_backend.return_value = None  # 404 past the gate is fine
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.delete("/agents/a1", headers=WS_HEADER)
    assert r.status_code == 404  # gate passed; handler ran and found no agent


# --- scoping passthrough ------------------------------------------------------


def test_reads_are_scoped_to_active_workspace(gate_client, mocks):
    """The resolved workspace_id must reach the data layer, so a caller only ever
    sees their own Workspace's rows."""
    _login_as(mocks, "admin")
    mocks.registry.list_backends.return_value = []
    mocks.generator.running_container_names.return_value = set()
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/agents", headers=WS_HEADER)
    assert r.status_code == 200
    assert mocks.registry.list_backends.await_args.args[1] == TEST_WORKSPACE_ID


def test_cross_workspace_agent_reads_as_404(gate_client, mocks):
    """IDOR guard: an agent in another Workspace isn't visible by id — the scoped
    get_backend returns None, so the caller gets 404, not the agent."""
    _login_as(mocks, "admin")
    mocks.registry.get_backend.return_value = None  # not in caller's workspace
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/agents/someone-elses-agent", headers=WS_HEADER)
    assert r.status_code == 404
    # The lookup was scoped to the caller's workspace, not global.
    assert mocks.registry.get_backend.await_args.args[1:] == (
        "someone-elses-agent",
        TEST_WORKSPACE_ID,
    )


def test_viewer_denied_webrtc_connect(gate_client, mocks):
    """The WebRTC test-call proxy (#35) initiates a live call — a viewer (read-only)
    must be refused before any lookup or key use runs."""
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post("/agents/a1/webrtc/connect", json={"sdp": "o"}, headers=WS_HEADER)
    assert r.status_code == 403
    mocks.registry.get_backend.assert_not_called()


def test_membership_resolved_per_request(gate_client, mocks):
    """Re-resolving membership every request is what makes a kick/re-role land on
    the next call — assert the lookup happens with the request's workspace."""
    _login_as(mocks, "admin")
    mocks.registry.list_backends.return_value = []
    mocks.generator.running_container_names.return_value = set()
    gate_client.cookies.set(COOKIE, "live")
    gate_client.get("/agents", headers=WS_HEADER)
    assert mocks.auth_store.resolve_membership.await_args.args[1:] == ("u1", TEST_WORKSPACE_ID)


if __name__ == "__main__":  # ponytail: smoke without pytest
    print("run via pytest")
