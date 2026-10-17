"""A change that lands partway is still in the audit log (ADR-0028).

A change that reaches TurnCall shares no transaction with anything, and many
are several steps: save the master, then write it into every opted-in agent;
unbind a number, then bind it elsewhere. A route begins such a change
(`audit.begin`) as it starts to land, and when it raises before recording, the
trail writes the entry itself, `outcome: "partial"`, with the reason, the
agents and engine requests it had reached, and lets the failure through. One
that failed before anything landed leaves no entry.

The mechanism is tested on a bare trail first, then on each route that can
fail halfway, by failing it at the step after the first that lands.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app import audit as audit_mod
from app.deps import AuthContext
from app.turncall_client import collecting, note_audit_id
from tests.conftest import TEST_WORKSPACE_ID, audit_entries, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
SID = "5e1f0000-0000-0000-0000-000000000001"
CTX = AuthContext(user={"id": "u-test", "email": "admin@test"}, workspace_id=TEST_WORKSPACE_ID, role="admin")


def _refused(status: int = 502, error: str = "TurnCall is down") -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "http://turncall/v1/agents/x")
    return httpx.HTTPStatusError(
        "refused", request=request, response=httpx.Response(status, request=request, json={"error": error})
    )


async def _run(body, *, fail: BaseException | None = None):
    """Drive the trail dependency as FastAPI does: `body(trail)` is the route;
    an exception it raises is thrown in at the yield."""
    gen = audit_mod._trail()
    trail = await gen.__anext__()
    try:
        await body(trail)
    except Exception as exc:  # noqa: BLE001
        with pytest.raises(type(exc)):
            await gen.athrow(exc)
        return trail
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    return trail


# --- the mechanism ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_change_that_raises_after_landing_is_recorded_partial(mocks):
    async def route(trail):
        change = trail.begin(CTX, "mcp_server", SID, "updated", before={"url": "a"}, after={"url": "b"})
        note_audit_id("eng-1", project_id="p1")
        change.agent_ids.append("a1")
        raise _refused(error="agent a2 refused")

    await _run(route)

    (entry,) = audit_entries(mocks)
    assert (entry["outcome"], entry["error"]) == ("partial", "agent a2 refused")
    assert entry["agent_ids"] == ["a1"]
    assert entry["engine_links"] == [{"project_id": "p1", "engine_audit_id": "eng-1"}]
    assert entry["changes"] == {"/url": {"before": "a", "after": "b"}}


@pytest.mark.asyncio
async def test_a_change_that_never_landed_leaves_no_entry(mocks):
    async def route(trail):
        trail.begin(CTX, "agent", "a1", "updated", landed=False, before={"x": 1}, after={"x": 2})
        raise _refused(status=422, error="bad field")

    await _run(route)

    assert audit_entries(mocks) == []


@pytest.mark.asyncio
async def test_a_turncall_write_accepted_since_it_began_counts_as_landed(mocks):
    async def route(trail):
        note_audit_id("before-it-began")  # an earlier step, not this change's
        trail.begin(CTX, "agent", "a1", "updated", landed=False, before={"x": 1}, after={"x": 2})
        note_audit_id("eng-2")
        raise RuntimeError("the registry write failed")

    await _run(route)

    (entry,) = audit_entries(mocks)
    assert entry["outcome"] == "partial"
    assert entry["error"] == "RuntimeError: the registry write failed"


@pytest.mark.asyncio
async def test_a_refusal_before_the_change_began_records_nothing(mocks):
    async def route(trail):
        raise HTTPException(status_code=404, detail="server not found")

    await _run(route)

    assert audit_entries(mocks) == []


@pytest.mark.asyncio
async def test_a_finished_change_is_completed_and_counts_as_recorded(mocks):
    async def route(trail):
        change = trail.begin(CTX, "mcp_server", SID, "updated", before={"url": "a"}, after={"url": "a"})
        await change.record(after={"url": "b"})

    trail = await _run(route)

    (entry,) = audit_entries(mocks)
    assert (entry["outcome"], entry["error"]) == ("completed", None)
    assert entry["changes"] == {"/url": {"before": "a", "after": "b"}}
    assert trail.satisfied


@pytest.mark.asyncio
async def test_a_change_left_open_on_return_is_settled_as_completed(mocks):
    async def route(trail):
        trail.begin(CTX, "mcp_server", SID, "updated", before={"url": "a"}, after={"url": "b"})

    await _run(route)

    assert [e["outcome"] for e in audit_entries(mocks)] == ["completed"]


@pytest.mark.asyncio
async def test_the_entry_failing_to_write_does_not_mask_the_routes_error(mocks):
    mocks.audit_store.insert.side_effect = RuntimeError("audit table unreachable")

    async def route(trail):
        trail.begin(CTX, "mcp_server", SID, "updated")
        raise _refused()

    # The route's own HTTPStatusError is what propagates, not the insert's.
    await _run(route)


def test_before_unknown_names_every_field_and_no_before():
    assert audit_mod.before_unknown({"limits": {"live_calls": 3}}) == {
        "/limits/live_calls": {"after": 3, "before_unknown": True}
    }


def test_collecting_counts_every_call_noted_even_once_taken():
    calls = audit_mod.AuditedCalls()
    with collecting(calls):
        note_audit_id("e1")
        calls.take()
        note_audit_id("e2")
    assert calls.noted == 2


# --- routes that fail halfway ------------------------------------------------------


def _mcp_server():
    return {
        "id": SID, "name": "crm", "transport": "http", "url": "https://old.example.com/mcp",
        "header_names": ["Authorization"],
    }


def test_an_mcp_edit_whose_repush_fails_is_recorded_partial(client, mocks):
    mocks.mcp_registry_store.get_server.return_value = _mcp_server()
    mocks.mcp_registry_store.update_server.return_value = {**_mcp_server(), "url": "https://new.example.com/mcp"}
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_1", config={"name": "One", "workspace_mcp_servers": [SID]}),
        backend_row(agent_id="a2", api_key="tc_2", config={"name": "Two", "workspace_mcp_servers": [SID]}),
    ]

    def update_agent(agent_id, payload, key):
        if agent_id == "a2":
            raise _refused(error="agent a2 is locked")
        note_audit_id("eng-a1", api_key=key)

    mocks.client.update_agent.side_effect = update_agent
    mocks.registry.projects_for_keys.return_value = {"tc_1": "p1"}

    r = client.put(
        f"/integrations/mcp-servers/{SID}",
        json={"name": "crm", "url": "https://new.example.com/mcp", "headers": {"Authorization": "Bearer new"}},
        headers=WS,
    )

    assert r.status_code == 500
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["action"], entry["outcome"]) == ("mcp_server", "updated", "partial")
    assert entry["error"] == "agent a2 is locked"
    assert entry["agent_ids"] == ["a1"]  # reached a1, not a2
    assert entry["engine_links"] == [{"project_id": "p1", "engine_audit_id": "eng-a1"}]
    assert entry["changes"]["/url"] == {"before": "https://old.example.com/mcp", "after": "https://new.example.com/mcp"}
    assert entry["changes"]["/headers"] == {"before": "***", "after": "***"}


def test_an_mcp_removal_that_reaches_no_agent_records_nothing(client, mocks):
    mocks.mcp_registry_store.get_server.return_value = _mcp_server()
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", config={"name": "One", "workspace_mcp_servers": [SID]}),
    ]
    mocks.client.update_agent.side_effect = _refused(status=422, error="no")

    r = client.delete(f"/integrations/mcp-servers/{SID}", headers=WS)

    assert r.status_code == 500
    assert audit_entries(mocks) == []
    mocks.mcp_registry_store.delete_server.assert_not_awaited()


def _number(**over):
    row = {
        "id": "pn1", "e164": "+15555550100", "sid": "PN1", "routing_type": "agent_call_init",
        "agent_id": "a1", "project_id": "p1", "server_url": "http://backend/call-init",
        "server_url_secret": "s1", "sms_enabled": False, "label": None, "carrier": "twilio",
        "workspace_id": TEST_WORKSPACE_ID, "twilio_credentials": "platform",
    }
    row.update(over)
    return row


def test_a_cross_project_move_whose_rebind_fails_is_recorded_partial(client, mocks):
    """Unbound from the old project, then the bind in the new one fails: the
    number is now unbound, and the log says so."""
    mocks.phones.get_number.return_value = _number()
    mocks.registry.get_backend.side_effect = lambda pool, agent_id, ws=None: backend_row(
        agent_id=agent_id, project_id={"a1": "p1", "a2": "p2"}[agent_id], api_key=f"tc_{agent_id}"
    )
    mocks.client.unbind_phone_number.side_effect = lambda *a, **k: note_audit_id("eng-unbind", project_id="p1")
    mocks.client.bind_phone_number.side_effect = _refused(error="number already bound")

    r = client.put(
        "/phone-numbers/pn1",
        json={"e164": "+15555550100", "routing_type": "agent_call_init", "agent_id": "a2"},
        headers=WS,
    )

    assert r.status_code >= 400
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["action"], entry["outcome"]) == ("phone_number", "updated", "partial")
    assert "number already bound" in entry["error"]
    assert entry["changes"]["/routing_type"] == {"before": "agent_call_init", "after": "none"}
    assert entry["changes"]["/agent_id"] == {"before": "a1", "after": None}
    assert entry["engine_links"] == [{"project_id": "p1", "engine_audit_id": "eng-unbind"}]
    assert entry["agent_ids"] == ["a1"]


def test_an_agent_edit_turncall_accepted_but_not_saved_is_recorded_partial(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.update_agent.side_effect = lambda *a, **k: note_audit_id("eng-edit", project_id="p1")
    mocks.client.list_webhooks.return_value = []
    mocks.registry.update_config.side_effect = RuntimeError("connection reset")

    r = client.put("/agents/a1", json={"config": {"name": "Agent", "system_prompt": "bye"}}, headers=WS)

    assert r.status_code == 500
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["action"], entry["outcome"]) == ("agent", "updated", "partial")
    assert entry["error"] == "RuntimeError: connection reset"
    assert entry["changes"]["/system_prompt"] == {"before": "hi", "after": "bye"}


def test_an_agent_edit_turncall_refused_records_nothing(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.update_agent.side_effect = _refused(status=422, error="bad voice")

    r = client.put("/agents/a1", json={"config": {"name": "Agent", "system_prompt": "bye"}}, headers=WS)

    assert r.status_code == 422
    assert audit_entries(mocks) == []


def test_a_teardown_that_fails_after_the_container_went_down_is_recorded(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.list_numbers.return_value = []
    mocks.registry.mark_deleted.side_effect = RuntimeError("connection reset")

    r = client.delete("/agents/a1", headers=WS)

    assert r.status_code == 500
    (entry,) = audit_entries(mocks)
    assert (entry["action"], entry["outcome"]) == ("deleted", "partial")


def test_a_provider_key_saved_but_not_projected_is_recorded_partial(client, mocks, monkeypatch):
    from app import integration_store, provider_catalog, provider_integrations
    from app.routers import provider_integrations as route

    mocks.integration_store.get.return_value = None
    mocks.integration_store.save.return_value = {"provider": "deepgram"}
    monkeypatch.setattr(provider_catalog, "probe", AsyncMock(return_value={"status": "valid"}))
    monkeypatch.setattr(
        provider_integrations, "project",
        AsyncMock(side_effect=integration_store.IntegrationError("INTEGRATIONS_ENCRYPTION_KEY changed")),
    )
    monkeypatch.setattr(route, "_known", AsyncMock(return_value=None))
    monkeypatch.setattr(route, "_document", lambda provider, body: {"api_key": "dg_secret_1234"})

    r = client.put("/integrations/providers/deepgram", json={"api_key": "dg_secret_1234"}, headers=WS)

    assert r.status_code == 503, r.text
    (entry,) = audit_entries(mocks)
    assert (entry["resource_type"], entry["action"], entry["outcome"]) == ("integration", "created", "partial")
    assert entry["changes"] == {"/value": {"before": None, "after": "***"}}
    assert "dg_secret" not in str(entry)


@pytest.mark.asyncio
async def test_a_change_takes_only_its_own_fields(mocks):
    trail = audit_mod.AuditTrail()
    change = trail.begin(CTX, "agent", "a1", "updated")
    with pytest.raises(TypeError):
        await change.record(landed=False)
    with pytest.raises(ValueError):
        trail.begin(CTX, "not_a_resource", "x", "updated")
