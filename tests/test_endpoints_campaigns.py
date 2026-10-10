"""An agent's campaigns (#75): outbound calls to a list, through its project."""

import httpx
import pytest

from tests.conftest import backend_row

NUMBER = {"id": "pn1", "e164": "+15550001111", "agent_id": "a1", "project_id": "p1"}
CAMPAIGN = {
    "name": "Renewals",
    "from_number_id": "pn1",
    "timezone": "America/New_York",
    "calling_window": {"start": "09:00", "end": "17:00", "days": ["mon", "tue"]},
    "retry_policy": {"customer_did_not_answer": {"max_attempts": 3, "spacing_seconds": 3600}},
    "targets": [{"number": "+15552223333", "variables": {"name": "Jane"}}],
}


def _refuses(status, body, method="POST"):
    resp = httpx.Response(status, json=body, request=httpx.Request(method, "http://tc/x"))
    return httpx.HTTPStatusError("refused", request=resp.request, response=resp)


@pytest.fixture
def agent(mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.phones.get_number.return_value = NUMBER
    return mocks


def test_a_campaign_is_created_as_the_agent(client, agent):
    agent.client.create_campaign.return_value = {"id": "c1", "status": "queued"}
    r = client.post("/agents/a1/campaigns", json=CAMPAIGN)
    assert r.status_code == 201
    assert r.json()["data"]["id"] == "c1"
    body, key = agent.client.create_campaign.await_args.args
    assert body == {**CAMPAIGN, "agent_id": "a1"}
    assert key == "tc_key"


def test_weighted_agents_are_not_offered(client, agent):
    r = client.post("/agents/a1/campaigns", json={**CAMPAIGN, "agents": [{"agent_id": "a1", "weight": 100}]})
    assert r.status_code == 422
    agent.client.create_campaign.assert_not_awaited()


def test_a_campaign_dials_from_the_agents_own_number(client, agent):
    agent.phones.get_number.return_value = {**NUMBER, "agent_id": "other", "project_id": "p9"}
    r = client.post("/agents/a1/campaigns", json=CAMPAIGN)
    assert r.status_code == 422
    agent.client.create_campaign.assert_not_awaited()


def test_a_target_turncall_refuses_is_named_by_its_row(client, agent):
    agent.client.create_campaign.side_effect = _refuses(422, {
        "success": False, "error": "Validation error",
        "details": {"errors": [{
            "loc": ["body", "targets", 0, "variables"],
            "msg": "Value error, a target takes at most 50 variables",
        }]},
    })
    r = client.post("/agents/a1/campaigns", json=CAMPAIGN)
    assert r.status_code == 422
    assert "targets.0.variables" in r.json()["detail"]


def test_campaigns_and_their_targets_are_listed(client, agent):
    agent.client.list_campaigns.return_value = {"success": True, "data": [{"id": "c1"}], "total": 1}
    agent.client.get_campaign.return_value = {"id": "c1", "targets": {"pending": 1}}
    agent.client.list_campaign_targets.return_value = {"success": True, "data": [{"id": "t1"}], "total": 1}

    assert client.get("/agents/a1/campaigns").json()["data"] == [{"id": "c1"}]
    agent.client.list_campaigns.assert_awaited_once_with("tc_key", page=1, limit=20)
    assert client.get("/agents/a1/campaigns/c1").json()["data"]["id"] == "c1"
    r = client.get("/agents/a1/campaigns/c1/targets?status=failed&page=2")
    assert r.json()["data"] == [{"id": "t1"}]
    agent.client.list_campaign_targets.assert_awaited_once_with(
        "c1", "tc_key", status="failed", page=2, limit=100
    )


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_a_campaign_is_paused_resumed_or_cancelled(client, agent, action):
    agent.client.control_campaign.return_value = {"id": "c1", "status": "paused"}
    r = client.post(f"/agents/a1/campaigns/c1/{action}")
    assert r.status_code == 200
    agent.client.control_campaign.assert_awaited_once_with("c1", action, "tc_key")


def test_an_impossible_move_is_turncalls_409(client, agent):
    agent.client.control_campaign.side_effect = _refuses(409, {
        "success": False, "code": "invalid_state_transition", "error": "campaign is completed; cannot pause",
    })
    r = client.post("/agents/a1/campaigns/c1/pause")
    assert r.status_code == 409
    assert r.json()["detail"] == "campaign is completed; cannot pause"


def test_an_unknown_action_is_refused(client, agent):
    assert client.post("/agents/a1/campaigns/c1/restart").status_code == 422
    agent.client.control_campaign.assert_not_awaited()


def test_one_call_is_read_for_a_targets_link(client, agent):
    agent.client.get_call.return_value = {"id": "call-1", "status": "completed"}
    r = client.get("/agents/a1/calls/call-1")
    assert r.status_code == 200
    assert r.json()["data"]["id"] == "call-1"
    agent.client.get_call.assert_awaited_once_with("call-1", "tc_key")


def test_a_mid_call_app_agent_campaigns_only_from_a_caller_info_number(client, agent):
    """Its calls would otherwise all run without the app's tools (#80)."""
    agent.registry.get_backend.return_value = backend_row(
        config={"name": "Agent", "system_prompt": "hi", "integrations": ["slack_v2"]}
    )
    agent.phones.get_number.return_value = {**NUMBER, "routing_type": "agent"}
    r = client.post("/agents/a1/campaigns", json=CAMPAIGN)
    assert r.status_code == 422
    assert "caller info" in r.json()["detail"]
    agent.client.create_campaign.assert_not_awaited()
