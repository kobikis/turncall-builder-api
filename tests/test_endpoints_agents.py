"""Agent endpoints: list, get, create saga, update, start, delete."""


import httpx

from tests.conftest import backend_row


def test_list_agents(client, mocks):
    mocks.registry.list_backends.return_value = [backend_row()]
    mocks.generator.running_container_names.return_value = {"turncall-agent-agent-1"}
    r = client.get("/agents")
    assert r.status_code == 200
    agents = r.json()["data"]["agents"]
    assert agents[0]["agent_id"] == "a1"


def test_get_agent_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.get("/agents/nope").status_code == 404


def test_get_agent_ok_even_when_live_fetch_fails(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_agent.side_effect = RuntimeError("stale key")
    mocks.generator.running_container_names.return_value = set()
    r = client.get("/agents/a1")
    assert r.status_code == 200
    assert r.json()["data"]["turncall_agent"] is None  # best-effort, not a 500


# ---- create_agent saga (the compensation-less 5-step flow) -----------------


def _finalized_session(cfg=None):
    return {"agent_id": None, "config": cfg or {"name": "Hotel", "system_prompt": "hi"}}


def _wire_create_success(mocks):
    mocks.store.get_session.return_value = _finalized_session()
    mocks.registry.next_port.return_value = 9001
    mocks.client.create_project.return_value = "proj-1"
    mocks.client.create_api_key.return_value = "tc_key"
    mocks.client.register_webhook.return_value = {"secret": "wh" * 8}
    mocks.client.create_agent.return_value = {"id": "agent-1"}
    mocks.registry.record_backend.return_value = backend_row(agent_id="agent-1", slug="hotel")
    mocks.toolgen.generate_tool_bodies.return_value = {}
    mocks.scaffold.render.return_value = {"app.py": "..."}
    mocks.generator.materialize_and_run.return_value = (True, "up")


def test_create_agent_happy_path(client, mocks):
    _wire_create_success(mocks)
    r = client.post("/sessions/s1/create")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["created"] is True
    assert data["agent_id"] == "agent-1"
    # The backend hears every event, not just the call's start and end.
    assert mocks.client.register_webhook.await_args.args[1] == ["*"]
    # Build is backgrounded — create returns immediately as 'generating'; the
    # console polls for the transition. Build outcome is tested against
    # _run_build directly (test_build_workers.py).
    assert data["backend"]["status"] == "generating"
    mocks.store.set_agent_id.assert_awaited_once()


def test_create_agent_config_not_finalized_400(client, mocks):
    mocks.store.get_session.return_value = {"agent_id": None, "config": None}
    assert client.post("/sessions/s1/create").status_code == 400


def test_create_agent_idempotent_when_already_created(client, mocks):
    mocks.store.get_session.return_value = {"agent_id": "agent-1", "config": {}}
    mocks.registry.get_backend.return_value = backend_row(agent_id="agent-1")
    r = client.post("/sessions/s1/create")
    assert r.status_code == 200
    assert r.json()["data"]["created"] is False
    mocks.client.create_project.assert_not_called()


def test_create_agent_webhook_failure_does_not_abort(client, mocks):
    # Webhook registration failing must not abort create — the agent still
    # provisions and the build is backgrounded (200, generating).
    _wire_create_success(mocks)
    mocks.client.register_webhook.side_effect = RuntimeError("webhook down")
    r = client.post("/sessions/s1/create")
    assert r.status_code == 200
    assert r.json()["data"]["backend"]["status"] == "generating"


def test_create_agent_provision_failure_rolls_back_and_502(client, mocks):
    # create_agent (remote) fails -> clean 502; rollback deletes the whole
    # dedicated project (key exists), backend generation never runs.
    _wire_create_success(mocks)
    mocks.client.create_agent.side_effect = RuntimeError("turncall 500")
    r = client.post("/sessions/s1/create")
    assert r.status_code == 502
    mocks.client.delete_project.assert_awaited_once()  # orphan project rolled back
    mocks.generator.materialize_and_run.assert_not_called()


def _refused(error: str, details: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://turncall/v1/agents")
    response = httpx.Response(
        422,
        json={"success": False, "error": error, "code": "invalid_agent_config",
              "details": details},
        request=request,
    )
    return httpx.HTTPStatusError("422", request=request, response=response)


COLLISION = (
    "skills[0] 'explain_bill' tool 'get_invoices' collides with the agent's own tool "
    "'get_invoices'"
)


def test_create_agent_refused_by_turncall_is_its_422_not_a_retry(client, mocks):
    # A config TurnCall refuses fails the same way on every retry, so it is
    # TurnCall's refusal naming the field, not "please retry"; still rolled back.
    _wire_create_success(mocks)
    mocks.client.create_agent.side_effect = _refused(COLLISION, {"field": "skills[0]"})
    r = client.post("/sessions/s1/create")
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "invalid_agent_config"
    assert body["detail"] == COLLISION
    assert body["errors"] == [{"field": "skills[0]", "message": COLLISION}]
    mocks.client.delete_project.assert_awaited_once()
    mocks.generator.materialize_and_run.assert_not_called()


def test_create_agent_rolls_back_project_when_tracking_fails(client, mocks):
    # Agent created in TurnCall but store.set_agent_id then fails -> the whole
    # dedicated project is soft-deleted (compensation), caller gets a clean 502.
    _wire_create_success(mocks)
    mocks.store.set_agent_id.side_effect = RuntimeError("db down")
    r = client.post("/sessions/s1/create")
    assert r.status_code == 502
    mocks.client.delete_project.assert_awaited_once()  # orphan project rolled back
    assert mocks.client.delete_project.await_args.args[0] == "proj-1"


# ---- update / start / delete ----------------------------------------------


def test_update_agent_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.put("/agents/nope", json={"config": {"name": "X"}}).status_code == 404


def test_update_agent_no_tool_change_skips_regen(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(
        status="running", config={"name": "A", "system_prompt": "hi"}
    )
    r = client.put("/agents/a1", json={"config": {"name": "A", "system_prompt": "hi2"}})
    assert r.status_code == 200
    assert r.json()["data"]["backend_regenerated"] is False
    mocks.generator.materialize_and_run.assert_not_called()


def test_a_save_subscribes_the_backend_to_every_event(client, mocks):
    """A backend created when it heard only call.initializing/started/ended is
    widened on its next save, by PATCH so its signing secret is kept."""
    from unittest.mock import ANY

    from app.backends import registry

    row = backend_row(status="running", config={"name": "A", "system_prompt": "hi"})
    mocks.registry.get_backend.return_value = row
    mocks.client.list_webhooks.return_value = [
        {
            "id": "w1",
            "url": f"{registry.backend_url(row['port'])}/events",
            "events": ["call.started", "call.ended", "call.initializing"],
            "active": True,
        },
        {"id": "w2", "url": "https://elsewhere/hook", "events": ["call.ended"], "active": True},
    ]
    r = client.put("/agents/a1", json={"config": {"name": "A", "system_prompt": "hi2"}})
    assert r.status_code == 200
    mocks.client.update_webhook_events.assert_awaited_once_with("w1", ["*"], ANY)


def test_a_save_survives_turncall_refusing_the_widening(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(
        status="running", config={"name": "A", "system_prompt": "hi"}
    )
    mocks.client.list_webhooks.side_effect = RuntimeError("no PATCH here")
    r = client.put("/agents/a1", json={"config": {"name": "A", "system_prompt": "hi2"}})
    assert r.status_code == 200


def test_update_agent_failed_status_forces_regen(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(status="failed")
    mocks.toolgen.generate_tool_bodies.return_value = {}
    mocks.scaffold.render.return_value = {"app.py": "..."}
    mocks.generator.materialize_and_run.return_value = (True, "up")
    r = client.put("/agents/a1", json={"config": {"name": "A", "system_prompt": "hi"}})
    assert r.status_code == 200
    assert r.json()["data"]["backend_regenerated"] is True


def test_start_agent_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.post("/agents/nope/start").status_code == 404


def test_start_agent_restarts_container(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    r = client.post("/agents/a1/start")
    assert r.status_code == 200
    # Restart is backgrounded — returns 'generating', console polls.
    assert r.json()["data"]["status"] == "generating"


def test_delete_agent_blocked_when_numbers_bound(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1")
    mocks.phones.list_numbers.return_value = [
        {"agent_id": "a1", "routing_type": "agent"}
    ]
    r = client.delete("/agents/a1")
    assert r.status_code == 409


def test_delete_agent_teardown(client, mocks):
    mocks.registry.get_backend.return_value = backend_row(agent_id="a1", project_id="p1")
    mocks.phones.list_numbers.return_value = []
    mocks.generator.teardown.return_value = (True, "down")
    r = client.delete("/agents/a1")
    assert r.status_code == 200
    # Deletes the whole dedicated project (removes agent + key + KB), no orphan.
    mocks.client.delete_project.assert_awaited_once()
    assert mocks.client.delete_project.await_args.args[0] == "p1"
    mocks.registry.mark_deleted.assert_awaited_once()


def test_update_agent_rejects_broken_config(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    r = client.put("/agents/a1", json={"config": {"custom_tools": [{"x": 1}]}})
    assert r.status_code == 422
    mocks.client.update_agent.assert_not_called()


def test_update_agent_surfaces_turncalls_refusal(client, mocks):
    """TurnCall is the authority on the agent schema; its 422 and reason must
    reach the console. It escaped as an unhandled HTTPStatusError — a bare
    500 — so an author saw "Request failed" with no hint of what to fix."""
    import httpx

    mocks.registry.get_backend.return_value = backend_row()
    response = httpx.Response(
        422,
        # TurnCall's real 422 shape: the reason lives in details.errors.
        json={
            "success": False,
            "error": "Validation error",
            "details": {
                "errors": [
                    {
                        "loc": ["body", "config", "s2s", "delegation"],
                        "msg": "Value error, s2s.delegation in responses mode needs a model",
                    }
                ]
            },
        },
        request=httpx.Request("PUT", "http://turncall/v1/agents/a1"),
    )
    mocks.client.update_agent.side_effect = httpx.HTTPStatusError(
        "422", request=response.request, response=response
    )
    r = client.put("/agents/a1", json={"config": {"name": "A", "system_prompt": "hi"}})
    assert r.status_code == 422
    assert "config.s2s.delegation: Value error, s2s.delegation in responses mode needs a model" in r.text
    mocks.registry.update_config.assert_not_called()


# ---- the backend's feeds, relayed (ADR-0025) ---------------------------------


def _relay_to(monkeypatch, handler):
    from app.routers import agents as agents_router

    real = httpx.AsyncClient
    monkeypatch.setattr(
        agents_router.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def test_backend_feed_relays_through_the_api(client, mocks, monkeypatch):
    mocks.registry.get_backend.return_value = backend_row()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"events": [{"event": "call.ended"}]}})

    _relay_to(monkeypatch, handler)
    r = client.get("/agents/a1/backend/events")
    assert r.status_code == 200
    assert r.json()["data"]["events"][0]["event"] == "call.ended"
    assert seen == ["http://host.docker.internal:9001/events"]  # registry.backend_url


def test_backend_events_can_be_one_calls(client, mocks, monkeypatch):
    """The console's live feed is per call; a backend stores every call's."""
    mocks.registry.get_backend.return_value = backend_row()
    stored = [
        {"event": "call.started", "call_id": "c1"},
        {"event": "call.started", "call_id": "c2"},
        {"event": "call.rejected", "call_id": None},
        {"event": "call.ended", "call_id": "c1"},
    ]
    _relay_to(monkeypatch, lambda _r: httpx.Response(200, json={"data": {"events": stored}}))
    r = client.get("/agents/a1/backend/events", params={"call_id": "c1"})
    assert [e["event"] for e in r.json()["data"]["events"]] == ["call.started", "call.ended"]
    assert len(client.get("/agents/a1/backend/events").json()["data"]["events"]) == 4


def test_backend_feed_refuses_other_paths(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    assert client.get("/agents/a1/backend/call-init").status_code == 422


def test_backend_feed_unreachable_is_502(client, mocks, monkeypatch):
    mocks.registry.get_backend.return_value = backend_row()

    def handler(request):
        raise httpx.ConnectError("refused")

    _relay_to(monkeypatch, handler)
    assert client.get("/agents/a1/backend/events").status_code == 502


def test_browser_url_is_the_relay_not_a_host_port(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.generator.running_container_names.return_value = set()
    r = client.get("/agents/a1")
    assert r.json()["data"]["backend"]["browser_url"] == "/agents/a1/backend"
