"""Agent endpoints: list, get, create saga, update, start, delete."""


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
