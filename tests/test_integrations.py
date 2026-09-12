"""Connecting a third-party app to a Workspace (ADR-0018, #24) on the TestClient seam.

Pipedream is the system of record for what is connected, so these tests drive the
mocked Pipedream client and assert what an admin can observe: they get sent to a
hosted consent page, coming back reports success or failure plainly, and the
Workspace they act in is the one Pipedream is told about. The client secret is
never in any of it.

Admin ops run under `client` (conftest overrides the gate to an admin). Role
denials use `gate_client`, which runs the real gate.
"""

from urllib.parse import parse_qs, urlparse

from app import pipedream
from app.routers.auth import COOKIE_NAME as COOKIE
from app.settings import load_settings
from tests.conftest import TEST_WORKSPACE_ID

WS_HEADER = {"X-Workspace-Id": TEST_WORKSPACE_ID}
SECRET = "pd_client_secret_shhh"


def _configured(mocks) -> None:
    """A deployment with Pipedream credentials in place."""
    mocks.pipedream_store.get_credential.return_value = pipedream.Credential(
        client_id="cid", client_secret=SECRET, project_id="proj", environment="development"
    )


def _account(**over):
    row = {
        "id": "apn_1",
        "app": "slack_v2",
        "name": "Slack",
        "account_name": "acme.slack.com",
        "healthy": True,
        "created_at": "2026-09-12T00:00:00Z",
    }
    row.update(over)
    return row


# --- starting the flow --------------------------------------------------------


def test_admin_starts_a_connect_flow(client, mocks):
    _configured(mocks)
    mocks.pipedream.connect_link.return_value = "https://pipedream.com/_static/connect.html?token=t&app=slack"

    r = client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)

    assert r.status_code == 200
    assert r.json()["data"]["connect_link_url"].startswith("https://pipedream.com/")
    kw = mocks.pipedream.connect_link.await_args.kwargs
    assert kw["workspace_id"] == TEST_WORKSPACE_ID  # the tenant Pipedream is told about
    assert kw["app"] == "slack_v2"


def test_connect_rejects_an_app_outside_the_catalog(client, mocks):
    _configured(mocks)
    r = client.post("/integrations/connect", json={"app": "dropbox"}, headers=WS_HEADER)
    assert r.status_code == 422
    mocks.pipedream.connect_link.assert_not_called()


def test_connect_without_pipedream_configured_says_so(client, mocks):
    mocks.pipedream_store.get_credential.return_value = None
    r = client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)
    assert r.status_code == 503
    assert "PIPEDREAM" in r.json()["detail"]


def test_connect_without_a_redirect_uri_says_which_one(client, mocks, monkeypatch):
    """Pipedream needs somewhere to send the admin back to, and it is this API —
    not PUBLIC_BASE_URL, which is the console."""
    _configured(mocks)
    from dataclasses import replace

    from app.routers import integrations

    settings = replace(load_settings(), pipedream_redirect_uri="")
    monkeypatch.setattr(integrations, "load_settings", lambda: settings)
    r = client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)
    assert r.status_code == 503
    assert "PIPEDREAM_REDIRECT_URI" in r.json()["detail"]
    mocks.pipedream.connect_link.assert_not_called()


def test_connect_sends_pipedream_this_apis_callback(client, mocks):
    _configured(mocks)
    mocks.pipedream.connect_link.return_value = "https://pipedream.com/c?token=t"
    client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)
    assert (
        mocks.pipedream.connect_link.await_args.kwargs["callback_url"]
        == load_settings().pipedream_redirect_uri
    )


def test_a_rotated_encryption_key_says_so_rather_than_500ing(client, mocks):
    """Nobody can act on an opaque 500. The stored secret being unreadable is a
    configuration failure with a fix, so it reads as one."""
    mocks.pipedream_store.get_credential.side_effect = pipedream.PipedreamError(
        "PIPEDREAM_ENCRYPTION_KEY has changed."
    )
    r = client.get("/integrations", headers=WS_HEADER)
    assert r.status_code == 503
    assert "PIPEDREAM_ENCRYPTION_KEY" in r.json()["detail"]


def test_pipedream_failure_is_reported_not_swallowed(client, mocks):
    _configured(mocks)
    mocks.pipedream.connect_link.side_effect = pipedream.PipedreamError("Pipedream is down")
    r = client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)
    assert r.status_code == 400
    assert "Pipedream is down" in r.json()["detail"]


# --- coming back --------------------------------------------------------------


def _callback(client, mocks, *, workspace_id=TEST_WORKSPACE_ID, app="slack_v2", error=None):
    state = pipedream.sign_state(workspace_id=workspace_id, app=app)
    params = {"state": state}
    if error:
        params["error"] = error
    return client.get("/integrations/callback", params=params, follow_redirects=False)


def test_successful_return_lands_on_a_connected_app(client, mocks):
    _configured(mocks)
    mocks.pipedream.list_accounts.return_value = [_account()]

    r = _callback(client, mocks)

    assert r.status_code == 303
    assert parse_qs(urlparse(r.headers["location"]).query)["connected"] == ["slack_v2"]
    # The Workspace is read off our own signed state, not off the query string.
    assert mocks.pipedream.list_accounts.await_args.kwargs["workspace_id"] == TEST_WORKSPACE_ID


def test_an_abandoned_authorization_reports_plainly(client, mocks):
    """Pipedream bounced the user to the error URI — nothing is recorded."""
    _configured(mocks)
    r = _callback(client, mocks, error="1")
    assert r.status_code == 303
    assert "error" in parse_qs(urlparse(r.headers["location"]).query)
    mocks.pipedream.list_accounts.assert_not_called()


def test_a_return_with_nothing_connected_is_a_failure_not_a_success(client, mocks):
    """Landing on the success URI with no account at Pipedream means it did not
    happen — reporting it as connected would be the half-made record."""
    _configured(mocks)
    mocks.pipedream.list_accounts.return_value = []
    r = _callback(client, mocks)
    assert r.status_code == 303
    assert "error" in parse_qs(urlparse(r.headers["location"]).query)


def test_a_forged_state_is_refused(client, mocks):
    _configured(mocks)
    r = client.get("/integrations/callback", params={"state": "forged"}, follow_redirects=False)
    assert r.status_code == 400
    mocks.pipedream.list_accounts.assert_not_called()


# --- listing and disconnecting -------------------------------------------------


def test_listing_is_scoped_to_the_callers_workspace(client, mocks):
    _configured(mocks)
    mocks.pipedream.list_accounts.return_value = [_account()]

    r = client.get("/integrations", headers=WS_HEADER)

    assert r.status_code == 200
    assert r.json()["data"][0]["app"] == "slack_v2"
    assert mocks.pipedream.list_accounts.await_args.kwargs["workspace_id"] == TEST_WORKSPACE_ID


def test_disconnect_removes_the_connected_app(client, mocks):
    _configured(mocks)
    mocks.pipedream.list_accounts.return_value = [_account(id="apn_1")]

    r = client.delete("/integrations/apn_1", headers=WS_HEADER)

    assert r.status_code == 204
    assert mocks.pipedream.delete_account.await_args.args[1] == "apn_1"


def test_cannot_disconnect_another_workspaces_connected_app(client, mocks):
    """The id is a guess unless it shows up in this Workspace's own list."""
    _configured(mocks)
    mocks.pipedream.list_accounts.return_value = [_account(id="apn_mine")]

    r = client.delete("/integrations/apn_theirs", headers=WS_HEADER)

    assert r.status_code == 404
    mocks.pipedream.delete_account.assert_not_called()


# --- the secret never leaves ---------------------------------------------------


def test_no_response_ever_carries_the_client_secret(client, mocks):
    _configured(mocks)
    mocks.pipedream.connect_link.return_value = "https://pipedream.com/c?token=t"
    mocks.pipedream.list_accounts.return_value = [_account()]

    bodies = [
        client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER).text,
        client.get("/integrations", headers=WS_HEADER).text,
        client.get("/integrations/catalog", headers=WS_HEADER).text,
    ]
    assert not any(SECRET in b for b in bodies)


# --- RBAC ----------------------------------------------------------------------


def _login_as(mocks, role: str) -> None:
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role


def test_editor_cannot_connect(gate_client, mocks):
    _configured(mocks)
    _login_as(mocks, "editor")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)
    assert r.status_code == 403
    mocks.pipedream.connect_link.assert_not_called()


def test_viewer_cannot_disconnect(gate_client, mocks):
    _configured(mocks)
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.delete("/integrations/apn_1", headers=WS_HEADER)
    assert r.status_code == 403
    mocks.pipedream.delete_account.assert_not_called()


def test_admin_may_connect(gate_client, mocks):
    _configured(mocks)
    _login_as(mocks, "admin")
    mocks.pipedream.connect_link.return_value = "https://pipedream.com/c?token=t"
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS_HEADER)
    assert r.status_code == 200


def test_a_viewer_may_still_see_what_is_connected(gate_client, mocks):
    _configured(mocks)
    _login_as(mocks, "viewer")
    mocks.pipedream.list_accounts.return_value = [_account()]
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/integrations", headers=WS_HEADER)
    assert r.status_code == 200


# --- minting for an Agent Backend (ADR-0019, #25) -----------------------------

BACKEND_TOKEN = "bt_" + "z" * 40


def _backend(mocks, **over):
    row = {"agent_id": "a1", "workspace_id": TEST_WORKSPACE_ID, "slug": "agent"}
    row.update(over)
    mocks.registry.get_backend_by_builder_token.return_value = row
    return row


def _mint(client, token=BACKEND_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/integrations/token", headers=headers)


def test_a_backend_exchanges_its_token_for_a_pipedream_one(client, mocks):
    _configured(mocks)
    _backend(mocks)
    mocks.pipedream.access_token.return_value = pipedream.AccessToken(
        token="pd_live_token", expires_at=1_800_000_000.0
    )

    r = _mint(client)

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["access_token"] == "pd_live_token"
    assert data["expires_at"] == 1_800_000_000.0
    # The tenant is the Workspace that owns the Agent — the backend never names one.
    assert data["external_user_id"] == TEST_WORKSPACE_ID
    assert mocks.registry.get_backend_by_builder_token.await_args.args[1] == BACKEND_TOKEN


def test_no_token_no_mint(client, mocks):
    _configured(mocks)
    r = _mint(client, token=None)
    assert r.status_code == 401
    mocks.pipedream.access_token.assert_not_called()


def test_an_unknown_backend_gets_nothing(client, mocks):
    _configured(mocks)
    mocks.registry.get_backend_by_builder_token.return_value = None
    r = _mint(client, token="bt_guessed")
    assert r.status_code == 401
    mocks.pipedream.access_token.assert_not_called()


def test_a_session_cookie_is_not_a_backend_credential(gate_client, mocks):
    """The caller is a container, not a person — a logged-in admin is not it."""
    _configured(mocks)
    _login_as(mocks, "admin")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post("/integrations/token", headers=WS_HEADER)
    assert r.status_code == 401


def test_the_mint_response_never_carries_the_client_secret(client, mocks):
    _configured(mocks)
    _backend(mocks)
    mocks.pipedream.access_token.return_value = pipedream.AccessToken(
        token="pd_live_token", expires_at=1.0
    )
    assert SECRET not in _mint(client).text


# --- attaching an app to an Agent (ADR-0018, #26) -----------------------------

AGENT = "a1"


def _agent(mocks, **over):
    row = {
        "agent_id": AGENT,
        "workspace_id": TEST_WORKSPACE_ID,
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
    mocks.registry.get_backend.return_value = row
    return row


def _sent_config(mocks) -> dict:
    """The agent Config that actually reached TurnCall."""
    return mocks.client.update_agent.await_args.args[1]["config"]


def _target(mocks) -> None:
    mocks.pipedream_store.mcp_target.return_value = {
        "project_id": "proj_1",
        "environment": "development",
        "external_user_id": TEST_WORKSPACE_ID,
    }


def test_an_editor_attaches_an_app_and_it_reaches_turncall(client, mocks):
    _configured(mocks)
    _target(mocks)
    _agent(mocks)

    r = client.put(
        f"/integrations/agents/{AGENT}", json={"apps": ["slack_v2"]}, headers=WS_HEADER
    )

    assert r.status_code == 200
    servers = _sent_config(mocks)["mcp_servers"]
    assert servers[0]["headers"]["x-pd-app-slug"] == "slack_v2"
    assert servers[0]["tool_filter"]
    # And the choice is remembered on the agent, so a later prompt edit keeps it.
    assert mocks.registry.update_config.await_args.args[2]["integrations"] == ["slack_v2"]


def test_detaching_everything_sends_a_config_with_no_servers(client, mocks):
    _configured(mocks)
    _target(mocks)
    _agent(mocks, config={"name": "A", "system_prompt": "hi", "integrations": ["slack_v2"]})

    r = client.put(f"/integrations/agents/{AGENT}", json={"apps": []}, headers=WS_HEADER)

    assert r.status_code == 200
    assert "mcp_servers" not in _sent_config(mocks)


def test_attached_apps_are_readable(client, mocks):
    _agent(mocks, config={"name": "A", "integrations": ["google_calendar"]})
    r = client.get(f"/integrations/agents/{AGENT}", headers=WS_HEADER)
    assert r.json()["data"]["apps"] == ["google_calendar"]


def test_an_unknown_app_is_refused_rather_than_attached(client, mocks):
    _configured(mocks)
    _agent(mocks)
    r = client.put(
        f"/integrations/agents/{AGENT}", json={"apps": ["dropbox"]}, headers=WS_HEADER
    )
    assert r.status_code == 422
    mocks.client.update_agent.assert_not_called()


def test_another_workspaces_agent_is_not_attachable(client, mocks):
    _configured(mocks)
    mocks.registry.get_backend.return_value = None  # scoped lookup finds nothing
    r = client.put(
        f"/integrations/agents/{AGENT}", json={"apps": ["slack_v2"]}, headers=WS_HEADER
    )
    assert r.status_code == 404


def test_a_viewer_cannot_attach(gate_client, mocks):
    _configured(mocks)
    _login_as(mocks, "viewer")
    _agent(mocks)
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.put(
        f"/integrations/agents/{AGENT}", json={"apps": ["slack_v2"]}, headers=WS_HEADER
    )
    assert r.status_code == 403
    mocks.client.update_agent.assert_not_called()


def test_attaching_a_takeaway_does_not_drop_the_attached_apps(client, mocks):
    """The silent-drop failure ADR-0017 opens on: a config path that rebuilds the
    TurnCall payload without knowing about integrations sends an agent that
    quietly loses its tools."""
    _configured(mocks)
    _target(mocks)
    _agent(mocks, config={"name": "A", "system_prompt": "hi", "integrations": ["slack_v2"]})
    mocks.client.create_takeaway.return_value = {"id": "tk1", "name": "outcome"}

    r = client.post(
        f"/agents/{AGENT}/takeaways",
        json={"name": "outcome", "schema": {"type": "object"}},
        headers=WS_HEADER,
    )

    assert r.status_code == 200
    assert _sent_config(mocks)["mcp_servers"][0]["headers"]["x-pd-app-slug"] == "slack_v2"
