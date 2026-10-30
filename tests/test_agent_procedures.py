"""An agent's procedures in its Config (#137): `procedures` reaches TurnCall
untouched on every path that pushes a config, TurnCall's refusal of one comes
back naming it (and the step its message names), and a call's `procedures`
summary reaches the Console beside its `skills`."""

from __future__ import annotations


import httpx

from app import pipedream
from app.mapper import to_create_agent_request
from tests.conftest import backend_row
from tests.test_endpoints_agents import _wire_create_success
from tests.test_integrations import _agent_backend, _call_config, _configured, _target

PROCEDURES = [
    {
        "name": "no_internet",
        "description": "Caller has no internet connection at home",
        "start": "call",
        "window_depth": 1,
        "steps": [
            {"id": "lights", "kind": "ask", "start": True,
             "text": "Ask which lights on the router are on.",
             "moves": [
                 {"to": "restart", "when": "the internet light is red"},
                 {"to": "resolved", "when": "all lights are green"}]},
            {"id": "restart", "kind": "instruct",
             "text": "Have the caller restart the router.",
             "moves": [
                 {"to": "resolved", "when": "it is back"},
                 {"to": "technician", "when": "it is still down"}]},
            {"id": "resolved", "kind": "end", "text": "Close politely."},
            {"id": "technician", "kind": "escalate",
             "text": "Tell the caller a technician will take it from here.",
             "escalate": {"transfer_call": {
                 "target_number": "+15550009999", "transfer_mode": "warm",
                 "briefing": {"from_summary": True}}}},
        ],
    }
]
CONFIG = {"name": "A", "system_prompt": "hi", "procedures": PROCEDURES}


def _sent(mocks) -> dict:
    return mocks.client.update_agent.await_args.args[1]["config"]


def test_the_mapper_sends_them_as_written():
    config = to_create_agent_request(CONFIG, tools_base_url="http://b")["config"]
    assert config["procedures"] == PROCEDURES


def test_an_agent_without_them_sends_none():
    config = to_create_agent_request(
        {"name": "A", "system_prompt": "hi", "procedures": None}, tools_base_url="http://b"
    )["config"]
    assert "procedures" not in config


def test_create_sends_them(client, mocks):
    _wire_create_success(mocks)
    session = mocks.store.get_session.return_value
    mocks.store.get_session.return_value = {**session, "config": {**session["config"], **CONFIG}}
    assert client.post("/sessions/s1/create").status_code == 200
    assert mocks.client.create_agent.await_args.args[0]["config"]["procedures"] == PROCEDURES


def test_an_edit_sends_them(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    assert client.put("/agents/a1", json={"config": CONFIG}).status_code == 200
    assert _sent(mocks)["procedures"] == PROCEDURES


def test_an_unrelated_save_keeps_them(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(config=CONFIG)
    edited = {**CONFIG, "system_prompt": "Be brief."}
    assert client.put("/agents/a1", json={"config": edited}).status_code == 200
    assert _sent(mocks)["procedures"] == PROCEDURES


def test_a_chat_edits_apply_sends_them(client, mocks):
    mocks.store.get_session.return_value = {
        "history": [], "config": CONFIG, "agent_id": "a1",
    }
    mocks.registry.get_backend.return_value = backend_row()
    assert client.post("/sessions/s1/apply").status_code == 200
    assert _sent(mocks)["procedures"] == PROCEDURES


def test_the_calls_inline_config_carries_them(client, mocks):
    _configured(mocks)
    _target(mocks)
    _agent_backend(mocks, config={**CONFIG, "integrations": ["google_calendar"]})
    mocks.pipedream.access_token.return_value = pipedream.AccessToken(token="pd", expires_at=1.0)

    agent = _call_config(client).json()["data"]["agent"]

    assert agent["procedures"] == PROCEDURES


# --- TurnCall's refusal -------------------------------------------------------


def _refusal(body: dict, sent: dict | None = None) -> httpx.HTTPStatusError:
    """TurnCall's 422 for the request that carried `sent`, the agent body the
    mapper built."""
    request = httpx.Request("PUT", "http://turncall/v1/agents/a1", json=sent or {})
    response = httpx.Response(422, json=body, request=request)
    return httpx.HTTPStatusError("422", request=request, response=response)


def _graph_refusal(message: str) -> dict:
    return {
        "success": False, "error": "Validation error", "code": "validation_error",
        "details": {"errors": [{"loc": ["body", "config"], "msg": f"Value error, {message}"}]},
    }


def _refuse_the_edit(mocks, message: str) -> None:
    mocks.registry.get_backend.return_value = backend_row()
    sent = to_create_agent_request(CONFIG, tools_base_url="http://b")
    mocks.client.update_agent.side_effect = _refusal(_graph_refusal(message), sent)


def test_a_refusal_naming_a_step_names_its_index(client, mocks):
    message = (
        "procedures[0] 'no_internet': step 'restart' moves to 'technican', "
        "which does not exist"
    )
    _refuse_the_edit(mocks, message)

    r = client.put("/agents/a1", json={"config": CONFIG})

    assert r.status_code == 422
    assert r.json()["errors"] == [{
        "field": "procedures[0].steps[1]", "message": message,
        "procedure": "no_internet", "step": "restart",
    }]
    mocks.registry.update_config.assert_not_called()


def test_a_refusal_of_the_whole_procedure_names_it(client, mocks):
    message = "procedures[0] 'no_internet': no step has start: true"
    _refuse_the_edit(mocks, message)

    r = client.put("/agents/a1", json={"config": CONFIG})

    assert r.json()["errors"] == [
        {"field": "procedures[0]", "message": message, "procedure": "no_internet"}
    ]


def test_a_step_the_sent_config_lacks_stays_on_the_procedure(client, mocks):
    message = "procedures[0] 'no_internet': step 'gone' is unreachable"
    _refuse_the_edit(mocks, message)

    r = client.put("/agents/a1", json={"config": CONFIG})

    assert r.json()["errors"] == [{
        "field": "procedures[0]", "message": message,
        "procedure": "no_internet", "step": "gone",
    }]


def test_a_schema_refusal_inside_a_step_names_its_path(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.update_agent.side_effect = _refusal({
        "success": False, "error": "Validation error", "code": "validation_error",
        "details": {"errors": [{
            "loc": ["body", "config", "procedures", 0, "steps", 3, "escalate",
                    "transfer_call", "target_number"],
            "msg": "String should match pattern '^\\+[1-9]\\d{1,14}$'",
        }]},
    })
    r = client.put("/agents/a1", json={"config": CONFIG})
    assert r.json()["errors"] == [{
        "field": "procedures[0].steps[3].escalate.transfer_call.target_number",
        "message": "String should match pattern '^\\+[1-9]\\d{1,14}$'",
    }]


def test_create_answers_the_refusal_naming_the_step(client, mocks):
    _wire_create_success(mocks)
    session = mocks.store.get_session.return_value
    mocks.store.get_session.return_value = {**session, "config": {**session["config"], **CONFIG}}
    message = "procedures[0] 'no_internet': step 'lights' names tool 'line_status', which the agent does not have"
    mocks.client.create_agent.side_effect = _refusal(
        _graph_refusal(message), {"name": "A", "config": {"procedures": PROCEDURES}}
    )

    r = client.post("/sessions/s1/create")

    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "procedures[0].steps[0]"


# --- a call's summary ---------------------------------------------------------

SUMMARY = [{
    "name": "no_internet", "path": ["lights", "restart", "technician"],
    "completed": False, "ended_at": "technician", "escalated_at": "technician",
    "refused_moves": 1,
}]


def test_a_calls_read_carries_its_procedures_beside_its_skills(client, mocks):
    """The Console reads a call's `procedures` where it reads its `skills`:
    TurnCall's call read, proxied as it is."""
    mocks.registry.get_backend.return_value = backend_row()
    call = {"id": "c1", "skills": [], "procedures": SUMMARY}
    mocks.client.get_call.return_value = call
    mocks.client.list_calls.return_value = {"success": True, "data": [call]}

    one = client.get("/agents/a1/calls/c1").json()["data"]
    listed = client.get("/agents/a1/calls").json()["data"][0]

    assert one["procedures"] == listed["procedures"] == SUMMARY


def test_create_sends_the_attached_apps_a_step_tool_names(client, mocks):
    """Create maps attached Connected apps to MCP servers, as an update does:
    without them TurnCall refuses a step whose tool is an app's."""
    _wire_create_success(mocks)
    _target(mocks)
    session = mocks.store.get_session.return_value
    session["config"] = {
        **session["config"],
        "integrations": [{"app": "google_calendar", "mid_call": True}],
        "procedures": PROCEDURES,
    }
    assert client.post("/sessions/s1/create").status_code == 200
    sent = mocks.client.create_agent.await_args.args[0]["config"]
    assert [s["headers"]["x-pd-app-slug"] for s in sent["mcp_servers"]] == ["google_calendar"]
