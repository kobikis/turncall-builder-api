"""An agent's limits (#76): its TurnCall project's, read, set and obeyed."""

import httpx

from tests.conftest import backend_row

LIMITS = {"live_calls": 3, "outbound_calls_per_second": None, "messages_per_minute": None}


def _turncall_refuses(status, body, method="POST"):
    resp = httpx.Response(status, json=body, request=httpx.Request(method, "http://tc/x"))
    return httpx.HTTPStatusError("refused", request=resp.request, response=resp)


def test_limits_are_read_from_the_agents_project(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_project.return_value = {"id": "p1", "name": "x", "limits": LIMITS}
    r = client.get("/agents/a1/limits")
    assert r.status_code == 200
    assert r.json()["data"] == LIMITS
    mocks.client.get_project.assert_awaited_once_with("p1", "tc_key")


def test_setting_limits_replaces_them_all_and_a_left_out_one_is_the_default(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.set_project_limits.return_value = {"id": "p1", "limits": LIMITS}
    r = client.put("/agents/a1/limits", json={"live_calls": 3})
    assert r.status_code == 200
    assert r.json()["data"] == LIMITS
    mocks.client.set_project_limits.assert_awaited_once_with(
        "p1", {"live_calls": 3, "outbound_calls_per_second": None, "messages_per_minute": None}, "tc_key"
    )


def test_a_limit_turncall_refuses_is_its_422(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.set_project_limits.side_effect = _turncall_refuses(422, {
        "success": False, "error": "Validation error",
        "details": {"errors": [{"loc": ["body", "live_calls"], "msg": "Input should be greater than or equal to 1"}]},
    }, "PUT")
    r = client.put("/agents/a1/limits", json={"live_calls": 0})
    assert r.status_code == 422
    assert "live_calls" in r.json()["detail"]


def test_a_chat_message_over_the_limit_says_so(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.send_chat.side_effect = _turncall_refuses(429, {
        "success": False, "code": "rate_limited",
        "error": "This conversation is at its limit of 5 messages per minute",
        "details": {"limit": "messages_per_minute", "max": 5, "retry_after_ms": 30000},
    })
    r = client.post("/agents/a1/chat", json={"message": "hi"})
    assert r.status_code == 429
    assert r.json()["detail"] == "This conversation is at its limit of 5 messages per minute"
    assert r.headers["retry-after"] == "30"


def test_other_chat_failures_stay_generic(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.send_chat.side_effect = _turncall_refuses(500, {"success": False, "error": "internal id 42"})
    r = client.post("/agents/a1/chat", json={"message": "hi"})
    assert r.status_code == 500
    assert r.json()["detail"] == "chat failed"


def test_a_test_call_over_the_live_call_limit_says_so(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.webrtc_connect.side_effect = _turncall_refuses(429, {
        "success": False, "code": "rate_limited",
        "error": "This project is at its limit of 3 live calls",
        "details": {"limit": "live_calls", "max": 3},
    })
    r = client.post("/agents/a1/webrtc/connect", json={"sdp": "o", "type": "offer"})
    assert r.status_code == 429
    assert r.json()["detail"] == "This project is at its limit of 3 live calls"
