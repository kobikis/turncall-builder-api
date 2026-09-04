"""Phone-number endpoints: list, get, bind, unbind, routing validation."""

import httpx

from tests.conftest import backend_row


def _phone(**over):
    n = {
        "id": "pn-1",
        "e164": "+15551234567",
        "sid": "PN" + "a" * 32,
        "routing_type": "agent",
        "agent_id": "a1",
        "project_id": "p1",
        "server_url": None,
        "server_url_secret": None,
        "sms_enabled": False,
    }
    n.update(over)
    return n


def test_list_phone_numbers(client, mocks):
    mocks.phones.list_numbers.return_value = [_phone()]
    r = client.get("/phone-numbers")
    assert r.status_code == 200
    assert r.json()["data"]["phone_numbers"][0]["id"] == "pn-1"


def test_get_phone_number_404(client, mocks):
    mocks.phones.get_number.return_value = None
    assert client.get("/phone-numbers/nope").status_code == 404


def _bind_body(**over):
    b = {
        "e164": "+15551234567",
        "sid": "PN" + "a" * 32,
        "routing_type": "agent",
        "agent_id": "a1",
        "sms_enabled": False,
    }
    b.update(over)
    return b


def test_bind_unassigned_only_mirrors_locally(client, mocks):
    # routing_type "none" never touches TurnCall — just a local mirror row.
    r = client.post("/phone-numbers", json=_bind_body(routing_type="none", agent_id=None))
    assert r.status_code == 200
    assert r.json()["data"]["routing_type"] == "none"
    mocks.client.bind_phone_number.assert_not_called()
    mocks.phones.record_number.assert_awaited_once()


def test_bind_agent_routes_through_turncall(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.bind_phone_number.return_value = {
        "id": "pn-9",
        "twilio_webhooks_configured": True,
    }
    r = client.post("/phone-numbers", json=_bind_body())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["id"] == "pn-9"
    assert data["twilio_webhooks_configured"] is True


def test_bind_agent_routing_requires_agent_id(client, mocks):
    r = client.post("/phone-numbers", json=_bind_body(agent_id=None))
    assert r.status_code == 400


def test_bind_agent_not_found_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.post("/phone-numbers", json=_bind_body()).status_code == 404


def test_update_phone_number_404(client, mocks):
    mocks.phones.get_number.return_value = None
    r = client.put("/phone-numbers/nope", json=_bind_body())
    assert r.status_code == 404


def test_update_same_project_is_in_place(client, mocks):
    # Same owning project -> TurnCall PUT, id + secret stable, rebound False.
    mocks.phones.get_number.return_value = _phone(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", project_id="p1")
    mocks.client.update_phone_number.return_value = {
        "server_url_secret": None,
        "twilio_webhooks_configured": True,
    }
    r = client.put("/phone-numbers/pn-1", json=_bind_body(agent_id="a1"))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["rebound"] is False
    assert data["id"] == "pn-1"  # id stable
    mocks.client.update_phone_number.assert_awaited_once()


def test_update_cross_project_rebinds(client, mocks):
    # Different owning project -> unbind + rebind, rebound True, old mirror dropped.
    mocks.phones.get_number.return_value = _phone(
        routing_type="agent", agent_id="a1", project_id="p1"
    )
    # _resolve_routing (new) resolves to a different project; _key_for_number
    # (unbind) + _bind both call get_backend — return the new-project backend.
    mocks.registry.get_backend.return_value = backend_row(agent_id="a2", project_id="p2")
    mocks.client.bind_phone_number.return_value = {"id": "pn-2", "twilio_webhooks_configured": True}
    r = client.put("/phone-numbers/pn-1", json=_bind_body(agent_id="a2"))
    assert r.status_code == 200
    assert r.json()["data"]["rebound"] is True
    mocks.client.unbind_phone_number.assert_awaited_once()
    mocks.phones.delete_number.assert_awaited_once()  # old mirror removed after new bind


def test_delete_phone_number_404(client, mocks):
    mocks.phones.get_number.return_value = None
    assert client.delete("/phone-numbers/nope").status_code == 404


def test_delete_unassigned_skips_turncall(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="none", agent_id=None)
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 200
    mocks.client.unbind_phone_number.assert_not_called()
    mocks.phones.delete_number.assert_awaited_once()


def test_delete_agent_routed_unbinds_in_turncall(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="agent", agent_id="a1")
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 200
    mocks.client.unbind_phone_number.assert_awaited_once()


def test_delete_maps_turncall_unreachable_to_502(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="agent", agent_id="a1")
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.client.unbind_phone_number.side_effect = httpx.ConnectError("down")
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 502


def test_delete_treats_turncall_404_as_already_unbound(client, mocks):
    mocks.phones.get_number.return_value = _phone(routing_type="agent", agent_id="a1")
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    resp = httpx.Response(404, request=httpx.Request("DELETE", "http://tc/x"))
    mocks.client.unbind_phone_number.side_effect = httpx.HTTPStatusError(
        "nf", request=resp.request, response=resp
    )
    r = client.delete("/phone-numbers/pn-1")
    assert r.status_code == 200  # already gone in TurnCall = success
    mocks.phones.delete_number.assert_awaited_once()
