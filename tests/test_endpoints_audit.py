"""The audit log of people on the TestClient seam (ADR-0028).

Reading it: `GET /audit` is admin-only, Workspace-scoped and passes its filters
to the store. Writing it: what each kind of change appends, observed as calls on
the mocked `audit_store.insert` — who, under which role, about what, and never a
secret's value. The SQL is covered by test_audit_store_db.py; that every audited
route records at all is held by test_audit_wiring.py.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import github, github_store, main, pipedream
from app.routers.auth import COOKIE_NAME as COOKIE
from app.routers.auth import current_user
from tests.conftest import TEST_WORKSPACE_ID, audit_entries, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
MEMBER = "11111111-1111-1111-1111-111111111111"


def _only_entry(mocks) -> dict:
    entries = audit_entries(mocks)
    assert len(entries) == 1, entries
    return entries[0]


# --- reading -------------------------------------------------------------------


def test_admin_reads_newest_first_with_filters(client, mocks):
    entry = {"id": "e1", "action": "updated"}
    mocks.audit_store.list_entries.return_value = ([entry], 7)

    r = client.get(
        "/audit",
        params={
            "member": MEMBER, "agent": "a1",
            "since": "2026-10-01T00:00:00", "until": "2026-10-02T00:00:00Z",
            "page": 2, "limit": 5,
        },
        headers=WS,
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "data": [entry], "total": 7, "page": 2, "limit": 5}
    args = mocks.audit_store.list_entries.await_args
    assert args.args[1] == TEST_WORKSPACE_ID
    kw = args.kwargs
    assert kw["user_id"] == MEMBER and kw["agent_id"] == "a1"
    # A time without a zone is UTC.
    assert kw["since"] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert kw["until"] == datetime(2026, 10, 2, tzinfo=timezone.utc)
    assert (kw["page"], kw["limit"]) == (2, 5)


def test_defaults_are_the_first_page_of_fifty(client, mocks):
    r = client.get("/audit", headers=WS)
    assert r.status_code == 200
    assert r.json() == {"success": True, "data": [], "total": 0, "page": 1, "limit": 50}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0}, {"limit": 201}, {"page": 0}, {"since": "yesterday"},
        {"since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
    ],
)
def test_bad_filters_are_refused(client, mocks, params):
    assert client.get("/audit", params=params, headers=WS).status_code == 422
    mocks.audit_store.list_entries.assert_not_called()


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_an_admin_reads_it(gate_client, mocks, role):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/audit", headers=WS)
    assert r.status_code == 403
    mocks.audit_store.list_entries.assert_not_called()


def test_reading_needs_a_login(gate_client, mocks):
    assert gate_client.get("/audit", headers=WS).status_code == 401


# --- agents ----------------------------------------------------------------------


def test_an_agent_edit_records_what_changed_and_masks_secrets(client, mocks):
    old = {
        "name": "Front desk",
        "system_prompt": "Be kind.",
        "mcp_servers": [{"name": "crm", "url": "https://crm/mcp", "headers": {"Authorization": "Bearer OLD"}}],
    }
    new = {**old, "system_prompt": "Be brief.", "mcp_servers": [
        {"name": "crm", "url": "https://crm/mcp", "headers": {"Authorization": "Bearer NEW"}},
    ]}
    mocks.registry.get_backend.return_value = backend_row(config=old)

    r = client.put("/agents/a1", json={"config": new}, headers=WS)

    assert r.status_code == 200, r.text
    entry = _only_entry(mocks)
    assert entry["workspace_id"] == TEST_WORKSPACE_ID
    assert (entry["user_id"], entry["user_email"], entry["role"]) == ("u-test", "admin@test", "admin")
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("agent", "a1", "updated")
    assert entry["resource_name"] == "Front desk"
    assert entry["agent_ids"] == ["a1"]
    assert entry["changes"]["/system_prompt"] == {"before": "Be kind.", "after": "Be brief."}
    assert entry["changes"]["/mcp_servers/0/headers"] == {"before": "***", "after": "***"}
    assert "Bearer" not in json.dumps(entry)
    # TurnCall named no audit entry (the mocked client sends no header).
    assert entry["engine_links"] == []


def test_a_teardown_records_what_the_agent_was(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(config={"name": "Bot", "system_prompt": "x"})
    mocks.phones.list_numbers.return_value = []

    assert client.delete("/agents/a1", headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["action"]) == ("agent", "deleted")
    assert entry["changes"]["/name"] == {"before": "Bot", "after": None}


def test_a_refused_change_records_nothing(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.list_numbers.return_value = [{"agent_id": "a1", "routing_type": "agent"}]

    assert client.delete("/agents/a1", headers=WS).status_code == 409
    mocks.audit_store.insert.assert_not_called()


def test_limits_record_before_and_after(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_project.return_value = {"limits": {"live_calls": 2}}
    mocks.client.set_project_limits.return_value = {"limits": {"live_calls": 5}}

    assert client.put("/agents/a1/limits", json={"live_calls": 5}, headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert entry["action"] == "limits_set"
    assert entry["changes"] == {"/limits/live_calls": {"before": 2, "after": 5}}


# --- numbers ---------------------------------------------------------------------


def test_a_bind_records_the_number_and_never_its_twilio_token(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9", "server_url_secret": None}
    body = {
        "e164": "+15551234567", "routing_type": "agent", "agent_id": "a1",
        "twilio_account_sid": "AC" + "1" * 32, "twilio_auth_token": "tw-secret-token",
    }

    assert client.post("/phone-numbers", json=body, headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("phone_number", "pn-9", "created")
    assert entry["resource_name"] == "+15551234567"
    assert entry["agent_ids"] == ["a1"]
    assert entry["changes"]["/routing_type"] == {"before": None, "after": "agent"}
    assert entry["changes"]["/twilio_auth_token"] == {"before": None, "after": "***"}
    assert "tw-secret-token" not in json.dumps(entry)


# --- integrations and connected apps ----------------------------------------------


def test_a_provider_key_records_that_it_changed_never_the_key(client, mocks, monkeypatch):
    from app import provider_catalog

    async def probe(provider, document):
        return {"status": "valid", "checked": "x", "detail": "ok"}

    monkeypatch.setattr(provider_catalog, "probe", probe)
    mocks.client.list_providers.return_value = {"accounts": {"deepgram": {}}}
    # Replacing one.
    mocks.integration_store.get.return_value = {"provider": "deepgram", "disconnecting": False, "version": None}
    mocks.integration_store.save.return_value = {"provider": "deepgram", "last4": "1234"}
    mocks.registry.list_backends.return_value = []

    r = client.put("/integrations/providers/deepgram", json={"api_key": "dg-very-secret-1234"}, headers=WS)

    assert r.status_code == 200, r.text
    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("integration", "deepgram", "updated")
    assert entry["changes"] == {"/value": {"before": "***", "after": "***"}}
    assert "dg-very-secret" not in json.dumps(entry)


def _pipedream(mocks):
    mocks.pipedream_store.get_credential.return_value = pipedream.Credential(
        client_id="cid", client_secret="s", project_id="proj", environment="development"
    )


def _flow(mocks, **over):
    flow = {
        "workspace_id": TEST_WORKSPACE_ID,
        "app": "slack_v2",
        "actor": {"id": MEMBER, "email": "boss@x.com", "role": "admin"},
        "known_account_ids": [],
        "started_at": None,
    }
    flow.update(over)
    mocks.pipedream_store.take_connect_flow.return_value = flow
    return pipedream.sign_state("n0nce")


def test_a_connected_app_is_recorded_as_the_admin_who_started_it(client, mocks):
    _pipedream(mocks)
    mocks.pipedream.list_accounts.return_value = [
        {"id": "apn_old", "app": "slack_v2", "name": "Slack", "account_name": "old", "created_at": "2026-11-01"},
        {"id": "apn_new", "app": "slack_v2", "name": "Slack", "account_name": "acme", "created_at": "2026-10-01"},
    ]
    # apn_old was already there when the flow started: not this flow's,
    # although its date sorts after.
    state = _flow(mocks, known_account_ids=["apn_old"])

    r = client.get("/integrations/callback", params={"state": state}, follow_redirects=False)

    assert r.status_code == 303
    entry = _only_entry(mocks)
    assert (entry["user_id"], entry["user_email"], entry["role"]) == (MEMBER, "boss@x.com", "admin")
    assert entry["workspace_id"] == TEST_WORKSPACE_ID
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("connected_app", "apn_new", "connected")


def test_a_replayed_callback_records_nothing_the_second_time(client, mocks):
    _pipedream(mocks)
    mocks.pipedream.list_accounts.return_value = [
        {"id": "apn_new", "app": "slack_v2", "name": "Slack", "account_name": "acme", "created_at": "2026-10-01"},
    ]
    state = _flow(mocks)
    flows = [mocks.pipedream_store.take_connect_flow.return_value, None]  # taken once
    mocks.pipedream_store.take_connect_flow.side_effect = lambda *a: flows.pop(0)

    first = client.get("/integrations/callback", params={"state": state}, follow_redirects=False)
    again = client.get("/integrations/callback", params={"state": state}, follow_redirects=False)

    assert first.status_code == again.status_code == 303
    assert "connected" in first.headers["location"] and "error" in again.headers["location"]
    assert len(audit_entries(mocks)) == 1


def test_connect_holds_who_started_it_server_side_not_in_the_state(client, mocks):
    _pipedream(mocks)
    mocks.pipedream.list_accounts.return_value = [
        {"id": "apn_had", "app": "slack_v2", "name": "Slack", "account_name": "had", "created_at": "2026-01-01"},
    ]
    mocks.pipedream.connect_link.return_value = "https://pipedream.com/c?token=t"

    assert client.post("/integrations/connect", json={"app": "slack_v2"}, headers=WS).status_code == 200

    held = mocks.pipedream_store.start_connect_flow.await_args.kwargs
    assert held["actor"] == {"id": "u-test", "email": "admin@test", "role": "admin"}
    assert (held["workspace_id"], held["app"], held["known_account_ids"]) == (TEST_WORKSPACE_ID, "slack_v2", ["apn_had"])
    state = mocks.pipedream.connect_link.await_args.kwargs["state"]
    assert pipedream.load_state(state) == held["nonce"]
    payload = state.split(".")[0]
    assert "admin@test" not in base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    mocks.audit_store.insert.assert_not_called()  # nothing exists until the callback


def test_an_abandoned_connect_records_nothing(client, mocks):
    _pipedream(mocks)
    state = _flow(mocks)
    r = client.get("/integrations/callback", params={"state": state, "error": "1"}, follow_redirects=False)
    assert r.status_code == 303
    mocks.audit_store.insert.assert_not_called()


def test_attaching_apps_records_the_agent_change(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(config={"name": "Bot", "integrations": []})
    mocks.pipedream_store.mcp_target.return_value = None
    mocks.client.list_webhooks.return_value = []
    assert client.put("/integrations/agents/a1", json={"apps": ["slack_v2"]}, headers=WS).status_code == 200
    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["action"], entry["agent_ids"]) == ("agent", "updated", ["a1"])
    assert entry["changes"] == {"/integrations/0": {"before": None, "after": "slack_v2"}}


# --- members, skills, compliance ---------------------------------------------------


def test_a_role_change_records_before_and_after(client, mocks):
    mocks.auth_store.resolve_membership.return_value = "viewer"
    mocks.auth_store.change_member_role.return_value = True
    mocks.auth_store.user_email.return_value = "bob@x.com"

    assert client.put(f"/members/{MEMBER}", json={"role": "editor"}, headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("member", MEMBER, "role_changed")
    assert entry["resource_name"] == "bob@x.com"
    assert entry["changes"] == {"/role": {"before": "viewer", "after": "editor"}}


def test_a_member_not_found_records_nothing(client, mocks):
    mocks.auth_store.change_member_role.return_value = False
    assert client.put(f"/members/{MEMBER}", json={"role": "editor"}, headers=WS).status_code == 404
    mocks.audit_store.insert.assert_not_called()


def test_an_invite_records_without_its_token(client, mocks):
    mocks.auth_store.create_invite.return_value = {
        "id": "inv1", "workspace_id": TEST_WORKSPACE_ID, "email": "new@x.com", "role": "editor", "token": "tok-secret",
    }
    assert client.post("/members/invites", json={"email": "new@x.com", "role": "editor"}, headers=WS).status_code == 200
    entry = _only_entry(mocks)
    assert entry["changes"]["/token"] == {"before": None, "after": "***"}
    assert "tok-secret" not in json.dumps(entry)


def test_joining_is_recorded_as_the_new_member_in_the_inviting_workspace(client, mocks):
    mocks.auth_store.resolve_session.return_value = {"id": MEMBER, "email": "new@x.com"}
    mocks.auth_store.accept_invite.return_value = {"workspace_id": "w9", "role": "editor", "already_member": False}
    client.cookies.set(COOKIE, "live")

    assert client.post("/invites/accept", json={"token": "tok"}).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["workspace_id"], entry["user_id"], entry["role"]) == ("w9", MEMBER, "editor")
    assert (entry["resource_type"], entry["action"]) == ("member", "joined")


def test_a_skill_records_created_then_updated(client, mocks):
    mocks.workspace_skills.validate.side_effect = None
    mocks.workspace_skills.save_skill.return_value = {"name": "orders", "description": "d", "body": "new"}

    assert client.put("/skills/orders", json={"description": "d", "body": "new"}, headers=WS).status_code == 200
    mocks.workspace_skills.get_skill.return_value = {"name": "orders", "description": "d", "body": "old"}
    assert client.put("/skills/orders", json={"description": "d", "body": "new"}, headers=WS).status_code == 200

    created, updated = audit_entries(mocks)
    assert (created["action"], updated["action"]) == ("created", "updated")
    assert updated["changes"] == {"/body": {"before": "old", "after": "new"}}


def test_compliance_settings_are_recorded(client, mocks):
    settings = {
        "redaction": {"enabled": True, "entities": ["PERSON"]},
        "retention": {"recordings_days": 30, "transcripts_days": None, "events_days": None},
    }
    mocks.registry.list_backends.return_value = []

    assert client.put("/compliance", json=settings, headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("compliance", "workspace", "created")
    assert entry["changes"]["/retention/recordings_days"] == {"before": None, "after": 30}


# --- linked repos ------------------------------------------------------------------


def test_a_push_records_the_commit(client, mocks, monkeypatch):
    store = SimpleNamespace(
        get_link=AsyncMock(return_value={"owner": "acme", "repo": "agents", "branch": "main", "path": "", "connection_id": "c1"}),
        get_token=AsyncMock(return_value="ghp"),
        get_tree_hash=AsyncMock(return_value=None),
        record_push=AsyncMock(),
    )
    for name, fn in vars(store).items():
        monkeypatch.setattr(github_store, name, fn)
    monkeypatch.setattr(
        github, "push_backend", AsyncMock(return_value=SimpleNamespace(commit_sha="abc123", tree_hash="t"))
    )
    mocks.registry.get_backend.return_value = backend_row()

    assert client.post("/agents/a1/github/push", headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["action"], entry["resource_name"]) == ("linked_repo", "pushed", "acme/agents")
    assert entry["changes"] == {"/commit": {"before": None, "after": "abc123"}}


def _github_store(monkeypatch, link):
    store = SimpleNamespace(
        get_link=AsyncMock(return_value=link),
        clear_link=AsyncMock(),
        set_link=AsyncMock(),
        get_connection=AsyncMock(return_value={"id": "c1"}),
        get_token=AsyncMock(return_value="ghp"),
    )
    for name, fn in vars(store).items():
        monkeypatch.setattr(github_store, name, fn)
    return store


def test_unlinking_records_the_link_it_removed(client, mocks, monkeypatch):
    link = {"owner": "acme", "repo": "agents", "branch": "main", "path": "", "connection_id": "c1"}
    store = _github_store(monkeypatch, link)
    mocks.registry.get_backend.return_value = backend_row()

    assert client.delete("/agents/a1/github", headers=WS).status_code == 204

    store.clear_link.assert_awaited_once()
    entry = _only_entry(mocks)
    assert (entry["action"], entry["resource_name"]) == ("unlinked", "acme/agents")
    assert entry["changes"]["/repo"] == {"before": "agents", "after": None}


def test_unlinking_an_agent_with_no_link_is_404_and_records_nothing(client, mocks, monkeypatch):
    store = _github_store(monkeypatch, None)
    mocks.registry.get_backend.return_value = backend_row()

    r = client.delete("/agents/a1/github", headers=WS)

    assert r.status_code == 404, r.text
    store.clear_link.assert_not_awaited()
    assert audit_entries(mocks) == []


def test_linking_an_agent_in_the_callers_workspace_still_works(client, mocks, monkeypatch):
    """#106 scoped the lookup; an editor's own agent links as before."""
    link = {"owner": "acme", "repo": "agents", "branch": "main", "path": "", "connection_id": "c1"}
    store = _github_store(monkeypatch, None)
    store.get_link.side_effect = [None, link]
    monkeypatch.setattr(github, "assert_writable", AsyncMock())
    main.app.dependency_overrides[current_user] = lambda: {"id": "u-test", "email": "admin@test"}
    mocks.registry.get_backend.return_value = backend_row()

    r = client.put("/agents/a1/github", json={"owner": "acme", "repo": "agents", "branch": "main"}, headers=WS)

    assert r.status_code == 200, r.text
    assert mocks.registry.get_backend.await_args.args[1:] == ("a1", TEST_WORKSPACE_ID)
    store.set_link.assert_awaited_once()
    assert _only_entry(mocks)["action"] == "linked"
