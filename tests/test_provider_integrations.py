"""Provider Integrations on the TestClient seam (ADR-0023, #55-#58).

Projection is observed as calls on the mocked TurnCall client: what a Workspace
admin's write causes the engine to be asked. The stores are autospecced; their
SQL is covered by test_integration_store_db.py.
"""

from __future__ import annotations

import pytest

from app import provider_catalog
from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, backend_row

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
KEY = "dg-secret-key-1234"
V1 = "11111111-1111-1111-1111-111111111111"

ENGINE = {
    "roles": {"stt": ["deepgram", "openai"], "llm": ["openai", "ollama"], "tts": ["deepgram"]},
    "accounts": {
        "deepgram": {"serves": {"stt": ["deepgram"], "tts": ["deepgram"]}, "platform_key": True, "project_credential": False},
        "openai": {"serves": {"stt": ["openai"], "llm": ["openai"]}, "platform_key": False, "project_credential": False},
        "cartesia": {"serves": {"tts": ["cartesia"]}, "platform_key": False, "project_credential": False},
    },
}


def _integration(provider="deepgram", **over):
    row = {
        "provider": provider,
        "last4": "1234",
        "validation": {"status": "valid", "checked": "GET https://api.deepgram.com/v1/projects"},
        "version": V1,
        "disconnecting": False,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    row.update(over)
    return row


@pytest.fixture
def engine(mocks):
    mocks.phones.ensure_webhook_project.return_value = {"project_id": "wp", "api_key": "tc_wp"}
    mocks.client.list_providers.return_value = ENGINE
    return mocks


@pytest.fixture
def probe(monkeypatch):
    result = {"status": "valid", "checked": "GET https://api.deepgram.com/v1/projects", "detail": "ok"}

    async def fake(provider, document):
        fake.calls.append((provider, document))
        return result

    fake.calls = []
    fake.result = result
    monkeypatch.setattr(provider_catalog, "probe", fake)
    return fake


def _two_agents(mocks, **second):
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_a1"),
        backend_row(agent_id="a2", api_key="tc_a2", **second),
    ]


# --- connecting -----------------------------------------------------------------


def test_connect_stores_and_projects_into_every_agent(client, engine, probe):
    _two_agents(engine)
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}

    r = client.put("/integrations/providers/deepgram", json={"api_key": f"  {KEY} "}, headers=WS)

    assert r.status_code == 200, r.text
    assert KEY not in r.text
    kw = engine.integration_store.save.await_args.kwargs
    assert kw["document"] == {"api_key": KEY}
    assert kw["last4"] == "1234"
    calls = engine.client.put_credential.await_args_list
    assert [c.args for c in calls] == [
        ("deepgram", {"api_key": KEY}, "tc_a1"),
        ("deepgram", {"api_key": KEY}, "tc_a2"),
    ]
    assert [a["state"] for a in r.json()["data"]["agents"]] == ["applied", "applied"]


def test_a_rejected_key_is_not_stored(client, engine, probe):
    probe.result.update(status="rejected", detail="deepgram answered 401")
    r = client.put("/integrations/providers/deepgram", json={"api_key": "bad"}, headers=WS)
    assert r.status_code == 400
    assert "not saved" in r.json()["detail"]
    engine.integration_store.save.assert_not_called()
    engine.client.put_credential.assert_not_called()


def test_an_unreachable_provider_is_stored_and_says_so(client, engine, probe):
    probe.result.update(status="unverified", detail="unreachable: ConnectError")
    engine.registry.list_backends.return_value = []
    engine.integration_store.save.return_value = _integration(validation=probe.result)
    r = client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    assert r.status_code == 200
    assert engine.integration_store.save.await_args.kwargs["validation"]["status"] == "unverified"


def test_unknown_provider_is_named(client, engine, probe):
    r = client.put("/integrations/providers/ollama", json={"api_key": "k"}, headers=WS)
    assert r.status_code == 422
    assert "'ollama'" in r.json()["detail"]


def test_malformed_body_is_422(client, engine, probe):
    r = client.put("/integrations/providers/deepgram", json={"key": "k"}, headers=WS)
    assert r.status_code == 422
    engine.integration_store.save.assert_not_called()


def test_a_pinned_agent_still_receives_the_workspace_key(client, engine, probe):
    """Its own key covers only its LLM; its voice on the same account must not
    fall to the platform key. It is recorded `pinned` so the card can say so."""
    engine.registry.list_backends.return_value = [
        backend_row(agent_id="a1", api_key="tc_a1"),
        backend_row(
            agent_id="pinned",
            api_key="tc_p",
            config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}},
        ),
    ]
    engine.integration_store.save.return_value = _integration("openai")
    engine.integration_store.get.return_value = _integration("openai")
    engine.integration_store.get_document.return_value = {"api_key": "sk-ws"}

    r = client.put("/integrations/providers/openai", json={"api_key": "sk-ws"}, headers=WS)

    assert [c.args[2] for c in engine.client.put_credential.await_args_list] == ["tc_a1", "tc_p"]
    states = {a["agent_id"]: a["state"] for a in r.json()["data"]["agents"]}
    assert states == {"a1": "applied", "pinned": "pinned"}


def test_a_credential_change_never_regenerates_a_backend(client, engine, probe):
    _two_agents(engine)
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}
    client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    engine.generator.materialize_and_run.assert_not_called()
    engine.registry.set_status.assert_not_called()


# --- retrying ---------------------------------------------------------------------


def test_partial_failure_is_recorded_and_the_retry_finishes_it(client, engine, probe):
    _two_agents(engine)
    engine.integration_store.save.return_value = _integration()
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.get_document.return_value = {"api_key": KEY}
    engine.client.put_credential.side_effect = [None, RuntimeError("turncall down")]

    r = client.put("/integrations/providers/deepgram", json={"api_key": KEY}, headers=WS)
    states = [(a["agent_id"], a["state"]) for a in r.json()["data"]["agents"]]
    assert states == [("a1", "applied"), ("a2", "failed")]

    # The log now says a1 holds this version; only a2 is written again.
    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "applied", "version": V1},
        {"agent_id": "a2", "state": "failed", "version": V1},
    ]
    engine.client.put_credential.reset_mock(side_effect=True)
    r = client.post("/integrations/providers/deepgram/apply", headers=WS)
    assert r.status_code == 200
    assert [c.args[2] for c in engine.client.put_credential.await_args_list] == ["tc_a2"]


def test_rerunning_a_complete_projection_writes_nothing(client, engine):
    _two_agents(engine)
    engine.integration_store.get.return_value = _integration()
    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "applied", "version": V1},
        {"agent_id": "a2", "state": "applied", "version": V1},
    ]
    r = client.post("/integrations/providers/deepgram/apply", headers=WS)
    assert r.status_code == 200
    engine.client.put_credential.assert_not_called()


# --- disconnecting ------------------------------------------------------------------


def test_disconnect_removes_from_every_agent_before_the_record(client, engine):
    _two_agents(engine)
    engine.integration_store.get.return_value = _integration()
    order = []
    engine.client.delete_credential.side_effect = lambda p, key: order.append(("engine", key))
    engine.integration_store.delete.side_effect = lambda *a: order.append(("record",))

    r = client.delete("/integrations/providers/deepgram", headers=WS)

    assert r.json()["data"]["complete"] is True
    assert order == [("engine", "tc_a1"), ("engine", "tc_a2"), ("record",)]


def test_an_interrupted_disconnect_keeps_the_record_and_finishes_on_retry(client, engine):
    _two_agents(engine)
    engine.integration_store.get.return_value = _integration()
    engine.client.delete_credential.side_effect = [None, RuntimeError("timeout")]

    r = client.delete("/integrations/providers/deepgram", headers=WS)

    data = r.json()["data"]
    assert data["complete"] is False
    assert [a["state"] for a in data["agents"]] == ["removed", "remove_failed"]
    engine.integration_store.set_disconnecting.assert_awaited()
    engine.integration_store.delete.assert_not_called()

    engine.integration_store.list_projections.return_value = [
        {"agent_id": "a1", "state": "removed", "version": None},
    ]
    engine.client.delete_credential.reset_mock(side_effect=True)
    r = client.delete("/integrations/providers/deepgram", headers=WS)
    assert r.json()["data"]["complete"] is True
    assert [c.args[1] for c in engine.client.delete_credential.await_args_list] == ["tc_a2"]
    engine.integration_store.delete.assert_awaited_once()


def test_disconnect_never_touches_a_pinned_agents_config(client, engine):
    pinned = backend_row(
        agent_id="p", api_key="tc_p", config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}}
    )
    engine.registry.list_backends.return_value = [pinned]
    engine.integration_store.get.return_value = _integration("openai")
    client.delete("/integrations/providers/openai", headers=WS)
    engine.client.update_agent.assert_not_called()


# --- cards: which key runs the call --------------------------------------------------


def test_cards_say_which_key_runs_the_call(client, engine):
    engine.registry.list_backends.return_value = [
        backend_row(agent_id="a1", config={"name": "One", "tts": {"provider": "deepgram"}}),
        backend_row(agent_id="p", config={"name": "Pin", "llm": {"provider": "openai", "api_key": "sk-own"}}),
    ]
    engine.integration_store.list_for_workspace.return_value = [_integration("openai", last4="wxyz")]

    r = client.get("/integrations/providers", headers=WS)

    assert r.status_code == 200
    assert "sk-own" not in r.text
    cards = {c["provider"]: c for c in r.json()["data"]["providers"]}
    assert cards["openai"]["runs_on"] == "workspace"
    assert cards["openai"]["last4"] == "wxyz"
    assert cards["openai"]["pinned_agents"] == [{"agent_id": "p", "name": "Pin"}]
    assert cards["deepgram"]["runs_on"] == "platform"  # no Integration, platform key
    assert cards["deepgram"]["agents_using"] == 2  # default STT/TTS on both
    assert cards["cartesia"]["runs_on"] == "none"  # neither: calls will fail
    assert cards["deepgram"]["serves"] == {"stt": ["deepgram"], "tts": ["deepgram"]}


# --- adoption -------------------------------------------------------------------------


def test_adopt_moves_the_agents_key_into_the_workspace(client, engine, probe):
    pinned = backend_row(
        agent_id="p", api_key="tc_p",
        config={"name": "P", "llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-own"}},
    )
    engine.registry.get_backend.return_value = pinned
    engine.registry.list_backends.return_value = [backend_row(agent_id="p", api_key="tc_p")]
    # None before adoption, the new Integration once saved.
    engine.integration_store.get.side_effect = [None, _integration("openai")]
    engine.integration_store.get_document.return_value = {"api_key": "sk-own"}

    r = client.post("/integrations/providers/openai/adopt", json={"agent_id": "p"}, headers=WS)

    assert r.status_code == 200, r.text
    assert engine.integration_store.save.await_args.kwargs["document"] == {"api_key": "sk-own"}
    pushed = engine.client.update_agent.await_args.args[1]["config"]["llm"]
    assert "api_key" not in pushed
    engine.client.put_credential.assert_awaited_with("openai", {"api_key": "sk-own"}, "tc_p")


def test_adopt_refuses_an_agent_without_its_own_key(client, engine):
    engine.registry.get_backend.return_value = backend_row(agent_id="a1")
    r = client.post("/integrations/providers/openai/adopt", json={"agent_id": "a1"}, headers=WS)
    assert r.status_code == 409
    engine.integration_store.save.assert_not_called()


# --- roles ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/integrations/providers/deepgram", {"api_key": "k"}),
        ("delete", "/integrations/providers/deepgram", None),
        ("post", "/integrations/providers/deepgram/apply", None),
        ("post", "/integrations/providers/openai/adopt", {"agent_id": "a1"}),
    ],
)
def test_editors_cannot_change_a_credential(gate_client, engine, method, path, body):
    engine.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    engine.auth_store.resolve_membership.return_value = "editor"
    gate_client.cookies.set(COOKIE, "live")
    kwargs = {"json": body} if body is not None else {}
    r = getattr(gate_client, method)(path, headers=WS, **kwargs)
    assert r.status_code == 403
    engine.integration_store.save.assert_not_called()


def test_a_viewer_can_read_presence(gate_client, engine):
    engine.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    engine.auth_store.resolve_membership.return_value = "viewer"
    engine.registry.list_backends.return_value = []
    gate_client.cookies.set(COOKIE, "live")
    assert gate_client.get("/integrations/providers", headers=WS).status_code == 200


# --- new agents and the pickers ---------------------------------------------------------


def test_a_new_agent_gets_every_connected_credential(engine):
    import asyncio

    from app import provider_integrations

    engine.integration_store.list_for_workspace.return_value = [
        _integration("deepgram"),
        _integration("openai", disconnecting=True),
    ]
    engine.integration_store.get_document.return_value = {"api_key": KEY}
    asyncio.run(
        provider_integrations.project_into_new_agent(
            object(), engine.client, TEST_WORKSPACE_ID, {"agent_id": "new", "api_key": "tc_new", "config": {}}
        )
    )
    engine.client.put_credential.assert_awaited_once_with("deepgram", {"api_key": KEY}, "tc_new")


def test_pickers_list_with_the_workspace_key(client, mocks, monkeypatch):
    mocks.integration_store.api_keys.return_value = {"openai": "sk-ws"}
    seen = []

    async def fetch(c, provider, key):
        seen.append(key)
        return ["gpt-x"]

    monkeypatch.setattr(provider_catalog, "_fetch_llm_models", fetch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-builder-env")
    r = client.get("/providers/llm/openai/models", headers=WS)
    assert r.json()["data"]["models"] == ["gpt-x"]
    assert seen == ["sk-ws"]

    # Removed: the next read falls back to the Builder's env under a new cache key.
    mocks.integration_store.api_keys.return_value = {}
    client.get("/providers/llm/openai/models", headers=WS)
    assert seen == ["sk-ws", "sk-builder-env"]


def test_adopt_never_overwrites_a_workspace_key(client, engine, probe):
    engine.registry.get_backend.return_value = backend_row(
        agent_id="p", config={"name": "P", "llm": {"provider": "openai", "api_key": "sk-own"}}
    )
    engine.integration_store.get.return_value = _integration("openai")
    r = client.post("/integrations/providers/openai/adopt", json={"agent_id": "p"}, headers=WS)
    assert r.status_code == 409
    engine.integration_store.save.assert_not_called()


def test_reapply_is_refused_while_a_disconnect_is_unfinished(client, engine):
    engine.integration_store.get.return_value = _integration(disconnecting=True)
    r = client.post("/integrations/providers/deepgram/apply", headers=WS)
    assert r.status_code == 409
    engine.client.put_credential.assert_not_called()
