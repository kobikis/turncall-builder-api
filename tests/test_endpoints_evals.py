"""Per-agent eval endpoints (proxy TurnCall's evals — builder-api #42).

TurnCall owns evals; this router puts a workspace boundary and a session cookie
in front of the agent's API key. So what is worth testing here is the seam, not
the engine: that the agent's key is used, that a viewer can read and cannot
start a run, that the target is the agent whose page it is, and that TurnCall's
own error text survives the trip.

That last one is the point of the router. A scenario rejected by pipecat's
parser comes back naming the offending field, and replacing it with "scenario
failed" would make the Console worse than curl at the one moment an author
needs help.

Proxy ops run under `client` (conftest overrides the gate to an admin). Role
denials use `gate_client`, which runs the real gate.
"""

import httpx
from app.routers.auth import COOKIE_NAME as COOKIE

from tests.conftest import TEST_WORKSPACE_ID, backend_row

WS_HEADER = {"X-Workspace-Id": TEST_WORKSPACE_ID}
SCENARIO = {
    "name": "asks-for-the-reference",
    "definition": {"turns": [{"user": "hi", "expect": [{"event": "llm_response"}]}]},
}


def _turncall_rejects(status: int, error: str) -> httpx.HTTPStatusError:
    """TurnCall's error envelope, as httpx surfaces it."""
    request = httpx.Request("POST", "http://turncall/v1/eval-scenarios")
    response = httpx.Response(
        status, request=request, json={"success": False, "error": error}
    )
    return httpx.HTTPStatusError(error, request=request, response=response)


def _login_as(mocks, role: str) -> None:
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role


# ---- reading ---------------------------------------------------------------


def test_scenarios_proxy_the_agents_project(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_eval_scenarios.return_value = {
        "success": True,
        "data": [{"id": "s1", "name": "asks-for-the-reference"}],
    }
    r = client.get("/agents/a1/eval-scenarios")
    assert r.status_code == 200
    assert r.json()["data"][0]["name"] == "asks-for-the-reference"


def test_no_backend_is_a_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.get("/agents/nope/eval-scenarios").status_code == 404


def test_a_batch_reads_back_without_being_mistaken_for_a_run(client, mocks):
    """`/eval-runs/batches/{id}` is declared before `/eval-runs/{run_id}`, so
    the literal segment is not parsed as a run id."""
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_eval_batch.return_value = {"batch_id": "b1", "status": "passed"}
    r = client.get("/agents/a1/eval-runs/batches/b1")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "passed"
    mocks.client.get_eval_batch.assert_awaited_once()
    mocks.client.get_eval_run.assert_not_awaited()


# ---- running ---------------------------------------------------------------


def test_a_run_targets_the_agent_whose_page_this_is(client, mocks):
    """The Console never asks which agent to run against: the answer is always
    the one you are looking at."""
    mocks.registry.get_backend.return_value = backend_row(agent_id="agent-42")
    mocks.client.create_eval_run.return_value = {"batch_id": "b1", "runs": []}
    r = client.post("/agents/a1/eval-runs", json={"scenario_id": "s1"})
    assert r.status_code == 202, "a worker executes it; this is not a verdict"
    payload = mocks.client.create_eval_run.await_args.args[1]
    assert payload["target"] == {"type": "agent", "agent_id": "agent-42"}


def test_a_tag_run_returns_every_run_it_created(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.create_eval_run.return_value = {
        "batch_id": "b1",
        "runs": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}],
    }
    r = client.post("/agents/a1/eval-runs", json={"tag": "pre-publish"})
    assert r.status_code == 202
    assert len(r.json()["data"]["runs"]) == 3
    assert r.json()["data"]["batch_id"] == "b1"


# ---- TurnCall's errors -----------------------------------------------------


def test_a_rejected_scenario_says_which_field(client, mocks):
    """The whole reason this router exists rather than a bare passthrough."""
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.create_eval_scenario.side_effect = _turncall_rejects(
        422, "'turns:' must be a list"
    )
    r = client.post("/agents/a1/eval-scenarios", json=SCENARIO)
    assert r.status_code == 422
    assert r.json()["detail"] == "'turns:' must be a list"


def test_an_empty_tag_reports_turncalls_refusal(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.create_eval_run.side_effect = _turncall_rejects(
        400, "no scenarios carry the tag 'pre-publsh'"
    )
    r = client.post("/agents/a1/eval-runs", json={"tag": "pre-publsh"})
    assert r.status_code == 400
    assert "pre-publsh" in r.json()["detail"], "the typo is the useful part"


def test_a_non_json_error_still_reports_something_usable(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    request = httpx.Request("POST", "http://turncall/v1/eval-runs")
    response = httpx.Response(502, request=request, text="<html>bad gateway</html>")
    mocks.client.create_eval_run.side_effect = httpx.HTTPStatusError(
        "boom", request=request, response=response
    )
    r = client.post("/agents/a1/eval-runs", json={"scenario_id": "s1"})
    assert r.status_code == 502
    assert r.json()["detail"] == "could not queue the run"


# ---- deriving from a call --------------------------------------------------


def test_from_call_defaults_to_review_not_save(client, mocks):
    """A derived scenario asserts whatever the agent did on that call, mistakes
    included, so nothing is stored until a human has looked."""
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.scenario_from_call.return_value = {"saved": False, "name": "call-abc"}
    r = client.post("/agents/a1/eval-scenarios/from-call", json={"call_id": "c1"})
    assert r.status_code == 200
    assert mocks.client.scenario_from_call.await_args.args[1]["save"] is False


def test_from_call_passes_an_explicit_save_through(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.scenario_from_call.return_value = {"saved": True}
    client.post(
        "/agents/a1/eval-scenarios/from-call", json={"call_id": "c1", "save": True}
    )
    assert mocks.client.scenario_from_call.await_args.args[1]["save"] is True


def test_a_call_that_cannot_convert_says_why(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.scenario_from_call.side_effect = _turncall_rejects(
        400, "the call has no caller speech to build turns from"
    )
    r = client.post("/agents/a1/eval-scenarios/from-call", json={"call_id": "c1"})
    assert r.status_code == 400
    assert "no caller speech" in r.json()["detail"]


# ---- RBAC ------------------------------------------------------------------


def test_a_viewer_can_read_the_scenarios(gate_client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_eval_scenarios.return_value = {"success": True, "data": []}
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.get("/agents/a1/eval-scenarios", headers=WS_HEADER)
    assert r.status_code == 200


def test_a_viewer_cannot_start_a_run(gate_client, mocks):
    """Running spends model time, so it is an editor action like every other
    one that costs something."""
    mocks.registry.get_backend.return_value = backend_row()
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post(
        "/agents/a1/eval-runs", json={"scenario_id": "s1"}, headers=WS_HEADER
    )
    assert r.status_code == 403
    mocks.client.create_eval_run.assert_not_called()


def test_a_viewer_cannot_derive_a_scenario_from_a_call(gate_client, mocks):
    """It reads a call's transcript and its tool results — more than a
    viewer's read of the call list."""
    mocks.registry.get_backend.return_value = backend_row()
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.post(
        "/agents/a1/eval-scenarios/from-call",
        json={"call_id": "c1"},
        headers=WS_HEADER,
    )
    assert r.status_code == 403
    mocks.client.scenario_from_call.assert_not_called()


def test_a_viewer_cannot_delete_a_scenario(gate_client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    _login_as(mocks, "viewer")
    gate_client.cookies.set(COOKIE, "live")
    r = gate_client.delete("/agents/a1/eval-scenarios/s1", headers=WS_HEADER)
    assert r.status_code == 403
    mocks.client.delete_eval_scenario.assert_not_called()
