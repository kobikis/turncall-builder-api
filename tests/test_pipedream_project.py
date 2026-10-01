"""A Workspace's own Pipedream project, with the deployment's as fallback."""

from __future__ import annotations

import pytest

from app import pipedream
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
BODY = {"client_id": "cid", "client_secret": "csec-very-secret", "project_id": "proj_abc"}
OWN = pipedream.Credential(client_id="cid", client_secret="s", project_id="proj_abc", environment="production")


@pytest.fixture
def status(mocks):
    mocks.pipedream_store.workspace_status.return_value = None
    mocks.pipedream_store.has_deployment_credential.return_value = True
    return mocks


def test_connect_checks_then_stores_and_never_returns_the_secret(client, status):
    status.pipedream.list_accounts.return_value = []
    r = client.put("/integrations/pipedream", json=BODY, headers=WS)
    assert r.status_code == 200, r.text
    assert "csec-very-secret" not in r.text
    status.pipedream.access_token.assert_awaited()
    kw = status.pipedream_store.save_workspace_credential.await_args.kwargs
    assert (kw["client_secret"], kw["project_id"], kw["environment"]) == ("csec-very-secret", "proj_abc", "production")


def test_a_refused_project_is_not_saved(client, status):
    status.pipedream.access_token.side_effect = pipedream.PipedreamError("Pipedream refused to issue an access token.")
    r = client.put("/integrations/pipedream", json=BODY, headers=WS)
    assert r.status_code == 400
    assert "not saved" in r.json()["detail"]
    status.pipedream_store.save_workspace_credential.assert_not_called()


def test_a_project_id_must_look_like_one(client, status):
    r = client.put("/integrations/pipedream", json={**BODY, "project_id": "abc"}, headers=WS)
    assert r.status_code == 422


def test_disconnect_is_refused_while_apps_live_in_the_project(client, status):
    status.pipedream_store.workspace_credential.return_value = OWN
    status.pipedream.list_accounts.return_value = [{"id": "apn_1", "app": "slack_v2", "name": "Slack"}]
    r = client.delete("/integrations/pipedream", headers=WS)
    assert r.status_code == 409
    assert "Slack" in r.json()["detail"]
    status.pipedream_store.delete_workspace_credential.assert_not_called()


def test_disconnect_with_no_apps_falls_back_to_the_platform(client, status):
    status.pipedream_store.workspace_credential.return_value = OWN
    status.pipedream.list_accounts.return_value = []
    r = client.delete("/integrations/pipedream", headers=WS)
    assert r.status_code == 200
    status.pipedream_store.delete_workspace_credential.assert_awaited_once()


def test_every_connected_app_path_asks_for_this_workspaces_project(client, status):
    """The lookup is per Workspace everywhere — the store decides own vs platform."""
    status.pipedream_store.get_credential.return_value = OWN
    status.pipedream.list_accounts.return_value = []
    client.get("/integrations", headers=WS)
    assert status.pipedream_store.get_credential.await_args.args[1] == TEST_WORKSPACE_ID


def test_a_backend_mints_from_its_workspaces_project(client, status):
    status.registry.get_backend_by_builder_token.return_value = backend_row(workspace_id="ws-other")
    status.pipedream_store.get_credential.return_value = OWN
    status.pipedream.access_token.return_value = pipedream.AccessToken(token="t", expires_at=1)
    status.pipedream.list_accounts.return_value = []
    r = client.post("/integrations/token", headers={"Authorization": "Bearer bt"})
    assert r.status_code == 200, r.text
    assert status.pipedream_store.get_credential.await_args.args[1] == "ws-other"
    assert r.json()["data"]["project_id"] == "proj_abc"


def test_editors_cannot_change_the_project(gate_client, status):
    status.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    status.auth_store.resolve_membership.return_value = "editor"
    gate_client.cookies.set(COOKIE, "live")
    assert gate_client.put("/integrations/pipedream", json=BODY, headers=WS).status_code == 403
