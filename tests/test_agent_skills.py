"""An agent's Skills in its Config (#133): `skills` and
`max_loaded_skills` reach TurnCall untouched on every path that pushes a
config, and TurnCall's refusal of one entry comes back naming it."""

from __future__ import annotations

import httpx

from app import pipedream
from app.mapper import to_create_agent_request
from tests.conftest import backend_row
from tests.test_endpoints_agents import _wire_create_success
from tests.test_integrations import _agent_backend, _call_config, _configured, _target

SKILLS = [
    {"skill": "verify_caller"},
    {"skill": "explain_bill", "version_id": "5b0c8a4e-2a43-4d3b-9c1f-1d2e3f4a5b6c"},
]
CONFIG = {"name": "A", "system_prompt": "hi", "skills": SKILLS, "max_loaded_skills": 2}


def _sent(mocks) -> dict:
    return mocks.client.update_agent.await_args.args[1]["config"]


def test_the_mapper_sends_them_as_written():
    config = to_create_agent_request(CONFIG, tools_base_url="http://b")["config"]
    assert config["skills"] == SKILLS
    assert config["max_loaded_skills"] == 2


def test_an_agent_without_them_sends_neither():
    config = to_create_agent_request(
        {"name": "A", "system_prompt": "hi", "skills": None}, tools_base_url="http://b"
    )["config"]
    assert "skills" not in config
    assert "max_loaded_skills" not in config


def test_create_sends_them(client, mocks):
    _wire_create_success(mocks)
    session = mocks.store.get_session.return_value
    mocks.store.get_session.return_value = {**session, "config": {**session["config"], **CONFIG}}
    assert client.post("/sessions/s1/create").status_code == 200
    sent = mocks.client.create_agent.await_args.args[0]["config"]
    assert (sent["skills"], sent["max_loaded_skills"]) == (SKILLS, 2)


def test_an_edit_sends_them_and_stores_them(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    assert client.put("/agents/a1", json={"config": CONFIG}).status_code == 200
    assert _sent(mocks)["skills"] == SKILLS
    assert _sent(mocks)["max_loaded_skills"] == 2


def test_an_unrelated_save_keeps_them(client, mocks):
    """The Console edits the prompt on an agent that lists Skills: the
    whole config goes back, so they are still there."""
    mocks.registry.get_backend.return_value = backend_row(config=CONFIG)
    edited = {**CONFIG, "system_prompt": "Be brief."}
    assert client.put("/agents/a1", json={"config": edited}).status_code == 200
    assert _sent(mocks)["skills"] == SKILLS


def test_the_calls_inline_config_carries_them(client, mocks):
    _configured(mocks)
    _target(mocks)
    _agent_backend(mocks, config={**CONFIG, "integrations": ["google_calendar"]})
    mocks.pipedream.access_token.return_value = pipedream.AccessToken(token="pd", expires_at=1.0)

    agent = _call_config(client).json()["data"]["agent"]

    assert agent["skills"] == SKILLS
    assert agent["max_loaded_skills"] == 2


def _refusal(body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "http://turncall/v1/agents/a1")
    response = httpx.Response(422, json=body, request=request)
    return httpx.HTTPStatusError("422", request=request, response=response)


def test_turncalls_refusal_of_one_entry_names_it(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    message = (
        "skills[1] 'explain_bill' tool 'get_invoices' collides with the agent's own tool "
        "'get_invoices'"
    )
    mocks.client.update_agent.side_effect = _refusal({
        "success": False,
        "error": message,
        "code": "invalid_agent_config",
        "details": {
            "field": "skills[1]", "skill": "explain_bill", "tool": "get_invoices",
            "collides_with": "get_invoices",
        },
    })

    r = client.put("/agents/a1", json={"config": CONFIG})

    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "invalid_agent_config"
    assert body["errors"] == [{
        "field": "skills[1]", "message": message, "skill": "explain_bill",
        "tool": "get_invoices", "collides_with": "get_invoices",
    }]
    mocks.registry.update_config.assert_not_called()


def test_a_schema_refusal_inside_an_entry_names_its_path(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.update_agent.side_effect = _refusal({
        "success": False, "error": "Validation error", "code": "validation_error",
        "details": {"errors": [{
            "loc": ["body", "config", "skills", 0, "version_id"],
            "msg": "Input should be a valid UUID",
        }]},
    })
    r = client.put("/agents/a1", json={"config": CONFIG})
    assert r.json()["errors"] == [
        {"field": "skills[0].version_id", "message": "Input should be a valid UUID"}
    ]
