"""The Builder and the Workspace's Capabilities (#135): the prompt lists the
published ones, the model may name them in `skills`, and a name with no
published Capability never reaches the saved config."""

from __future__ import annotations

import pytest
from conftest import FakeStream

from app import builder
from app.builder import ComposeResult, build_system_prompt
from app.routers import sessions
from tests.test_capabilities import VERIFY
from tests.turncall_fake import FakeLibraryStore, FakeTurnCall

PUBLISHED = [
    {"name": "take_payment", "description": "When the caller wants to pay a bill"},
    {"name": "verify_caller", "description": "When the caller asks about their account"},
]


def test_the_prompt_lists_the_published_capabilities():
    prompt = build_system_prompt(first_turn=False, capabilities=PUBLISHED)
    assert "- take_payment: When the caller wants to pay a bill" in prompt
    assert "- verify_caller: When the caller asks about their account" in prompt
    assert "never create, edit or describe a new Capability" in prompt


@pytest.mark.parametrize("capabilities", [None, []])
def test_with_none_the_prompt_says_nothing_of_them(capabilities):
    prompt = build_system_prompt(first_turn=False, capabilities=capabilities)
    assert "Capabilit" not in prompt


def test_the_config_schema_offers_skills_by_name_only():
    props = builder.COMPOSE_SCHEMA["properties"]["agent_config"]["properties"]
    assert props["skills"]["items"]["required"] == ["skill"]
    assert set(props["skills"]["items"]["properties"]) == {"skill", "version_id"}
    assert (props["max_loaded_skills"]["minimum"], props["max_loaded_skills"]["maximum"]) == (1, 10)


# --- a turn ------------------------------------------------------------------------


@pytest.fixture
def library(client, mocks):
    """A Workspace with verify_caller published and take_payment only drafted."""
    fake = FakeTurnCall()
    fake.install()
    FakeLibraryStore(mocks)
    v = client.post("/capabilities", json=VERIFY).json()["data"]["id"]
    client.post(f"/capability-versions/{v}/publish")
    client.post("/capabilities", json={**VERIFY, "name": "take_payment"})
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    return fake


def test_a_turn_is_given_the_published_ones(client, mocks, monkeypatch, library):
    fake = FakeStream(ComposeResult(action="ask", question="?"))
    monkeypatch.setattr(sessions, "stream", fake)
    client.post("/sessions/s1/messages", json={"message": "hi"})
    assert fake.kwargs["capabilities"] == [
        {"name": "verify_caller", "description": VERIFY["description"]}
    ]


def test_an_unpublished_name_is_dropped_with_a_note(client, mocks, monkeypatch, library):
    cfg = {
        "name": "A", "system_prompt": "hi",
        "skills": [{"skill": "verify_caller"}, {"skill": "take_payment"}, {"skill": "made_up"}],
    }
    monkeypatch.setattr(
        sessions, "stream", FakeStream(ComposeResult(action="finalize", agent_config=cfg))
    )

    r = client.post("/sessions/s1/messages", json={"message": "done"})

    data = r.json()["data"]
    assert data["agent_config"]["skills"] == [{"skill": "verify_caller"}]
    assert data["notes"] == [
        "Left out the Capability 'take_payment': this workspace has no published "
        "Capability by that name.",
        "Left out the Capability 'made_up': this workspace has no published "
        "Capability by that name.",
    ]
    saved = mocks.store.save_config.await_args.args[2]
    assert saved["skills"] == [{"skill": "verify_caller"}]
    history = mocks.store.save_history.await_args.args[2]
    assert history[-1]["content"].startswith("Updated the agent configuration. Left out")


def test_a_config_naming_only_published_ones_has_no_notes(client, mocks, monkeypatch, library):
    cfg = {"name": "A", "system_prompt": "hi", "skills": [{"skill": "verify_caller"}]}
    monkeypatch.setattr(
        sessions, "stream", FakeStream(ComposeResult(action="finalize", agent_config=cfg))
    )
    data = client.post("/sessions/s1/messages", json={"message": "done"}).json()["data"]
    assert data["agent_config"]["skills"] == [{"skill": "verify_caller"}]
    assert "notes" not in data


def test_an_unreadable_list_filters_nothing(client, mocks, monkeypatch):
    """Unknown is not none: the turn goes on, and TurnCall checks at save."""
    mocks.capability_store.get.side_effect = RuntimeError("db down")
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    cfg = {"name": "A", "system_prompt": "hi", "skills": [{"skill": "verify_caller"}]}
    fake = FakeStream(ComposeResult(action="finalize", agent_config=cfg))
    monkeypatch.setattr(sessions, "stream", fake)

    data = client.post("/sessions/s1/messages", json={"message": "done"}).json()["data"]

    assert fake.kwargs["capabilities"] is None
    assert data["agent_config"]["skills"] == [{"skill": "verify_caller"}]
