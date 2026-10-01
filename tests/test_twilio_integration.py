"""The Workspace's Twilio account on the Integrations page.

Connected once, chosen when binding a number, and rotated in one place: a new
pair reaches every number bound with it. Never projected into agent projects.
"""

from __future__ import annotations

import httpx
import pytest

from app import twilio_api
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
AC = "AC" + "c" * 32
TOKEN = "workspace-twilio-token"


@pytest.fixture
def twilio(monkeypatch):
    """Twilio's number list, answering as each test says."""
    state = {"numbers": [{"sid": "PN1", "phone_number": "+15550001111", "friendly_name": "Main"}], "raise": None}

    async def fake(sid, token, page_size=1000):
        if state["raise"]:
            raise state["raise"]
        return state["numbers"]

    monkeypatch.setattr(twilio_api, "list_numbers", fake)
    return state


def _saved():
    return {"account_sid": AC, "last4": "oken", "validation": {"status": "valid"},
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"}


def _number(**over):
    n = {"id": "pn-1", "e164": "+15550001111", "sid": "PN1", "routing_type": "agent",
         "agent_id": "a1", "project_id": "p1", "server_url": None, "server_url_secret": None,
         "sms_enabled": False, "twilio_account_sid": AC, "twilio_credentials": "workspace"}
    n.update(over)
    return n


def test_connect_validates_stores_and_never_returns_the_token(client, mocks, twilio):
    mocks.twilio_store.save.return_value = _saved()
    r = client.put("/integrations/twilio", json={"account_sid": AC, "auth_token": TOKEN}, headers=WS)
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
    kw = mocks.twilio_store.save.await_args.kwargs
    assert (kw["account_sid"], kw["auth_token"]) == (AC, TOKEN)
    assert kw["validation"]["status"] == "valid"
    mocks.client.put_credential.assert_not_called()  # never projected into agents


def test_rejected_credentials_are_not_stored(client, mocks, twilio):
    twilio["raise"] = twilio_api.TwilioRejected("Twilio answered 401")
    r = client.put("/integrations/twilio", json={"account_sid": AC, "auth_token": "bad"}, headers=WS)
    assert r.status_code == 400
    assert "not saved" in r.json()["detail"]
    mocks.twilio_store.save.assert_not_called()


def test_unreachable_twilio_stores_and_says_so(client, mocks, twilio):
    twilio["raise"] = httpx.ConnectError("down")
    mocks.twilio_store.save.return_value = _saved()
    client.put("/integrations/twilio", json={"account_sid": AC, "auth_token": TOKEN}, headers=WS)
    assert mocks.twilio_store.save.await_args.kwargs["validation"]["status"] == "unverified"


def test_a_rotation_reaches_every_number_bound_with_it(client, mocks, twilio):
    mocks.twilio_store.save.return_value = _saved()
    mocks.phones.on_workspace_twilio.return_value = [_number(), _number(id="pn-2", routing_type="none")]
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", api_key="tc_a1")

    r = client.put("/integrations/twilio", json={"account_sid": AC, "auth_token": "rotated"}, headers=WS)

    sent = mocks.client.update_phone_number.await_args
    assert sent.args[0] == "pn-1"
    assert sent.args[1]["twilio_auth_token"] == "rotated"
    assert sent.args[2] == "tc_a1"
    states = {n["id"]: n["state"] for n in r.json()["data"]["numbers"]}
    assert states == {"pn-1": "updated", "pn-2": "unassigned"}


def test_a_number_the_new_account_does_not_hold_fails_visibly(client, mocks, twilio):
    mocks.twilio_store.save.return_value = _saved()
    mocks.phones.on_workspace_twilio.return_value = [_number()]
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    request = httpx.Request("PUT", "http://tc/v1/phone-numbers/pn-1")
    mocks.client.update_phone_number.side_effect = httpx.HTTPStatusError(
        "422", request=request,
        response=httpx.Response(422, json={"error": "+15550001111 is not a phone number on Twilio account AC…"}, request=request),
    )
    r = client.put("/integrations/twilio", json={"account_sid": AC, "auth_token": TOKEN}, headers=WS)
    [n] = r.json()["data"]["numbers"]
    assert n["state"] == "failed" and "not a phone number" in n["error"]


def test_disconnect_is_refused_while_numbers_use_it(client, mocks):
    mocks.phones.on_workspace_twilio.return_value = [_number()]
    r = client.delete("/integrations/twilio", headers=WS)
    assert r.status_code == 409
    assert "+15550001111" in r.json()["detail"]
    mocks.twilio_store.delete.assert_not_called()


def test_disconnect_with_no_numbers(client, mocks):
    r = client.delete("/integrations/twilio", headers=WS)
    assert r.status_code == 200
    mocks.twilio_store.delete.assert_awaited_once()


def test_numbers_lists_the_account_and_marks_bound_ones(client, mocks, twilio):
    mocks.twilio_store.credentials.return_value = (AC, TOKEN)
    twilio["numbers"].append({"sid": "PN2", "phone_number": "+15550002222", "friendly_name": None})
    mocks.phones.list_numbers.return_value = [_number()]
    r = client.get("/integrations/twilio/numbers", headers=WS)
    assert [(n["sid"], n["bound"]) for n in r.json()["data"]] == [("PN1", True), ("PN2", False)]


def test_binding_with_the_workspace_account_sends_its_pair(client, mocks):
    mocks.twilio_store.credentials.return_value = (AC, TOKEN)
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {"id": "pn-9", "external_number_sid": "PN1"}
    r = client.post("/phone-numbers", json={
        "e164": "+15550001111", "sid": "PN1", "routing_type": "agent", "agent_id": "a1",
        "use_workspace_twilio": True,
    })
    assert r.status_code == 200, r.text
    payload = mocks.client.bind_phone_number.await_args.args[0]
    assert (payload["twilio_account_sid"], payload["twilio_auth_token"]) == (AC, TOKEN)
    mirrored = mocks.phones.record_number.await_args.args[1]
    assert mirrored["twilio_source"] == "workspace"
    assert "twilio_auth_token_encrypted" not in mirrored  # the account row is the copy
    assert r.json()["data"]["twilio_credentials"] == "workspace"


def test_binding_with_no_workspace_account_says_so(client, mocks):
    r = client.post("/phone-numbers", json={
        "e164": "+15550001111", "sid": "PN1", "routing_type": "agent", "agent_id": "a1",
        "use_workspace_twilio": True,
    })
    assert r.status_code == 400
    assert "Integrations" in r.json()["detail"]


def test_editors_cannot_connect_twilio(gate_client, mocks):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = "editor"
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.put("/integrations/twilio", json={"account_sid": AC, "auth_token": TOKEN}, headers=WS)
    assert r.status_code == 403
