"""The Builder authors procedures (#137): a step-by-step process the user
describes lands in `procedures`, escalation is a transfer, and an edit that
does not touch them keeps them."""

from __future__ import annotations

import pathlib

from conftest import FakeStream

from app import builder
from app.builder import ComposeResult, build_system_prompt
from app.routers import sessions
from tests.test_agent_procedures import PROCEDURES

SKILLS = pathlib.Path(builder.SKILLS_DIR)


def _procedure_schema() -> dict:
    props = builder.COMPOSE_SCHEMA["properties"]["agent_config"]["properties"]
    return props["procedures"]["items"]


def test_the_config_schema_offers_procedures_in_turncalls_shape():
    procedure = _procedure_schema()
    assert procedure["required"] == ["name", "steps"]
    assert procedure["properties"]["start"]["enum"] == ["call", "on_request"]
    assert procedure["properties"]["window_depth"]["maximum"] == 5
    step = procedure["properties"]["steps"]["items"]
    assert procedure["properties"]["steps"]["maxItems"] == 50
    assert step["properties"]["kind"]["enum"] == ["ask", "instruct", "check", "escalate", "end"]
    assert step["properties"]["moves"]["items"]["required"] == ["to", "when"]


def test_escalation_offers_a_transfer_only():
    """Each Builder agent is its own project: a handoff to another Builder
    agent cannot work, so the composer is never offered one."""
    escalate = _procedure_schema()["properties"]["steps"]["items"]["properties"]["escalate"]
    assert set(escalate["properties"]) == {"transfer_call"}
    transfer = escalate["properties"]["transfer_call"]
    assert transfer["required"] == ["target_number"]
    assert "handoff_to_agent" not in str(builder.COMPOSE_SCHEMA)


def test_every_prompt_says_a_described_process_is_a_procedure():
    for prompt in (
        build_system_prompt(first_turn=True),
        build_system_prompt(first_turn=False),
        build_system_prompt(first_turn=False, current_config={"name": "A"}),
    ):
        assert "agent_config.procedures" in prompt
        assert "procedures builder skill" in prompt


def test_the_procedures_skill_covers_what_the_composer_must_get_right():
    skill = (SKILLS / "procedures" / "SKILL.md").read_text()
    # Escalation is a transfer to a number the user gave, never a handoff.
    assert "transfer_call" in skill and "handoff_to_agent" in skill
    assert "E.164" in skill
    # A step's tool is one the agent has.
    assert "custom_tools" in skill
    # move_to_step is the platform's, never listed.
    assert "move_to_step" in skill
    # Cascade only.
    assert "s2s" in skill


# --- a turn ------------------------------------------------------------------------


def _turn(client, mocks, monkeypatch, *, current, finalized) -> dict:
    mocks.store.get_session.return_value = {"history": [], "config": current, "agent_id": None}
    monkeypatch.setattr(
        sessions, "stream", FakeStream(ComposeResult(action="finalize", agent_config=finalized))
    )
    r = client.post("/sessions/s1/messages", json={"message": "go"})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_a_described_procedure_lands_in_procedures(client, mocks, monkeypatch):
    cfg = {"name": "Support", "system_prompt": "You help.", "procedures": PROCEDURES}

    data = _turn(client, mocks, monkeypatch, current=None, finalized=cfg)

    assert data["agent_config"]["procedures"] == PROCEDURES
    assert mocks.store.save_config.await_args.args[2]["procedures"] == PROCEDURES


def test_an_unrelated_edit_keeps_them(client, mocks, monkeypatch):
    """The model rewrote the greeting and left procedures out of what it
    finalized: they were not part of the change, so they stay."""
    current = {"name": "Support", "system_prompt": "You help.", "procedures": PROCEDURES}
    edited = {"name": "Support", "system_prompt": "You help.", "first_message": "Hi there!"}

    data = _turn(client, mocks, monkeypatch, current=current, finalized=edited)

    assert data["agent_config"]["procedures"] == PROCEDURES
    assert data["agent_config"]["first_message"] == "Hi there!"
    assert mocks.store.save_config.await_args.args[2]["procedures"] == PROCEDURES


def test_an_edit_can_remove_them(client, mocks, monkeypatch):
    current = {"name": "Support", "system_prompt": "You help.", "procedures": PROCEDURES}
    edited = {"name": "Support", "system_prompt": "You help.", "procedures": []}

    data = _turn(client, mocks, monkeypatch, current=current, finalized=edited)

    assert data["agent_config"]["procedures"] == []


def test_an_edit_can_change_them(client, mocks, monkeypatch):
    current = {"name": "Support", "system_prompt": "You help.", "procedures": PROCEDURES}
    changed = [{**PROCEDURES[0], "window_depth": 2}]
    edited = {"name": "Support", "system_prompt": "You help.", "procedures": changed}

    data = _turn(client, mocks, monkeypatch, current=current, finalized=edited)

    assert data["agent_config"]["procedures"] == changed
