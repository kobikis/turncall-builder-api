"""The builder can see the catalog, and still cannot attach anything."""

import json
import pathlib
import re

from app import builder
from app.pipedream import CATALOG


def test_the_prompt_names_every_catalog_app():
    """Derived from the catalog so a third app needs no edit to the prompt."""
    prompt = builder.build_system_prompt(first_turn=False)
    for app in CATALOG.values():
        assert app.name in prompt


def test_it_is_told_not_to_duplicate_an_integration():
    """The bug this exists for: a `schedule_google_calendar_event` custom tool
    invented beside a real Google Calendar integration."""
    prompt = builder.build_system_prompt(first_turn=False)
    assert "NEVER write a custom_tool that duplicates it" in prompt


def test_it_is_told_it_cannot_attach_one_itself():
    """Attaching is a human act (ADR-0021). Seeing is not attaching."""
    prompt = builder.build_system_prompt(first_turn=False)
    assert "cannot attach an app yourself" in prompt


def _config_properties() -> dict:
    return builder.COMPOSE_TOOL["input_schema"]["properties"]["agent_config"]["properties"]


def test_the_builder_still_cannot_attach_an_app():
    """Seeing the catalog must not become a way to attach from it: attaching is a
    human act (ADR-0021), and a prompt that could perform one would undo the
    guarantee that the Workspace chooses its own integrations.

    Asserted on the property name, not a substring — `needs_integrations` is a
    request and contains the word.
    """
    assert "integrations" not in _config_properties()


def test_it_can_record_what_an_agent_needs():
    """The outlet. Without one, a model told "never write a tool for this" and
    asked for calendar bookings just writes the tool anyway — which is what it
    did before this field existed."""
    assert "needs_integrations" in _config_properties()
    described = json.dumps(_config_properties()["needs_integrations"])
    assert "ATTACHES NOTHING" in described


def test_tools_are_named_the_way_the_agent_will_see_them():
    """The prefix is part of the name. An agent told to "use create-event" is
    told to use nothing — a system_prompt that named the tool that way is what
    made one live call improvise instead of booking."""
    prompt = builder.build_system_prompt(first_turn=False)
    for app in CATALOG.values():
        for tool in app.tools:
            assert tool in prompt, tool


def test_it_knows_the_agent_is_handed_the_date():
    """An LLM has no clock, and call-init gives it one (scaffold._now_line). A
    builder that does not know that writes prompts which guess, and a guess
    lands in the training data's past — one booking went to October 2023."""
    prompt = builder.build_system_prompt(first_turn=False)
    assert "never guess one" in prompt
    assert "Never write today's date" in prompt


def test_the_prompt_carries_no_date_of_its_own():
    """It is assembled once per process and outlives any date in it."""
    prompt = builder.build_system_prompt(first_turn=False)
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", prompt)


# --- the connected-apps skill ------------------------------------------------
# The prompt points at it; these pin what it has to be carrying when the builder
# opens it. Each assertion is a fault seen on a live call, not a style rule.

SKILL = (
    pathlib.Path(builder.SKILLS_DIR) / "connected-apps" / "SKILL.md"
).read_text()


def test_the_directive_points_at_a_skill_that_exists():
    assert "connected-apps skill" in builder.build_system_prompt(first_turn=False)
    assert SKILL.startswith("---\nname: connected-apps\n")


def test_the_skill_is_described_by_when_to_open_it():
    """deepagents chooses a skill from its description alone. One that describes
    its contents instead of its trigger never gets opened."""
    description = SKILL.split("description:", 1)[1].split("\n", 1)[0].lower()
    assert "use when" in description or "use whenever" in description
    for app in CATALOG.values():
        assert app.name in SKILL


def test_it_says_how_to_invite_the_caller():
    """Three steps, and the third is the silent one: an attendee added without
    sendUpdates is a guest Google never emails."""
    assert "attendee" in SKILL
    assert "send updates" in SKILL.lower()


def test_it_says_what_belongs_in_a_calendar_invitation():
    """Business name in the title, street address in the location — a calendar
    entry the staff cannot act on is not a booking."""
    invitation = SKILL.split("## Google Calendar", 1)[1]
    assert "business name" in invitation
    assert "address" in invitation


def test_it_does_not_let_the_builder_promise_an_end_of_call_automation():
    """The agent cannot post one — it is the workspace's trigger (ADR-0021), and
    a prompt claiming otherwise describes a capability that does not exist."""
    assert "end-of-call Automation" in SKILL


def test_the_tools_skill_does_not_send_the_builder_back_to_writing_one():
    """agent-tools says "author a custom_tool" for any external action, which is
    where `schedule_google_calendar_event` came from. The carve-out has to live
    on that page — a rule in a skill the builder did not open stops nothing."""
    tools_skill = (pathlib.Path(builder.SKILLS_DIR) / "agent-tools" / "SKILL.md").read_text()
    action = tools_skill.split("## Actions the agent performs", 1)[1]
    assert "connected-apps skill" in action
    assert action.index("Connected app") < action.index("author a custom_tool")
