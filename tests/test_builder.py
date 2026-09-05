"""Parser tests for the builder — pure, no network."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.builder import _to_result, step


def _mock_client():
    block = SimpleNamespace(
        type="tool_use", name="compose", input={"action": "ask", "question": "?"}
    )
    resp = SimpleNamespace(content=[block])
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=resp)))


@pytest.mark.asyncio
async def test_step_edit_mode_injects_current_config():
    client = _mock_client()
    await step(
        [{"role": "user", "content": "change the name to Zoe"}],
        client=client,
        current_config={"name": "Bob", "system_prompt": "hi"},
    )
    system = client.messages.create.call_args.kwargs["system"]
    assert "EDITING an existing agent" in system
    assert '"name": "Bob"' in system  # current config is grounded in the prompt


@pytest.mark.asyncio
async def test_step_build_mode_has_no_edit_section():
    client = _mock_client()
    await step([{"role": "user", "content": "build a receptionist"}], client=client)
    assert "EDITING an existing agent" not in client.messages.create.call_args.kwargs["system"]


@pytest.mark.asyncio
async def test_step_injects_knowledge_doc_names():
    client = _mock_client()
    await step(
        [{"role": "user", "content": "add room service using the menu"}],
        client=client,
        knowledge_docs=["House of Sanskara - Food Menu.pdf"],
    )
    system = client.messages.create.call_args.kwargs["system"]
    assert "House of Sanskara - Food Menu.pdf" in system
    assert "do not ask the user to paste" in system.lower()


def test_system_prompt_has_knowledge_base_awareness():
    from app.builder import SYSTEM

    assert "knowledge base" in SYSTEM.lower()
    assert "paste" in SYSTEM.lower()  # must be told not to ask for pasted content


def test_ask_maps_question():
    r = _to_result({"action": "ask", "question": "What should it do?"})
    assert r.action == "ask"
    assert r.question == "What should it do?"
    assert r.agent_config is None


def test_finalize_maps_config():
    cfg = {"name": "Bot", "system_prompt": "You are...", "llm": {}, "tts": {}}
    r = _to_result({"action": "finalize", "agent_config": cfg})
    assert r.action == "finalize"
    assert r.agent_config == cfg


def test_ask_without_question_rejected():
    with pytest.raises(ValueError):
        _to_result({"action": "ask"})


def test_finalize_without_config_rejected():
    with pytest.raises(ValueError):
        _to_result({"action": "finalize"})


def test_unknown_action_rejected():
    with pytest.raises(ValueError):
        _to_result({"action": "wat"})


def test_system_prompt_covers_human_transfer_patterns():
    """The builder must know both transfer patterns: fixed number in the
    prompt, dynamic number via a get_transfer_number backend tool."""
    from app.builder import SYSTEM

    assert "get_transfer_number" in SYSTEM
    assert "transfer_call" in SYSTEM
    assert "fixed" in SYSTEM.lower()


# --- Builder model providers (per-Session provider choice) ---


def _mock_openai_client(arguments='{"action": "ask", "question": "?"}'):
    # Responses API shape: output items, function_call ones carry name+arguments.
    call = SimpleNamespace(type="function_call", name="compose", arguments=arguments)
    resp = SimpleNamespace(output=[SimpleNamespace(type="reasoning"), call])
    return SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=resp)))


@pytest.mark.asyncio
async def test_openai_builder_translates_tool_and_parses_result():
    from app.builder import COMPOSE_TOOL, OpenAIBuilder

    client = _mock_openai_client()
    out = await OpenAIBuilder(client).compose(
        model="gpt-test", system="sys", messages=[{"role": "user", "content": "hi"}]
    )
    assert out == {"action": "ask", "question": "?"}
    kwargs = client.responses.create.call_args.kwargs
    # forced function call, schema carried over verbatim, system as instructions
    assert kwargs["tool_choice"] == {"type": "function", "name": "compose"}
    assert kwargs["tools"][0]["parameters"] == COMPOSE_TOOL["input_schema"]
    assert kwargs["instructions"] == "sys"
    assert kwargs["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_openai_builder_malformed_arguments_is_builder_error():
    from app.builder import BuilderError, OpenAIBuilder

    client = _mock_openai_client(arguments="not json")
    with pytest.raises(BuilderError):
        await OpenAIBuilder(client).compose(
            model="gpt-test", system="s", messages=[{"role": "user", "content": "x"}]
        )


@pytest.mark.asyncio
async def test_step_unknown_provider_is_builder_error():
    from app.builder import BuilderError

    with pytest.raises(BuilderError):
        await step([{"role": "user", "content": "hi"}], provider="mistral", model="m")


@pytest.mark.asyncio
async def test_step_routes_to_session_provider(monkeypatch):
    """step(provider='openai') must call the OpenAI adapter with the session model."""
    from app import builder as builder_mod

    captured = {}

    class FakeOpenAI:
        async def compose(self, *, model, system, messages):
            captured["model"] = model
            return {"action": "ask", "question": "?"}

    monkeypatch.setitem(builder_mod._PROVIDERS, "openai", FakeOpenAI)
    r = await step([{"role": "user", "content": "hi"}], provider="openai", model="gpt-test")
    assert r.action == "ask"
    assert captured["model"] == "gpt-test"


def test_provider_availability_reflects_env(monkeypatch):
    from app.builder import provider_availability

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert provider_availability() == {"anthropic": True, "openai": False}


# --- vendor error classification (surfacing actionable failures) --------------


class _VendorError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_classify_credit_exhaustion_despite_a_400():
    """Anthropic reports an empty credit balance as a 400, indistinguishable
    from a malformed request unless the message is read."""
    from app.builder import classify_vendor_error

    exc = _VendorError(
        "Error code: 400 - {'error': {'message': 'Your credit balance is too low "
        "to access the Anthropic API.'}}",
        400,
    )
    assert classify_vendor_error(exc) == "credit"


def test_classify_openai_quota_beats_its_429():
    """OpenAI sends insufficient_quota as 429, but retrying never clears it."""
    from app.builder import classify_vendor_error

    assert classify_vendor_error(_VendorError("You exceeded your current quota", 429)) == "credit"


def test_classify_auth_and_rate_limit_and_unknown():
    from app.builder import classify_vendor_error

    assert classify_vendor_error(_VendorError("invalid x-api-key", 401)) == "auth"
    assert classify_vendor_error(_VendorError("Rate limit reached", 429)) == "rate_limit"
    assert classify_vendor_error(_VendorError("Internal server error", 500)) == "upstream"


def test_builder_error_defaults_to_upstream():
    from app.builder import BuilderError

    assert BuilderError("boom").kind == "upstream"


# --- prompt assembly: interview shape, recommendations, language --------------


def test_opening_turn_asks_exactly_one_question():
    """The first reply decides whether this reads as a conversation or a form."""
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=True)
    assert "exactly ONE question" in p
    assert "Suggested:" not in p  # rounds guidance must not leak into turn one


def test_later_turns_ask_a_round_with_recommendations():
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=False)
    assert "single round" in p
    assert "At most 4 questions" in p
    assert "Suggested:" in p          # every question carries a recommendation
    assert "all suggested" in p       # ...and the user can accept the whole round
    assert "exactly ONE question" not in p


def test_rounds_forbid_markdown():
    """The console renders builder text verbatim — asterisks would show up raw."""
    from app.builder import build_system_prompt

    assert "no markdown" in build_system_prompt(first_turn=False).lower()


def test_rounds_keep_dependent_questions_out_of_the_same_round():
    from app.builder import build_system_prompt

    assert "depends on another question" in build_system_prompt(first_turn=False)


def test_builder_is_told_to_reuse_the_users_vocabulary():
    """The user's own nouns are the most valuable thing they say."""
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=False)
    assert "Use the user's own words" in p
    assert "covers, guests, patients" in p


def test_edit_mode_drops_the_interview_but_keeps_the_domain_rules():
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=False, current_config={"name": "Bot"})
    assert "Suggested:" not in p            # editing must not re-interview
    assert "EDITING an existing agent" in p
    assert "pipeline_mode" in p             # ...but still has to emit a valid config
    assert "guardrails.prohibited_topics" in p


def test_knowledge_rule_applies_before_any_document_is_attached():
    """Someone saying "I'll upload our menu" must not be asked to paste it."""
    from app.builder import build_system_prompt

    assert "NEVER ask the user to" in build_system_prompt(first_turn=False)


def test_attached_filenames_are_named_only_when_present():
    from app.builder import build_system_prompt

    with_docs = build_system_prompt(first_turn=False, knowledge_docs=["menu.pdf"])
    assert "menu.pdf" in with_docs
    assert "menu.pdf" not in build_system_prompt(first_turn=False)


def test_nova_sonic_is_offered_as_an_s2s_option():
    from app.builder import build_system_prompt

    assert "amazon.nova-2-sonic-v1:0" in build_system_prompt(first_turn=False)
