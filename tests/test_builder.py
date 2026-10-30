"""Parser tests for the builder — pure, no network."""

import pathlib

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import builder as builder_mod
from app.builder import BuilderError, _to_result, step

SKILLS = pathlib.Path(builder_mod.SKILLS_DIR)


@pytest.fixture
def agent(monkeypatch):
    """Record what the turn builds its deep agent with, and answer for it.

    Everything worth asserting about a turn is in those arguments — the model,
    the prompt, what is mounted and what the loop is allowed to do — so this
    needs no fake LLM. Set `agent["reply"]` to change what comes back.
    """
    calls: dict = {"reply": {"action": "ask", "question": "?"}}

    class FakeAgent:
        async def astream(self, state, config=None, stream_mode=None):
            calls["state"] = state
            calls["config"] = config
            if isinstance(calls["reply"], BaseException):
                raise calls["reply"]
            for update in calls.get("updates", []):
                yield "updates", update
            yield "values", {"structured_response": calls["reply"]}

    monkeypatch.setattr(
        builder_mod, "create_deep_agent", lambda **kw: (calls.update(kw), FakeAgent())[1]
    )
    return calls


@pytest.mark.asyncio
async def test_step_edit_mode_injects_current_config(agent):
    await step(
        [{"role": "user", "content": "change the name to Zoe"}],
        current_config={"name": "Bob", "system_prompt": "hi"},
    )
    system = agent["system_prompt"]
    assert "EDITING an existing agent" in system
    assert '"name": "Bob"' in system  # current config is grounded in the prompt


@pytest.mark.asyncio
async def test_step_build_mode_has_no_edit_section(agent):
    await step([{"role": "user", "content": "build a receptionist"}])
    assert "EDITING an existing agent" not in agent["system_prompt"]


@pytest.mark.asyncio
async def test_step_injects_knowledge_doc_names(agent):
    await step(
        [{"role": "user", "content": "add room service using the menu"}],
        knowledge_docs=["House of Sanskara - Food Menu.pdf"],
    )
    system = agent["system_prompt"]
    assert "House of Sanskara - Food Menu.pdf" in system
    assert "do not ask the user to paste" in system.lower()


# --- the deep-agent turn (ADR-0014) ---


@pytest.mark.asyncio
async def test_the_builder_gets_its_skills_library(agent):
    await step([{"role": "user", "content": "hi"}])
    assert agent["skills"] == ["/skills/"]


@pytest.mark.asyncio
async def test_documents_are_mounted_only_when_there_are_some(agent, tmp_path):
    await step([{"role": "user", "content": "hi"}], documents_dir=str(tmp_path / "nope"))
    assert "/documents/" not in agent["backend"].routes

    await step([{"role": "user", "content": "hi"}], documents_dir=str(tmp_path))
    assert "/documents/" in agent["backend"].routes


@pytest.mark.asyncio
async def test_the_loop_is_capped(agent):
    """Uncapped and stateless is the bad pair: a runaway turn saves nothing."""
    await step([{"role": "user", "content": "hi"}])
    assert agent["config"]["recursion_limit"] == builder_mod.MAX_STEPS


@pytest.mark.asyncio
async def test_a_turn_that_never_finishes_is_a_timeout_not_a_hang(agent, monkeypatch):
    monkeypatch.setattr(builder_mod, "TURN_TIMEOUT_SECONDS", 0.01)

    async def never(*_a, **_k):
        await asyncio.sleep(10)
        yield "values", {}

    monkeypatch.setattr(
        builder_mod, "create_deep_agent", lambda **kw: SimpleNamespace(astream=never)
    )
    with pytest.raises(BuilderError) as exc:
        await step([{"role": "user", "content": "hi"}])
    assert exc.value.kind == "timeout"


@pytest.mark.asyncio
async def test_a_turn_with_no_result_is_a_protocol_error(agent, monkeypatch):
    async def prose(*_a, **_k):
        yield "values", {"messages": []}

    monkeypatch.setattr(
        builder_mod, "create_deep_agent", lambda **kw: SimpleNamespace(astream=prose)
    )
    with pytest.raises(BuilderError) as exc:
        await step([{"role": "user", "content": "hi"}])
    assert exc.value.kind == "protocol"


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


def test_transfer_patterns_live_in_the_tools_skill():
    """Both transfer patterns still have to be written down somewhere the
    builder will find them — they moved out of the prompt into a skill, so the
    guarantee moves with them rather than disappearing."""
    skill = (SKILLS / "agent-tools" / "SKILL.md").read_text()
    assert "get_transfer_number" in skill
    assert "transfer_call" in skill
    assert "fixed" in skill.lower()


def test_every_skill_declares_a_name_and_description():
    """Only the description is loaded until a skill is opened, so a skill
    without one is invisible and a skill whose name mismatches its directory is
    not discoverable at all."""
    for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
        front = skill_md.read_text().split("---")[1]
        assert f"name: {skill_md.parent.name}" in front, skill_md
        assert "description:" in front, skill_md


def test_conditional_guidance_left_the_base_prompt():
    """The point of the split: guidance that applies to some agents is loaded
    when it applies. What applies to every agent stays in the prompt."""
    from app.builder import SYSTEM

    assert "get_transfer_number" not in SYSTEM
    assert "prohibited_topics" not in SYSTEM
    # ...while the rules true of every phone agent are still unconditional.
    assert "PHONE CALL" in SYSTEM
    assert "paste" in SYSTEM.lower()


# --- Builder model providers (per-Session provider choice) ---


@pytest.mark.asyncio
async def test_step_unknown_provider_is_builder_error():
    from app.builder import BuilderError

    with pytest.raises(BuilderError):
        await step([{"role": "user", "content": "hi"}], provider="mistral", model="m")


@pytest.mark.asyncio
async def test_step_routes_to_session_provider(agent):
    """The Session's provider/model becomes the model id the agent runs on."""
    r = await step([{"role": "user", "content": "hi"}], provider="openai", model="gpt-test")
    assert r.action == "ask"
    assert agent["model"] == "openai:gpt-test"


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


@pytest.mark.asyncio
async def test_editing_an_agent_still_has_the_skills(agent):
    """Guardrails and tools moved out of the edit prompt into skills, so an edit
    turn has to be able to load them — otherwise the split loses guidance that
    edit mode used to carry."""
    await step(
        [{"role": "user", "content": "stop it discussing competitors"}],
        current_config={"name": "Bot"},
    )
    assert agent["skills"] == ["/skills/"]


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
    """Moved into the s2s skill with the rest of the provider detail. Still
    reachable, still asserted — the config schema names the provider, the skill
    names the model and its voices."""
    assert "amazon.nova-2-sonic-v1:0" in (SKILLS / "speech-to-speech" / "SKILL.md").read_text()
    assert "aws" in str(builder_mod._AGENT_CONFIG_SCHEMA["properties"]["s2s"])


# --- the generated agent has to sound like a phone call ----------------------
# From a real Sushi Samba transcript: the agent read out "1. Your name 2. The
# time you'd like 3. A contact phone number" to a caller, and answered an
# out-of-scope request with a flat "I'm only able to assist with reservations".


def test_generated_agent_is_told_to_ask_one_thing_at_a_time():
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=False)
    assert "ONE thing at a time" in p
    assert "Never enumerate" in p


def test_generated_agent_is_told_not_to_emit_lists_or_markdown():
    """Everything the agent says is spoken verbatim by TTS."""
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=False)
    assert "numbered lists" in p
    assert "spoken verbatim" in p


def test_out_of_scope_requests_get_a_warm_redirect_not_a_refusal():
    from app.builder import build_system_prompt

    assert "Never a flat refusal" in build_system_prompt(first_turn=False)


def test_builder_is_told_not_to_copy_its_own_interview_style():
    """The Builder asks numbered rounds in a text console; an agent that copies
    that style reads a list down the phone."""
    from app.builder import build_system_prompt

    assert "never copy that style" in build_system_prompt(first_turn=False)


def test_voice_guidance_applies_when_editing_an_existing_agent():
    """Editing is how an already-created agent gets this fixed."""
    from app.builder import build_system_prompt

    p = build_system_prompt(first_turn=False, current_config={"name": "Sushi"})
    assert "ONE thing at a time" in p
    assert "Never a flat refusal" in p


def test_classification_reads_through_a_wrapped_vendor_error():
    """langchain wraps vendor exceptions. If classification only looked at the
    outermost one, an exhausted account would read as a transient failure and
    the console would tell the user to retry something that cannot succeed."""
    from app.builder import classify_vendor_error

    vendor = Exception("Your credit balance is too low to access the API")
    wrapped = RuntimeError("error calling the model")
    wrapped.__cause__ = vendor
    assert classify_vendor_error(wrapped) == "credit"


def test_the_real_agent_builds_and_finds_its_skills(monkeypatch, tmp_path):
    """Every other turn test fakes create_deep_agent, so this is the one that
    fails if the real signature, the /skills/ mount or the compose schema stops
    being accepted.

    The skills half matters most: a skill with broken frontmatter or a name that
    no longer matches its directory just disappears, and guidance vanishing
    quietly is the failure mode this split introduces. Reaching for deepagents'
    private lister is deliberate — there is no public way to ask "what did you
    find?", and finding nothing must not read as success.
    """
    from deepagents import create_deep_agent
    from deepagents.middleware.skills import _list_skills_with_errors
    from langchain.agents.structured_output import ToolStrategy

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    skills, error = _list_skills_with_errors(builder_mod._backend(None), "/skills/")
    assert error is None
    assert {s["name"] for s in skills} == {d.name for d in SKILLS.iterdir() if d.is_dir()}
    assert skills, "no skills discovered — the mount or the frontmatter is wrong"

    agent = create_deep_agent(
        model="anthropic:claude-sonnet-4-6",
        system_prompt=builder_mod.build_system_prompt(first_turn=False),
        backend=builder_mod._backend(str(tmp_path)),
        skills=["/skills/"],
        response_format=ToolStrategy(builder_mod.COMPOSE_SCHEMA),
    )
    assert agent is not None


# --- what the console gets to show while it waits ---------------------------


def _ai(*calls):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"name": n, "args": a, "id": str(i)} for i, (n, a) in enumerate(calls)],
    )


def test_progress_is_derived_not_raw():
    """`read_file("/documents/menu.pdf")` is developer output; the user gets a
    sentence."""
    assert builder_mod._progress_for("read_file", {"file_path": "/documents/menu.pdf"}).text == "Reading menu.pdf"
    assert builder_mod._progress_for("ls", {"path": "/documents/"}).text == "Looking at what you attached"
    assert builder_mod._progress_for("compose", {}).text == "Writing the configuration"


def test_a_skill_read_names_the_skill():
    """The one signal a skill's author has no other way to get: did the
    description I wrote actually get my skill opened?"""
    p = builder_mod._progress_for(
        "read_file", {"file_path": "/workspace-skills/restaurant-reservations/SKILL.md"}
    )
    assert p.skill == "restaurant-reservations"
    assert "restaurant-reservations" in p.text


def test_the_agents_own_housekeeping_is_not_progress():
    """A feed that reports everything is a spinner with extra steps."""
    assert builder_mod._progress_for("write_file", {"file_path": "/notes.md"}) is None
    assert builder_mod._progress_for("grep", {"path": "/skills/"}) is None
    assert builder_mod._progress_for("read_file", {"file_path": "/scratch.txt"}) is None


@pytest.mark.asyncio
async def test_stream_says_what_it_is_doing_then_what_it_decided(agent):
    agent["updates"] = [
        {"model": {"messages": [_ai(("read_file", {"file_path": "/documents/menu.pdf"}))]}},
        {"model": {"messages": [_ai(("read_file", {"file_path": "/workspace-skills/takeaway-orders/SKILL.md"}))]}},
        # ...and the same skill again, which is one line, not two
        {"model": {"messages": [_ai(("read_file", {"file_path": "/workspace-skills/takeaway-orders/SKILL.md"}))]}},
    ]
    events = [e async for e in builder_mod.stream([{"role": "user", "content": "hi"}])]
    assert [e.text for e in events[:-1]] == [
        "Reading menu.pdf",
        "Using the takeaway-orders builder skill",
    ]
    assert events[-1].action == "ask"


@pytest.mark.asyncio
async def test_progress_survives_a_renamed_node(agent):
    """Node names belong to LangGraph. Missing a line would degrade the feed;
    coupling to a name would break it."""
    agent["updates"] = [
        {"some_future_node_name": {"messages": [_ai(("read_file", {"file_path": "/documents/menu.pdf"}))]}},
    ]
    events = [e async for e in builder_mod.stream([{"role": "user", "content": "hi"}])]
    assert events[0].text == "Reading menu.pdf"


@pytest.mark.asyncio
async def test_the_turn_does_not_mutate_the_history_it_was_given(monkeypatch):
    """LangGraph coerces input messages to BaseMessage objects *in place*, and
    the list it is handed is the session history — which is written straight
    back to Postgres as JSON.

    The fake agent here mutates its input the way LangGraph does, because the
    ordinary fixture does not, which is exactly how this reached production: a
    `TypeError: Object of type HumanMessage is not JSON serializable` raised
    from `save_history`, after the response had already started streaming.
    """

    class MutatingAgent:
        async def astream(self, state, config=None, stream_mode=None):
            state["messages"][:] = [object() for _ in state["messages"]]
            yield "values", {"structured_response": {"action": "ask", "question": "?"}}

    monkeypatch.setattr(builder_mod, "create_deep_agent", lambda **kw: MutatingAgent())

    history = [{"role": "user", "content": "hi"}]
    async for _ in builder_mod.stream(history):
        pass
    assert history == [{"role": "user", "content": "hi"}]
    json.dumps(history)  # the actual thing that blew up


# --- confirm before build ----------------------------------------------------


def test_confirm_maps_the_summary_and_builds_nothing():
    r = _to_result({"action": "confirm", "summary": "- Books appointments"})
    assert r.action == "confirm"
    assert r.summary.startswith("- Books")
    assert r.agent_config is None


def test_confirm_without_a_summary_is_rejected():
    with pytest.raises(ValueError, match="requires a summary"):
        _to_result({"action": "confirm"})


def test_the_builder_checks_before_it_builds():
    from app.builder import SYSTEM

    assert "action='confirm'" in SYSTEM
    assert "do NOT build it yet" in SYSTEM


def test_the_summary_is_about_their_business_not_the_configuration():
    """A practice manager can check "transfers billing to the practice manager".
    Nobody can check `custom_tools[0].parameters_schema`."""
    from app.builder import SYSTEM

    assert "Never list providers, models, voices" in SYSTEM
    assert "their words and their numbers" in SYSTEM


def test_a_summary_the_user_accepted_is_settled():
    """Being asked to approve the same thing twice is the failure mode that
    would make people hate this. The model's behaviour can only be verified
    against a live model — what is asserted here is that it was told, plainly."""
    from app.builder import SYSTEM

    assert "Do NOT confirm a second time" in SYSTEM
    assert "Never send the same summary twice" in SYSTEM
