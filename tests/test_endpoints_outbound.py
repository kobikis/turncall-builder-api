"""Outbound calls and the do-not-call list, per agent (#74)."""

import httpx

from tests.conftest import backend_row

NUMBER = {"id": "pn1", "e164": "+15550001111", "agent_id": "a1", "project_id": "p1"}


def _turncall_refuses(status, body, headers=None):
    resp = httpx.Response(status, json=body, headers=headers, request=httpx.Request("POST", "http://tc/x"))
    return httpx.HTTPStatusError("refused", request=resp.request, response=resp)


def test_outbound_call_is_placed_from_the_agents_number(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.get_number.return_value = NUMBER
    mocks.client.place_outbound_call.return_value = {"id": "c1", "status": "queued"}
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 201
    assert r.json()["data"]["id"] == "c1"
    mocks.client.place_outbound_call.assert_awaited_once_with(
        agent_id="a1", from_number_id="pn1", to_number="+15552223333", api_key="tc_key"
    )


def test_a_number_routed_elsewhere_cannot_place_the_agents_call(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.get_number.return_value = {**NUMBER, "agent_id": "other", "project_id": "p9"}
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 422
    mocks.client.place_outbound_call.assert_not_awaited()


def test_a_suppressed_destination_is_a_409_naming_the_do_not_call_list(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.get_number.return_value = NUMBER
    mocks.client.place_outbound_call.side_effect = _turncall_refuses(409, {
        "success": False, "code": "number_suppressed",
        "error": "+15552223333 is on this project's suppression list",
        "details": {"to_number": "+15552223333"},
    })
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 409
    assert r.json()["detail"] == "+15552223333 is on this agent's do-not-call list."


def test_a_rate_limit_surfaces_with_its_wait_and_is_not_retried(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.get_number.return_value = NUMBER
    mocks.client.place_outbound_call.side_effect = _turncall_refuses(429, {
        "success": False, "code": "rate_limited",
        "error": "+15550001111 is at its limit of 1 outbound calls per second",
        "details": {"limit": "outbound_calls_per_second", "max": 1, "retry_after_ms": 1200},
    })
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 429
    assert "1 outbound calls per second" in r.json()["detail"]
    assert r.headers["retry-after"] == "2"
    assert mocks.client.place_outbound_call.await_count == 1


def test_do_not_call_list_is_the_agents_suppression_list(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_suppressed.return_value = {
        "success": True, "data": [{"id": "s1", "number": "+15552223333"}], "total": 1, "page": 1, "limit": 100,
    }
    r = client.get("/agents/a1/do-not-call")
    assert r.status_code == 200
    assert r.json()["data"] == [{"id": "s1", "number": "+15552223333"}]
    mocks.client.list_suppressed.assert_awaited_once_with("tc_key", page=1, limit=100)


def test_add_and_remove_a_do_not_call_number(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.suppress_number.return_value = {"id": "s1", "number": "+15552223333"}
    r = client.post("/agents/a1/do-not-call", json={"number": "+15552223333"})
    assert r.status_code == 201
    mocks.client.suppress_number.assert_awaited_once_with("+15552223333", "tc_key")

    r = client.delete("/agents/a1/do-not-call/s1")
    assert r.status_code == 200
    mocks.client.unsuppress_number.assert_awaited_once_with("s1", "tc_key")


def test_an_invalid_number_is_turncalls_422(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.suppress_number.side_effect = _turncall_refuses(422, {
        "success": False, "error": "Validation error",
        "details": {"errors": [{"loc": ["body", "number"], "msg": "String should match pattern"}]},
    })
    r = client.post("/agents/a1/do-not-call", json={"number": "555"})
    assert r.status_code == 422
    assert "number" in r.json()["detail"]


def test_unknown_agent_is_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.get("/agents/nope/do-not-call").status_code == 404
    assert client.post("/agents/nope/calls/outbound", json={"from_number_id": "pn1", "to_number": "+1555"}).status_code == 404


def test_a_turncall_failure_listing_the_do_not_call_list_keeps_its_status(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_suppressed.side_effect = _turncall_refuses(503, {"success": False, "error": "down"})
    r = client.get("/agents/a1/do-not-call")
    assert r.status_code == 503
    assert r.json()["detail"] == "down"


# An agent with a mid-call Connected app gets that app's tools only through
# call-init (the Pipedream token lives an hour), and an outbound call runs
# call-init only from a number with caller-info routing (turncall#277) (#80).
MID_CALL = backend_row(config={"name": "Agent", "system_prompt": "hi", "integrations": ["slack_v2"]})


def test_a_mid_call_app_agent_calls_from_a_caller_info_number(client, mocks):
    mocks.registry.get_backend.return_value = MID_CALL
    mocks.phones.get_number.return_value = {**NUMBER, "routing_type": "agent_call_init"}
    mocks.client.place_outbound_call.return_value = {"id": "c1", "status": "queued"}
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 201


def test_a_mid_call_app_agent_is_refused_a_plain_number_naming_why(client, mocks):
    mocks.registry.get_backend.return_value = MID_CALL
    mocks.phones.get_number.return_value = {**NUMBER, "routing_type": "agent"}
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "slack_v2" in detail and "caller info" in detail and "+15550001111" in detail
    mocks.client.place_outbound_call.assert_not_awaited()


def test_an_end_of_call_only_app_needs_no_caller_info_number(client, mocks):
    end_of_call = {"app": "slack_v2", "call_ended": {"action": "slack_v2-post-message", "summary_arg": "text"}}
    mocks.registry.get_backend.return_value = backend_row(
        config={"name": "Agent", "system_prompt": "hi", "integrations": [end_of_call]}
    )
    mocks.phones.get_number.return_value = {**NUMBER, "routing_type": "agent"}
    mocks.client.place_outbound_call.return_value = {"id": "c1", "status": "queued"}
    r = client.post("/agents/a1/calls/outbound", json={"from_number_id": "pn1", "to_number": "+15552223333"})
    assert r.status_code == 201
