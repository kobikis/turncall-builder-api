"""Builder session endpoints: /meta, /sessions, messages, config."""

from unittest.mock import AsyncMock


from app.routers import sessions
from app.builder import ComposeResult


def test_meta_returns_webhook_hints(client):
    r = client.get("/meta")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "twilio_voice_webhook" in data


def test_providers_lists_stt_llm_tts_s2s(client):
    r = client.get("/providers")
    assert r.status_code == 200
    d = r.json()["data"]
    assert {"openai", "anthropic", "custom_openai", "openrouter", "ollama", "bedrock"} <= set(d["llm"])
    assert "deepgram" in d["tts"]
    assert "deepgram" in d["stt"]
    assert set(d["s2s"]) == {"openai", "google", "aws"}


def test_s2s_models_and_voices_endpoints(client, monkeypatch):
    from app.routers import sessions

    async def fake_models(provider):
        return ["gpt-4o-realtime-preview"]

    async def fake_voices(provider):
        return ["alloy", "echo"]

    monkeypatch.setattr(sessions.provider_catalog, "s2s_models", fake_models)
    monkeypatch.setattr(sessions.provider_catalog, "s2s_voices", fake_voices)
    assert client.get("/providers/s2s/openai/models").json()["data"]["models"] == ["gpt-4o-realtime-preview"]
    assert client.get("/providers/s2s/openai/voices").json()["data"]["voices"] == ["alloy", "echo"]


def test_stt_models_endpoint(client, monkeypatch):
    from app.routers import sessions

    async def fake(provider):
        return ["nova-2", "nova-3"]

    monkeypatch.setattr(sessions.provider_catalog, "stt_models", fake)
    r = client.get("/providers/stt/deepgram/models")
    assert r.status_code == 200
    assert r.json()["data"]["models"] == ["nova-2", "nova-3"]


def test_llm_models_endpoint(client, monkeypatch):
    from app.routers import sessions

    async def fake(provider):
        return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr(sessions.provider_catalog, "llm_models", fake)
    r = client.get("/providers/llm/openai/models")
    assert r.status_code == 200
    assert r.json()["data"]["models"] == ["gpt-4o", "gpt-4o-mini"]


def test_tts_voices_endpoint(client, monkeypatch):
    from app.routers import sessions

    async def fake(provider):
        return ["alloy", "echo"]

    monkeypatch.setattr(sessions.provider_catalog, "tts_voices", fake)
    r = client.get("/providers/tts/openai/voices")
    assert r.status_code == 200
    assert r.json()["data"]["voices"] == ["alloy", "echo"]


def test_create_session(client, mocks):
    mocks.store.create_session.return_value = "sess-1"
    r = client.post("/sessions")
    assert r.status_code == 200
    assert r.json()["data"]["session_id"] == "sess-1"


def test_post_message_ask(client, mocks, monkeypatch):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    monkeypatch.setattr(
        sessions, "step", AsyncMock(return_value=ComposeResult(action="ask", question="What for?"))
    )
    r = client.post("/sessions/sess-1/messages", json={"message": "hi"})
    assert r.status_code == 200
    assert r.json()["data"]["question"] == "What for?"
    mocks.store.save_history.assert_awaited_once()


def test_post_message_finalize_saves_config(client, mocks, monkeypatch):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    cfg = {"name": "A", "system_prompt": "hi"}
    monkeypatch.setattr(
        sessions, "step", AsyncMock(return_value=ComposeResult(action="finalize", agent_config=cfg))
    )
    r = client.post("/sessions/sess-1/messages", json={"message": "done"})
    assert r.status_code == 200
    mocks.store.save_config.assert_awaited_once()


def test_post_message_unknown_session_404(client, mocks):
    mocks.store.get_session.return_value = None
    r = client.post("/sessions/nope/messages", json={"message": "hi"})
    assert r.status_code == 404


def test_edit_config(client, mocks):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    r = client.put("/sessions/sess-1/config", json={"config": {"name": "X"}})
    assert r.status_code == 200
    mocks.store.save_config.assert_awaited_once()


def test_edit_config_unknown_session_404(client, mocks):
    mocks.store.get_session.return_value = None
    r = client.put("/sessions/nope/config", json={"config": {}})
    assert r.status_code == 404


# ---- chat as a grounded edit surface ---------------------------------------

def test_post_message_grounds_on_session_config(client, mocks, monkeypatch):
    cfg = {"name": "Bob", "system_prompt": "hi"}
    mocks.store.get_session.return_value = {"history": [], "config": cfg, "agent_id": None}
    fake = AsyncMock(return_value=ComposeResult(action="ask", question="?"))
    monkeypatch.setattr(sessions, "step", fake)
    client.post("/sessions/sess-1/messages", json={"message": "rename it"})
    # the builder is told to edit the current config, not rebuild
    assert fake.await_args.kwargs["current_config"] == cfg


def test_post_message_passes_queued_doc_names(client, mocks, monkeypatch):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    fake = AsyncMock(return_value=ComposeResult(action="ask", question="?"))
    monkeypatch.setattr(sessions, "step", fake)
    client.post(
        "/sessions/sess-1/messages",
        json={"message": "add room service using the menu", "doc_names": ["Menu.pdf"]},
    )
    # the builder learns the agent has this doc so it won't ask for pasted content
    assert fake.await_args.kwargs["knowledge_docs"] == ["Menu.pdf"]


def test_finalize_on_generated_agent_is_pending_not_applied(client, mocks, monkeypatch):
    mocks.store.get_session.return_value = {"history": [], "config": {}, "agent_id": "a1"}
    cfg = {"name": "A", "system_prompt": "hi"}
    monkeypatch.setattr(
        sessions, "step", AsyncMock(return_value=ComposeResult(action="finalize", agent_config=cfg))
    )
    applied = AsyncMock()
    monkeypatch.setattr(sessions.agent_update, "apply_update", applied)
    r = client.post("/sessions/sess-1/messages", json={"message": "make it friendlier"})
    assert r.json()["data"]["pending_apply"] is True  # confirm-first
    applied.assert_not_awaited()  # chat never mutates the live agent


def test_apply_pushes_to_generated_agent(client, mocks, monkeypatch):
    mocks.store.get_session.return_value = {
        "history": [], "config": {"name": "A", "system_prompt": "hi"}, "agent_id": "a1",
    }
    mocks.registry.get_backend.return_value = {"agent_id": "a1", "port": 9001, "status": "running", "config": {}}
    monkeypatch.setattr(
        sessions.agent_update, "apply_update",
        AsyncMock(return_value={"backend_regenerated": True}),
    )
    r = client.post("/sessions/sess-1/apply")
    assert r.status_code == 200
    assert r.json()["data"] == {"agent_id": "a1", "backend_regenerated": True}


def test_apply_before_generation_400(client, mocks):
    mocks.store.get_session.return_value = {"history": [], "config": {}, "agent_id": None}
    r = client.post("/sessions/sess-1/apply")
    assert r.status_code == 400


def test_create_session_from_existing_agent_seeds_config(client, mocks):
    cfg = {"name": "Reception", "system_prompt": "hi"}
    mocks.registry.get_backend.return_value = {"slug": "reception-abc", "config": cfg}
    mocks.store.get_creation_builder_choice.return_value = (None, None)
    mocks.store.create_session.return_value = "sess-9"
    r = client.post("/sessions", json={"agent_id": "a1"})
    assert r.status_code == 200
    assert r.json()["data"]["agent_id"] == "a1"
    # session seeded with the agent's config + its id so the builder can edit it
    kwargs = mocks.store.create_session.await_args.kwargs
    assert kwargs["config"] == cfg
    assert kwargs["agent_id"] == "a1"


# ---- config validation (typed shape guard) ---------------------------------


def test_edit_config_rejects_toolless_name(client, mocks):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    # a custom tool without a name breaks _normalize_config -> was a raw 500
    r = client.put(
        "/sessions/s1/config",
        json={"config": {"custom_tools": [{"description": "no name"}]}},
    )
    assert r.status_code == 422
    mocks.store.save_config.assert_not_called()


def test_edit_config_rejects_non_list_custom_tools(client, mocks):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    r = client.put("/sessions/s1/config", json={"config": {"custom_tools": "nope"}})
    assert r.status_code == 422


def test_edit_config_allows_extra_fields(client, mocks):
    # Permissive: unknown fields pass through (TurnCall validates the full schema).
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    r = client.put(
        "/sessions/s1/config",
        json={"config": {"system_prompt": "hi", "some_future_field": 42}},
    )
    assert r.status_code == 200
    mocks.store.save_config.assert_awaited_once()


def test_create_agent_rejects_broken_session_config(client, mocks):
    mocks.store.get_session.return_value = {
        "agent_id": None,
        "config": {"custom_tools": [{"description": "no name"}]},
    }
    r = client.post("/sessions/s1/create")
    assert r.status_code == 422
    mocks.client.create_project.assert_not_called()  # never starts provisioning


# --- Builder-model picker (per-Session provider/model choice) ---


def test_builder_providers_endpoint(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.get("/providers/builder")
    assert r.status_code == 200
    data = r.json()["data"]
    assert {"name": "anthropic", "available": True} in data["providers"]
    assert {"name": "openai", "available": False} in data["providers"]
    assert data["default"]["provider"]


def test_create_session_provider_without_model_400(client, mocks):
    r = client.post("/sessions", json={"builder_provider": "openai"})
    assert r.status_code == 400
    assert "together" in r.json()["detail"]


def test_create_session_unknown_provider_400(client, mocks):
    r = client.post("/sessions", json={"builder_provider": "mistral", "builder_model": "m"})
    assert r.status_code == 400


def test_create_session_unkeyed_provider_400(client, mocks, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.post("/sessions", json={"builder_provider": "openai", "builder_model": "gpt-test"})
    assert r.status_code == 400
    assert "no API key" in r.json()["detail"]


def test_create_session_stores_builder_choice(client, mocks, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    mocks.store.create_session.return_value = "sess-1"
    r = client.post("/sessions", json={"builder_provider": "openai", "builder_model": "gpt-test"})
    assert r.status_code == 200
    kwargs = mocks.store.create_session.call_args.kwargs
    assert kwargs["builder_provider"] == "openai"
    assert kwargs["builder_model"] == "gpt-test"


def test_post_message_uses_session_builder_choice(client, mocks, monkeypatch):
    mocks.store.get_session.return_value = {
        "history": [], "config": None, "agent_id": None,
        "builder_provider": "openai", "builder_model": "gpt-test",
    }
    fake_step = AsyncMock(return_value=ComposeResult(action="ask", question="?"))
    monkeypatch.setattr(sessions, "step", fake_step)
    r = client.post("/sessions/sess-1/messages", json={"message": "hi"})
    assert r.status_code == 200
    assert fake_step.call_args.kwargs["provider"] == "openai"
    assert fake_step.call_args.kwargs["model"] == "gpt-test"


def test_post_message_builder_error_is_503(client, mocks, monkeypatch):
    from app.builder import BuilderError

    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    monkeypatch.setattr(sessions, "step", AsyncMock(side_effect=BuilderError("boom")))
    r = client.post("/sessions/sess-1/messages", json={"message": "hi"})
    assert r.status_code == 503


def test_edit_session_inherits_creation_builder_choice(client, mocks, monkeypatch):
    """Opening an agent for chat editing reuses (and reports) the Builder model
    its creating session picked — when that provider is still keyed."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    mocks.registry.get_backend.return_value = {"slug": "bot", "config": {"name": "Bot"}}
    mocks.store.get_creation_builder_choice.return_value = ("openai", "gpt-test")
    mocks.store.create_session.return_value = "sess-2"
    r = client.post("/sessions", json={"agent_id": "a1"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert (data["builder_provider"], data["builder_model"]) == ("openai", "gpt-test")
    kwargs = mocks.store.create_session.call_args.kwargs
    assert kwargs["builder_provider"] == "openai"
    assert kwargs["builder_model"] == "gpt-test"


def test_edit_session_ignores_creation_choice_when_unkeyed(client, mocks, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mocks.registry.get_backend.return_value = {"slug": "bot", "config": {"name": "Bot"}}
    mocks.store.get_creation_builder_choice.return_value = ("openai", "gpt-test")
    mocks.store.create_session.return_value = "sess-3"
    r = client.post("/sessions", json={"agent_id": "a1"})
    assert r.status_code == 200
    assert r.json()["data"]["builder_provider"] is None
    assert mocks.store.create_session.call_args.kwargs["builder_provider"] is None
