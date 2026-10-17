"""An audit entry links to what TurnCall recorded of the same change (#102, ADR-0028).

The real client notes `X-TurnCall-Audit-Id` in `_request` (test_turncall_client.py
covers that). Here the mocked client stands in for it: a mocked call TurnCall
"audited" notes an engine audit id exactly as `_request` would, and the entry the
route then records is observed on the mocked `audit_store.insert`.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import audit
from app.deps import AuthContext
from app.routers.auth import COOKIE_NAME as COOKIE
from app.turncall_client import collecting, note_audit_id
from tests.conftest import TEST_WORKSPACE_ID, audit_entries, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
ENTRY_ID = "5e1f0000-0000-0000-0000-000000000001"


def _audited(audit_id, result=None, *, project_id=None, api_key=None):
    """A mocked TurnCall call whose response named `audit_id`."""

    async def call(*args, **kwargs):
        note_audit_id(audit_id, project_id=project_id, api_key=api_key)
        return result

    return call


def _only_entry(mocks) -> dict:
    entries = audit_entries(mocks)
    assert len(entries) == 1, entries
    return entries[0]


def _link(project_id, engine_audit_id):
    return {"project_id": project_id, "engine_audit_id": engine_audit_id}


# --- writing the link ---------------------------------------------------------------


def test_an_agent_edit_links_the_engine_entry_in_the_agents_project(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(config={"name": "Bot", "system_prompt": "a"})
    mocks.client.update_agent.side_effect = _audited("eng-1", {}, api_key="tc_key")
    mocks.registry.projects_for_keys.return_value = {"tc_key": "p1"}

    r = client.put("/agents/a1", json={"config": {"name": "Bot", "system_prompt": "b"}}, headers=WS)

    assert r.status_code == 200, r.text
    entry = _only_entry(mocks)
    assert entry["engine_links"] == [_link("p1", "eng-1")]
    # The project is found from the key the call was made with; the key itself
    # is in no entry.
    assert mocks.registry.projects_for_keys.await_args.args[1] == ["tc_key"]
    assert "tc_key" not in json.dumps(entry)


def test_create_links_every_engine_request_it_made_in_order(client, mocks):
    from tests.test_endpoints_agents import _wire_create_success

    _wire_create_success(mocks)
    # The bootstrap calls name their project; the agent's is the new key's.
    mocks.client.create_project.side_effect = _audited("eng-project", "proj-1", project_id="proj-1")
    mocks.client.create_api_key.side_effect = _audited("eng-key", "tc_key", project_id="proj-1")
    mocks.client.create_agent.side_effect = _audited("eng-agent", {"id": "agent-1"}, api_key="tc_key")
    mocks.registry.projects_for_keys.return_value = {"tc_key": "proj-1"}

    assert client.post("/sessions/s1/create", headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["action"]) == ("agent", "created")
    # Registering the webhook is not audited by TurnCall, so it links nothing.
    assert entry["engine_links"] == [
        _link("proj-1", "eng-project"),
        _link("proj-1", "eng-key"),
        _link("proj-1", "eng-agent"),
    ]


def test_a_compliance_change_links_every_agents_project(client, mocks):
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", project_id="p1", api_key="tc_a1"),
        backend_row(agent_id="a2", project_id="p2", api_key="tc_a2"),
    ]
    settings = {
        "redaction": {"enabled": True, "entities": None},
        "retention": {"recordings_days": 30, "transcripts_days": None, "events_days": None},
    }
    mocks.compliance_store.get_workspace.side_effect = [None, {**settings, "updated_at": None}]

    async def redaction(project_id, body, key):
        note_audit_id(f"red-{project_id}", project_id=project_id, api_key=key)
        return {}

    async def retention(project_id, body, key):
        note_audit_id(f"ret-{project_id}", project_id=project_id, api_key=key)
        return {}

    mocks.client.set_project_redaction.side_effect = redaction
    mocks.client.set_project_retention.side_effect = retention

    assert client.put("/compliance", json=settings, headers=WS).status_code == 200

    entry = _only_entry(mocks)
    assert (entry["resource_type"], entry["resource_id"]) == ("compliance", "workspace")
    assert sorted(entry["engine_links"], key=lambda link: link["engine_audit_id"]) == [
        _link("p1", "red-p1"), _link("p2", "red-p2"), _link("p1", "ret-p1"), _link("p2", "ret-p2"),
    ]
    # Each project was named by its own route: nothing to look up by key.
    mocks.registry.projects_for_keys.assert_not_called()


def test_a_change_that_never_reached_turncall_links_nothing(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()

    assert client.post("/agents/a1/start", headers=WS).status_code == 200

    assert _only_entry(mocks)["engine_links"] == []


def test_a_builder_side_change_links_nothing(client, mocks):
    mocks.workspace_skills.save_skill.return_value = {"name": "orders", "description": "d", "body": "b"}

    r = client.put("/skills/orders", json={"description": "d", "body": "b"}, headers=WS)

    assert r.status_code == 200, r.text
    assert _only_entry(mocks)["engine_links"] == []


def test_a_key_the_builder_no_longer_holds_still_links_the_entry(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(config={"name": "Bot", "system_prompt": "a"})
    mocks.client.update_agent.side_effect = _audited("eng-1", {}, api_key="tc_key")

    client.put("/agents/a1", json={"config": {"name": "Bot", "system_prompt": "b"}}, headers=WS)

    assert _only_entry(mocks)["engine_links"] == [_link(None, "eng-1")]


_CTX = AuthContext(user={"id": "u1", "email": "a@x"}, workspace_id=TEST_WORKSPACE_ID, role="admin")


def test_each_entry_links_the_calls_made_since_the_previous_one(mocks):
    async def run():
        trail = audit.AuditTrail()
        with collecting(trail.engine_calls):
            note_audit_id("eng-a", project_id="p1")
            note_audit_id("eng-b", project_id="p2")
            first = await trail.record(_CTX, "phone_number", "pn1", "updated", changes={})
            note_audit_id("eng-c", project_id="p3")
            second = await trail.record(_CTX, "phone_number", "pn2", "updated", changes={})
            third = await trail.record(_CTX, "skill", "s", "updated", changes={})
        # A call after the request returned (a task it started) is not collected.
        note_audit_id("eng-late", project_id="p4")
        assert trail.engine_calls.take() == []
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first["engine_links"] == [_link("p1", "eng-a"), _link("p2", "eng-b")]
    assert second["engine_links"] == [_link("p3", "eng-c")]
    assert third["engine_links"] == []


def test_a_call_outside_an_audited_request_is_noted_nowhere():
    note_audit_id("eng-x", project_id="p1")  # no trail open: nothing to do, no error


# --- reading it ---------------------------------------------------------------------


def test_get_audit_returns_the_links(client, mocks):
    entry = {"id": ENTRY_ID, "action": "updated", "engine_links": [_link("p1", "eng-1")]}
    mocks.audit_store.list_entries.return_value = ([entry], 1)

    r = client.get("/audit", headers=WS)

    assert r.status_code == 200
    assert r.json()["data"][0]["engine_links"] == [_link("p1", "eng-1")]


def _stored(mocks, *links):
    mocks.audit_store.get_entry.return_value = {"id": ENTRY_ID, "engine_links": list(links)}


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://tc/v1/audit-log")
    response = httpx.Response(status, json={"error": "no"}, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def test_the_engine_entries_are_read_with_each_projects_own_key(client, mocks):
    _stored(mocks, _link("p1", "eng-1"), _link("p2", "eng-2"))
    mocks.registry.key_for_project.side_effect = lambda pool, project, ws: {"p1": "tc_p1", "p2": "tc_p2"}[project]
    pages = {
        ("tc_p1", None): {"data": [{"id": "eng-1"}], "next_cursor": "c2"},
        ("tc_p1", "c2"): {"data": [{"id": "eng-1b"}], "next_cursor": None},
        ("tc_p2", None): {"data": [{"id": "eng-2"}], "next_cursor": None},
    }

    async def read(key, *, request_id, cursor=None):
        assert request_id == {"tc_p1": "eng-1", "tc_p2": "eng-2"}[key]
        return pages[(key, cursor)]

    mocks.client.list_audit_log.side_effect = read

    r = client.get(f"/audit/{ENTRY_ID}/engine", headers=WS)

    assert r.status_code == 200, r.text
    assert r.json()["data"] == [
        {**_link("p1", "eng-1"), "entries": [{"id": "eng-1"}, {"id": "eng-1b"}], "error": None},
        {**_link("p2", "eng-2"), "entries": [{"id": "eng-2"}], "error": None},
    ]
    assert mocks.audit_store.get_entry.await_args.args[1:] == (TEST_WORKSPACE_ID, ENTRY_ID)
    # Keys are looked up within the reader's Workspace, and never returned.
    assert {c.args[2] for c in mocks.registry.key_for_project.call_args_list} == {TEST_WORKSPACE_ID}
    assert "tc_p" not in r.text


def test_a_link_that_cannot_be_read_says_why_and_the_rest_still_read(client, mocks):
    _stored(mocks, _link("p-gone", "eng-1"), _link("p-unknown", "eng-2"), _link("p-ok", "eng-3"), _link(None, "eng-4"))
    mocks.registry.key_for_project.side_effect = lambda pool, project, ws: {
        "p-gone": "tc_gone", "p-ok": "tc_ok",
    }.get(project)

    async def read(key, *, request_id, cursor=None):
        if key == "tc_gone":
            raise _status_error(401)  # a torn-down Agent's project and key
        return {"data": [{"id": request_id}], "next_cursor": None}

    mocks.client.list_audit_log.side_effect = read

    data = client.get(f"/audit/{ENTRY_ID}/engine", headers=WS).json()["data"]

    assert [(d["engine_audit_id"], d["entries"]) for d in data] == [
        ("eng-1", None), ("eng-2", None), ("eng-3", [{"id": "eng-3"}]), ("eng-4", None),
    ]
    assert "deleted" in data[0]["error"]
    assert "no key" in data[1]["error"] and "no key" in data[3]["error"]


def test_an_entry_with_no_links_reads_empty(client, mocks):
    _stored(mocks)
    assert client.get(f"/audit/{ENTRY_ID}/engine", headers=WS).json()["data"] == []
    mocks.client.list_audit_log.assert_not_called()


def test_another_workspaces_entry_is_404(client, mocks):
    assert client.get(f"/audit/{ENTRY_ID}/engine", headers=WS).status_code == 404


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_an_admin_reads_the_engine_entries(gate_client, mocks, role):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role
    gate_client.cookies.set(COOKIE, "live")
    assert gate_client.get(f"/audit/{ENTRY_ID}/engine", headers=WS).status_code == 403
    mocks.audit_store.get_entry.assert_not_called()

