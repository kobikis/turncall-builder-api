"""Which agents use a Skill (#134): TurnCall's agents listing on the
library, named as Console agents through the Backend registry, and a refused
delete answered naming them."""

from __future__ import annotations

import pytest

from tests.conftest import audit_entries, backend_row
from tests.test_skills import VERIFY
from tests.turncall_fake import FakeLibraryStore, FakeTurnCall

LIBRARY_KEY = "Bearer tc_proj-1"


@pytest.fixture
def tc(mocks):
    fake = FakeTurnCall()
    fake.install()
    fake.store = FakeLibraryStore(mocks)
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", project_id="p1", config={"name": "Front desk"}),
    ]
    return fake


def _listing(**over):
    row = {
        "agent_id": "tc-agent-1", "agent_name": "builder-front-desk", "agent_version": 1,
        "agent_state": "draft", "skill": "verify_caller", "resolved_skill_id": "v-2",
        "resolved_version": 2, "pinned": False, "project_id": "p1",
    }
    row.update(over)
    return row


def test_used_by_names_console_agents_and_keeps_unknown_ones(client, tc):
    client.post("/skills", json=VERIFY)
    tc.agents_listing["verify_caller"] = [
        _listing(),
        _listing(agent_id="tc-agent-9", agent_name="someone-else", project_id="p9",
                 resolved_skill_id="v-1", resolved_version=1, pinned=True),
    ]

    r = client.get("/skills/verify_caller/agents")

    assert r.status_code == 200, r.text
    assert r.json()["data"] == [
        {
            "agent_id": "a1", "name": "Front desk", "known": True, "project_id": "p1",
            "turncall_agent_id": "tc-agent-1", "agent_version": 1, "agent_state": "draft",
            "resolved_skill_id": "v-2", "resolved_version": 2, "pinned": False,
        },
        {
            "agent_id": None, "name": "someone-else", "known": False, "project_id": "p9",
            "turncall_agent_id": "tc-agent-9", "agent_version": 1, "agent_state": "draft",
            "resolved_skill_id": "v-1", "resolved_version": 1, "pinned": True,
        },
    ]
    (asked,) = tc.sent("GET", "/v1/skills/verify_caller/agents")
    assert asked.headers["Authorization"] == LIBRARY_KEY


def test_a_name_turncall_has_no_version_of_is_its_404(client, tc):
    client.post("/skills", json=VERIFY)
    tc.refuse("GET", "/v1/skills/nothing/agents", 404,
              {"success": False, "error": "Skill not found", "code": "not_found"})
    assert client.get("/skills/nothing/agents").status_code == 404


def test_a_refused_delete_names_the_console_agents(client, mocks, tc):
    v1 = client.post("/skills", json=VERIFY).json()["data"]["id"]
    tc.refuse("DELETE", f"/v1/skills/{v1}", 409, {
        "success": False,
        "error": "Skill 'verify_caller' is listed by 2 agent versions",
        "code": "conflict",
        "details": {"skill": "verify_caller", "agents": [
            {"agent_id": "tc-agent-1", "name": "builder-front-desk", "version": 1,
             "state": "draft", "project_id": "p1"},
            {"agent_id": "tc-agent-9", "name": "someone-else", "version": 3,
             "state": "published", "project_id": "p9"},
        ]},
    })

    r = client.delete(f"/skill-versions/{v1}")

    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "skill_in_use"
    assert body["skill"] == "verify_caller"
    assert body["detail"] == "'verify_caller' is used by 'Front desk', 'someone-else'; take it off them first"
    assert [(a["agent_id"], a["name"], a["known"], a["agent_version"], a["agent_state"])
            for a in body["agents"]] == [
        ("a1", "Front desk", True, 1, "draft"),
        (None, "someone-else", False, 3, "published"),
    ]
    # Nothing was deleted, so nothing is recorded.
    assert [e["action"] for e in audit_entries(mocks) if e["resource_type"] == "capability"] == ["created"]


def test_another_conflict_is_turncalls_as_given(client, tc):
    v1 = client.post("/skills", json=VERIFY).json()["data"]["id"]
    tc.refuse("DELETE", f"/v1/skills/{v1}", 409, {"success": False, "error": "busy", "code": "conflict"})
    r = client.delete(f"/skill-versions/{v1}")
    assert (r.status_code, r.json()["detail"]) == (409, "busy")
