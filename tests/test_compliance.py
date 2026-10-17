"""Workspace compliance settings on the TestClient seam (#103, ADR-0028).

Projection is observed as calls on the mocked TurnCall client — what an admin's
write makes the engine be asked, project by project. The store is autospecced;
its SQL is covered by test_compliance_store_db.py.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import compliance
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
NEVER = {"recordings_days": None, "transcripts_days": None, "events_days": None}
OFF = {"enabled": False, "entities": None}
SETTINGS = {
    "redaction": {"enabled": True, "entities": ["PHONE_NUMBER", "PERSON"]},
    "retention": {"recordings_days": 30, "transcripts_days": 90, "events_days": None},
}


def _saved(mocks, settings=SETTINGS):
    """What the store hands back once a setting is saved."""
    mocks.compliance_store.get_workspace.return_value = {**settings, "updated_at": None}


def _two_agents(mocks):
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", project_id="p1", api_key="tc_a1"),
        backend_row(agent_id="a2", project_id="p2", api_key="tc_a2", config={"name": "Second"}),
    ]


def _writes(mocks):
    """(project, body, key) of every redaction and retention PUT, in order."""
    red = [c.args for c in mocks.client.set_project_redaction.await_args_list]
    ret = [c.args for c in mocks.client.set_project_retention.await_args_list]
    return red, ret


def _refused(project_id):
    request = httpx.Request("PUT", f"http://tc/v1/projects/{project_id}/redaction")
    response = httpx.Response(403, json={"error": "A project can only set its own redaction"}, request=request)
    return httpx.HTTPStatusError("403", request=request, response=response)


# --- the Workspace setting --------------------------------------------------------


def test_put_stores_and_projects_into_every_agent(client, mocks):
    _two_agents(mocks)
    _saved(mocks)

    r = client.put("/compliance", json=SETTINGS, headers=WS)

    assert r.status_code == 200, r.text
    kw = mocks.compliance_store.save_workspace.await_args.kwargs
    assert kw == {"redaction": SETTINGS["redaction"], "retention": SETTINGS["retention"]}
    red, ret = _writes(mocks)
    assert red == [("p1", SETTINGS["redaction"], "tc_a1"), ("p2", SETTINGS["redaction"], "tc_a2")]
    assert ret == [("p1", SETTINGS["retention"], "tc_a1"), ("p2", SETTINGS["retention"], "tc_a2")]
    data = r.json()["data"]
    assert [(a["agent_id"], a["state"]) for a in data["agents"]] == [("a1", "applied"), ("a2", "applied")]
    assert data["agents"][1]["name"] == "Second"
    assert data["complete"] is True and data["set"] is True
    recorded = [c.args[2] for c in mocks.compliance_store.record_projection.await_args_list]
    assert recorded == ["a1", "a2"]


def test_a_failed_project_is_reported_recorded_and_retried(client, mocks):
    """A partial failure is visible and retryable: the next apply writes only
    the agent that failed."""
    _two_agents(mocks)
    _saved(mocks)

    async def redaction(project_id, body, key):
        if project_id == "p2":
            raise _refused("p2")

    mocks.client.set_project_redaction.side_effect = redaction
    r = client.put("/compliance", json=SETTINGS, headers=WS)

    data = r.json()["data"]
    assert [a["state"] for a in data["agents"]] == ["applied", "failed"]
    assert "only set its own redaction" in data["agents"][1]["error"]
    assert data["complete"] is False
    failed = mocks.compliance_store.record_projection.await_args_list[1].kwargs
    assert failed["state"] == "failed" and failed["error"]

    # The retry: a1 already holds this exact setting, a2 does not.
    fp = compliance.fingerprint(compliance.effective(SETTINGS, None))
    mocks.compliance_store.list_projections.return_value = {
        "a1": {"state": "applied", "fingerprint": fp, "error": None},
        "a2": {"state": "failed", "fingerprint": fp, "error": "403"},
    }
    mocks.client.set_project_redaction.reset_mock(side_effect=True)
    mocks.client.set_project_retention.reset_mock()
    r = client.post("/compliance/apply", headers=WS)

    red, ret = _writes(mocks)
    assert [w[0] for w in red] == ["p2"] and [w[0] for w in ret] == ["p2"]
    assert r.json()["data"]["complete"] is True


def test_reapplying_a_finished_projection_writes_nothing(client, mocks):
    _two_agents(mocks)
    _saved(mocks)
    fp = compliance.fingerprint(compliance.effective(SETTINGS, None))
    mocks.compliance_store.list_projections.return_value = {
        a: {"state": "applied", "fingerprint": fp, "error": None} for a in ("a1", "a2")
    }
    r = client.put("/compliance", json=SETTINGS, headers=WS)
    assert r.json()["data"]["complete"] is True
    assert _writes(mocks) == ([], [])
    mocks.compliance_store.record_projection.assert_not_called()


def test_a_changed_setting_shows_pending_until_applied(client, mocks):
    _two_agents(mocks)
    _saved(mocks)
    # Both hold what the Workspace said before this change.
    mocks.compliance_store.list_projections.return_value = {
        a: {"state": "applied", "fingerprint": "an-older-one", "error": None} for a in ("a1", "a2")
    }
    data = client.get("/compliance", headers=WS).json()["data"]
    assert [a["state"] for a in data["agents"]] == ["pending", "pending"]
    assert data["complete"] is False
    assert _writes(mocks) == ([], [])  # a read never writes


def test_unset_is_the_engines_default_and_writes_nothing(client, mocks):
    """No redaction, keep everything: what a fresh project already holds."""
    _two_agents(mocks)
    data = client.get("/compliance", headers=WS).json()["data"]
    assert data["set"] is False
    assert data["redaction"] == OFF and data["retention"] == NEVER
    assert [a["state"] for a in data["agents"]] == ["applied", "applied"]
    assert "PERSON" in data["entity_types"]["supported"]
    assert "PERSON" not in data["entity_types"]["default"]

    client.post("/compliance/apply", headers=WS)
    assert _writes(mocks) == ([], [])


def test_the_webhook_project_gets_the_workspace_setting_never_an_override(client, mocks):
    mocks.registry.list_backends.return_value = [backend_row(agent_id="a1", project_id="p1", api_key="tc_a1")]
    mocks.phones.workspace_webhook_project.return_value = {"project_id": "wp-ws", "api_key": "tc_wp"}
    _saved(mocks)
    mocks.compliance_store.list_overrides.return_value = {
        f"webhook-numbers:{TEST_WORKSPACE_ID}": {"redaction": OFF, "retention": None},
    }
    data = client.put("/compliance", json=SETTINGS, headers=WS).json()["data"]
    red, _ = _writes(mocks)
    assert red[-1] == ("wp-ws", SETTINGS["redaction"], "tc_wp")
    assert data["webhook_numbers"] == {"state": "applied", "error": None}
    assert [a["agent_id"] for a in data["agents"]] == ["a1"]


@pytest.mark.parametrize(
    ("body", "names"),
    [
        ({**SETTINGS, "redaction": {"enabled": True, "entities": ["PHONE", "PERSON"]}}, "PHONE"),
        ({**SETTINGS, "redaction": {"enabled": True, "entities": []}}, "at least one"),
        ({**SETTINGS, "redaction": {"entities": ["PERSON"]}}, "redaction.enabled"),
        ({**SETTINGS, "retention": {**NEVER, "recordings_days": 0}}, "recordings_days"),
        ({**SETTINGS, "retention": {**NEVER, "events_days": 36501}}, "events_days"),
        ({**SETTINGS, "retention": {**NEVER, "transcripts_days": "30"}}, "transcripts_days"),
        ({**SETTINGS, "retention": {**NEVER, "audio_days": 3}}, "audio_days"),
        ({"redaction": SETTINGS["redaction"]}, "retention"),
    ],
)
def test_invalid_settings_fail_fast_with_the_field(client, mocks, body, names):
    _two_agents(mocks)
    r = client.put("/compliance", json=body, headers=WS)
    assert r.status_code == 422
    assert names in r.json()["detail"]
    mocks.compliance_store.save_workspace.assert_not_called()
    assert _writes(mocks) == ([], [])


def test_the_bounds_themselves_are_accepted(client, mocks):
    _saved(mocks)
    body = {
        "redaction": {"enabled": True, "entities": ["PERSON", "PERSON"]},
        "retention": {"recordings_days": 1, "transcripts_days": 36500, "events_days": None},
    }
    assert client.put("/compliance", json=body, headers=WS).status_code == 200
    stored = mocks.compliance_store.save_workspace.await_args.kwargs
    assert stored["redaction"]["entities"] == ["PERSON"]  # deduplicated, as the engine does


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_the_workspace_setting_is_admin_only(gate_client, mocks, role):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role
    gate_client.cookies.set(COOKIE, "live")
    assert gate_client.get("/compliance", headers=WS).status_code == 403
    assert gate_client.put("/compliance", json=SETTINGS, headers=WS).status_code == 403
    assert gate_client.post("/compliance/apply", headers=WS).status_code == 403
    mocks.compliance_store.save_workspace.assert_not_called()


# --- an Agent's override ------------------------------------------------------------


def test_an_override_replaces_redaction_and_retention_field_by_field():
    override = {
        "redaction": {"enabled": False, "entities": None},
        "retention": {"recordings_days": 7, "transcripts_days": None, "events_days": None},
    }
    assert compliance.effective(SETTINGS, override) == {
        "redaction": OFF,
        "retention": {"recordings_days": 7, "transcripts_days": 90, "events_days": None},
    }
    # Without one, an agent follows the Workspace; without either, the engine's default.
    assert compliance.effective(SETTINGS, None) == {k: SETTINGS[k] for k in ("redaction", "retention")}
    assert compliance.effective(None, None) == compliance.DEFAULT


def test_workspace_projection_writes_each_agents_own_effective_setting(client, mocks):
    _two_agents(mocks)
    _saved(mocks)
    mocks.compliance_store.list_overrides.return_value = {
        "a2": {"redaction": None, "retention": {**NEVER, "recordings_days": 7}},
    }
    data = client.put("/compliance", json=SETTINGS, headers=WS).json()["data"]
    _, ret = _writes(mocks)
    assert ret[0] == ("p1", SETTINGS["retention"], "tc_a1")
    assert ret[1] == ("p2", {**SETTINGS["retention"], "recordings_days": 7}, "tc_a2")
    assert [a["overridden"] for a in data["agents"]] == [False, True]


def test_put_an_override_projects_into_that_agent_only(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", project_id="p1", api_key="tc_a1")
    _saved(mocks)
    override = {"redaction": None, "retention": {**NEVER, "recordings_days": 7}}
    mocks.compliance_store.get_override.return_value = override

    r = client.put("/agents/a1/compliance", json=override, headers=WS)

    assert r.status_code == 200, r.text
    kw = mocks.compliance_store.save_override.await_args.kwargs
    assert kw == {"redaction": None, "retention": {**NEVER, "recordings_days": 7}}
    red, ret = _writes(mocks)
    assert red == [("p1", SETTINGS["redaction"], "tc_a1")]
    assert ret == [("p1", {**SETTINGS["retention"], "recordings_days": 7}, "tc_a1")]
    data = r.json()["data"]
    assert data["projection"] == {"state": "applied", "error": None}
    assert data["effective"]["retention"]["recordings_days"] == 7
    mocks.registry.list_backends.assert_not_called()


def test_an_all_null_override_is_removed(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    r = client.put("/agents/a1/compliance", json={"redaction": None, "retention": NEVER}, headers=WS)
    assert r.status_code == 200
    assert mocks.compliance_store.save_override.await_args.kwargs == {"redaction": None, "retention": None}


def test_an_override_is_validated_like_the_workspace_setting(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    r = client.put(
        "/agents/a1/compliance",
        json={"redaction": {"enabled": True, "entities": ["NOPE"]}, "retention": None},
        headers=WS,
    )
    assert r.status_code == 422 and "NOPE" in r.json()["detail"]
    r = client.put("/agents/a1/compliance", json={"retention": None}, headers=WS)
    assert r.status_code == 422 and "redaction" in r.json()["detail"]
    mocks.compliance_store.save_override.assert_not_called()


def test_an_override_on_another_workspaces_agent_is_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    r = client.put("/agents/zz/compliance", json={"redaction": None, "retention": None}, headers=WS)
    assert r.status_code == 404
    mocks.compliance_store.save_override.assert_not_called()


def test_a_viewer_reads_an_agents_setting_but_only_an_admin_overrides(gate_client, mocks):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.registry.get_backend.return_value = backend_row()
    gate_client.cookies.set(COOKIE, "live")
    mocks.auth_store.resolve_membership.return_value = "viewer"
    r = gate_client.get("/agents/a1/compliance", headers=WS)
    assert r.status_code == 200
    assert r.json()["data"]["effective"] == compliance.DEFAULT
    mocks.auth_store.resolve_membership.return_value = "editor"
    body = {"redaction": OFF, "retention": None}
    assert gate_client.put("/agents/a1/compliance", json=body, headers=WS).status_code == 403
    mocks.compliance_store.save_override.assert_not_called()


# --- new projects -------------------------------------------------------------------


def test_create_projects_the_workspace_setting_into_the_new_agent(client, mocks):
    from tests.test_endpoints_agents import _wire_create_success

    _wire_create_success(mocks)
    _saved(mocks)
    assert client.post("/sessions/s1/create").status_code == 200
    red, ret = _writes(mocks)
    assert red == [("proj-1", SETTINGS["redaction"], "tc_key")]
    assert ret == [("proj-1", SETTINGS["retention"], "tc_key")]
    rec = mocks.compliance_store.record_projection.await_args
    assert rec.args[2] == "agent-1" and rec.kwargs["state"] == "applied"


def test_create_writes_nothing_while_the_workspace_has_no_setting(client, mocks):
    from tests.test_endpoints_agents import _wire_create_success

    _wire_create_success(mocks)
    assert client.post("/sessions/s1/create").status_code == 200
    assert _writes(mocks) == ([], [])


def test_a_create_that_cannot_project_still_creates_and_records_why(client, mocks):
    from tests.test_endpoints_agents import _wire_create_success

    _wire_create_success(mocks)
    _saved(mocks)
    mocks.client.set_project_retention.side_effect = httpx.ConnectError("down")
    r = client.post("/sessions/s1/create")
    assert r.status_code == 200 and r.json()["data"]["created"] is True
    rec = mocks.compliance_store.record_projection.await_args.kwargs
    assert rec["state"] == "failed" and "ConnectError" in rec["error"]


def test_a_new_webhook_project_gets_the_workspace_setting(mocks):
    _saved(mocks)
    target = compliance.webhook_target(TEST_WORKSPACE_ID, {"project_id": "wp-ws", "api_key": "tc_wp"})
    asyncio.run(compliance.project_into_new_target(object(), mocks.client, TEST_WORKSPACE_ID, target))
    red, _ = _writes(mocks)
    assert red == [("wp-ws", SETTINGS["redaction"], "tc_wp")]
    assert mocks.compliance_store.record_projection.await_args.args[2] == f"webhook-numbers:{TEST_WORKSPACE_ID}"


# --- concurrency, the per-agent read, clearing and deleting (#103 review) -----------


def _agents(mocks, n):
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id=f"a{i:02}", project_id=f"p{i:02}", api_key=f"tc_{i:02}", config={"name": f"A{i}"})
        for i in range(n)
    ]


def test_targets_are_written_concurrently_a_few_at_a_time_and_reported_in_order(client, mocks):
    """Writes finish in the reverse of target order; the report does not care."""
    n = 20
    _agents(mocks, n)
    _saved(mocks)
    in_flight, most = 0, 0

    async def slow_redaction(project_id, setting, key):
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        await asyncio.sleep(0.002 * (n - int(project_id[1:])))
        in_flight -= 1

    mocks.client.set_project_redaction.side_effect = slow_redaction
    mocks.client.set_project_retention.side_effect = (
        lambda project_id, *a: _raise(_refused(project_id)) if project_id == "p07" else None
    )

    data = client.put("/compliance", json=SETTINGS, headers=WS).json()["data"]

    assert 1 < most <= compliance.CONCURRENCY
    assert [a["agent_id"] for a in data["agents"]] == [f"a{i:02}" for i in range(n)]
    states = {a["agent_id"]: a["state"] for a in data["agents"]}
    assert states.pop("a07") == "failed"
    assert set(states.values()) == {"applied"}
    finished = [c.args[2] for c in mocks.compliance_store.record_projection.await_args_list]
    assert finished != sorted(finished)  # the writes did finish out of order

    # Idempotent: with every target recorded, applying again writes only the
    # one that failed.
    mocks.compliance_store.list_projections.return_value = {
        c.args[2]: c.kwargs for c in mocks.compliance_store.record_projection.await_args_list
    }
    mocks.client.set_project_redaction.reset_mock()
    mocks.client.set_project_retention.side_effect = None
    data = client.post("/compliance/apply", headers=WS).json()["data"]
    assert [c.args[0] for c in mocks.client.set_project_redaction.await_args_list] == ["p07"]
    assert data["complete"] is True


def _raise(exc):
    raise exc


def test_the_agent_report_reads_only_its_own_projection(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    _saved(mocks)
    fp = compliance.fingerprint(compliance.effective(SETTINGS, None))
    mocks.compliance_store.get_projection.return_value = {"state": "applied", "fingerprint": fp, "error": None}

    data = client.get("/agents/a1/compliance", headers=WS).json()["data"]

    assert data["projection"] == {"state": "applied", "error": None}
    assert mocks.compliance_store.get_projection.await_args.args[1:] == (TEST_WORKSPACE_ID, "a1")
    mocks.compliance_store.list_projections.assert_not_called()


def test_clearing_an_override_is_its_own_action(client, mocks):
    from tests.conftest import audit_entries

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.compliance_store.get_override.return_value = {"redaction": OFF, "retention": None}

    r = client.put("/agents/a1/compliance", json={"redaction": None, "retention": None}, headers=WS)

    assert r.status_code == 200, r.text
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == ("compliance", "a1", "override_cleared")
    assert entry["changes"]["/redaction/enabled"] == {"before": False, "after": None}


def test_clearing_an_override_that_was_never_set_records_nothing(client, mocks):
    from tests.conftest import audit_entries

    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")

    r = client.put("/agents/a1/compliance", json={"redaction": None, "retention": None}, headers=WS)

    assert r.status_code == 200, r.text
    assert audit_entries(mocks) == []


def test_deleting_an_agent_forgets_its_override_and_projection(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.phones.list_numbers.return_value = []
    order = []
    mocks.registry.mark_deleted.side_effect = lambda *a: order.append("hidden")
    mocks.compliance_store.forget_agent.side_effect = lambda *a: order.append("forgotten")

    assert client.delete("/agents/a1", headers=WS).status_code == 200

    assert mocks.compliance_store.forget_agent.await_args.args[1:] == (TEST_WORKSPACE_ID, "a1")
    assert order == ["hidden", "forgotten"]
