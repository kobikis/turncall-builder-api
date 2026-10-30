"""Every project of a Workspace linked to its Skill library (#132).

Over a MockTransport TurnCall for the library and the retry; on the mocked
client for Create and the webhook bind, as their own tests run. What is
asserted is what reaches TurnCall: which project is linked to which library,
with the platform credential, and what the Builder says of each link."""

from __future__ import annotations

import pytest

from tests.conftest import audit_entries, backend_row
from tests.test_skills import VERIFY
from tests.test_endpoints_agents import _wire_create_success
from tests.test_endpoints_phones import _bind_body
from tests.turncall_fake import PLATFORM_KEY, FakeLibraryStore, FakeTurnCall


@pytest.fixture
def tc(mocks):
    fake = FakeTurnCall()
    fake.install()
    fake.store = FakeLibraryStore(mocks)
    mocks.registry.list_backends.return_value = [
        backend_row(agent_id="a1", project_id="p1", config={"name": "Front desk"}),
        backend_row(agent_id="a2", project_id="p2", config={"name": "Billing"}),
    ]
    mocks.phones.workspace_webhook_project.return_value = {"project_id": "wp", "api_key": "tc_wp"}
    return fake


def _links(client):
    return {
        link["project_id"]: (link["state"], link["error"])
        for link in client.get("/skill-library").json()["data"]["links"]
    }


def test_making_the_library_links_every_agent_project_and_the_webhook_project(client, mocks, tc):
    assert client.post("/skills", json=VERIFY).status_code == 200

    assert tc.links == {"p1": "proj-1", "p2": "proj-1", "wp": "proj-1"}
    puts = tc.sent("PUT", "/v1/projects/")
    assert all(r.headers["X-Platform-Key"] == PLATFORM_KEY for r in puts)
    assert all("Authorization" not in r.headers for r in puts)

    report = client.get("/skill-library").json()["data"]
    assert report["library"] == {"project_id": "proj-1"}
    assert report["complete"] is True
    assert [(x["project_id"], x["kind"], x["name"], x["state"]) for x in report["links"]] == [
        ("p1", "agent", "Front desk", "linked"),
        ("p2", "agent", "Billing", "linked"),
        ("wp", "webhook_numbers", None, "linked"),
    ]

    library_entry = audit_entries(mocks)[0]
    assert (library_entry["resource_type"], library_entry["action"]) == ("skill_library", "created")
    assert library_entry["agent_ids"] == ["a1", "a2"]
    assert library_entry["changes"]["/links/p1"] == {"before": None, "after": "linked"}
    # Each link is in TurnCall's log too, as a change to that project.
    linked = {link["project_id"] for link in library_entry["engine_links"]}
    assert {"p1", "p2", "wp"} <= linked


def test_a_library_whose_linking_fails_is_in_the_log_as_partial(client, mocks, tc):
    # The library is made in TurnCall before its projects are linked; linking
    # failing outright (not one link refused: that is recorded and retried)
    # must not leave a library nothing in the log says was made.
    mocks.registry.list_backends.side_effect = RuntimeError("database went away")

    r = client.post("/skills", json=VERIFY)

    assert r.status_code == 500
    assert tc.sent("POST", "/v1/projects")  # the library was made
    [entry] = audit_entries(mocks)
    assert (entry["resource_type"], entry["action"]) == ("skill_library", "created")
    assert entry["outcome"] == "partial"
    assert entry["changes"]["/project_id"] == {"before": None, "after": "proj-1"}
    assert not tc.sent("POST", "/v1/skills")  # and no Skill


def test_running_the_links_again_writes_nothing(client, mocks, tc):
    client.post("/skills", json=VERIFY)
    before = len(tc.sent("PUT", "/v1/projects/"))
    entries = len(audit_entries(mocks))

    r = client.post("/skill-library/links")

    assert r.status_code == 200
    assert len(tc.sent("PUT", "/v1/projects/")) == before
    assert len(audit_entries(mocks)) == entries
    assert r.json()["data"]["complete"] is True


def test_a_failed_link_is_kept_shown_and_retried(client, mocks, tc):
    tc.refuse("PUT", "/v1/projects/p2/skill-library", 503, {"success": False, "error": "TurnCall is restarting"})
    # The Skill is made all the same: a link is retried, not a reason to fail.
    assert client.post("/skills", json=VERIFY).status_code == 200
    assert _links(client) == {
        "p1": ("linked", None),
        "p2": ("failed", "TurnCall is restarting"),
        "wp": ("linked", None),
    }
    assert client.get("/skill-library").json()["data"]["complete"] is False

    r = client.post("/skill-library/links")

    assert r.status_code == 200
    # Only the failed one is written again.
    assert [p.url.path for p in tc.sent("PUT", "/v1/projects/")][-1] == "/v1/projects/p2/skill-library"
    assert len(tc.sent("PUT", "/v1/projects/p1/")) == 1
    assert _links(client)["p2"] == ("linked", None)
    entry = audit_entries(mocks)[-1]
    assert (entry["resource_type"], entry["action"]) == ("skill_library", "linked")
    assert entry["changes"] == {"/links/p2": {"before": "failed", "after": "linked"}}
    assert entry["agent_ids"] == ["a2"]


def test_a_project_made_since_is_pending_until_linked(client, mocks, tc):
    client.post("/skills", json=VERIFY)
    mocks.registry.list_backends.return_value = [
        *mocks.registry.list_backends.return_value,
        backend_row(agent_id="a3", project_id="p3", config={"name": "Late"}),
    ]
    assert _links(client)["p3"] == ("pending", None)
    client.post("/skill-library/links")
    assert tc.links["p3"] == "proj-1"


def test_no_library_reads_as_none_and_relinking_is_a_404(client, tc):
    assert client.get("/skill-library").json()["data"] == {
        "library": None, "links": [], "complete": True,
    }
    assert client.post("/skill-library/links").status_code == 404
    assert tc.requests == []


def test_a_viewer_reads_the_links_and_cannot_retry(gate_client, mocks, tc):
    from tests.test_skills import WS, _login_as

    _login_as(mocks, gate_client, "viewer")
    assert gate_client.get("/skill-library", headers=WS).status_code == 200
    assert gate_client.post("/skill-library/links", headers=WS).status_code == 403


# --- a project made after the library ----------------------------------------------


def test_create_links_the_new_agents_project_before_saving_the_agent(client, mocks):
    _wire_create_success(mocks)
    mocks.skill_store.library_project.return_value = "lib-proj"

    assert client.post("/sessions/s1/create").status_code == 200

    mocks.client.set_skill_library.assert_awaited_once_with("proj-1", "lib-proj")
    mocks.skill_store.record_link.assert_awaited_once()
    assert mocks.skill_store.record_link.await_args.kwargs["state"] == "linked"
    order = [c[0] for c in mocks.client.mock_calls]
    assert order.index("set_skill_library") < order.index("create_agent")


def test_create_with_no_library_links_nothing(client, mocks):
    _wire_create_success(mocks)
    assert client.post("/sessions/s1/create").status_code == 200
    mocks.client.set_skill_library.assert_not_called()


def test_a_failed_link_at_create_is_recorded_and_the_agent_is_made(client, mocks):
    _wire_create_success(mocks)
    mocks.skill_store.library_project.return_value = "lib-proj"
    mocks.client.set_skill_library.side_effect = RuntimeError("TurnCall down")

    r = client.post("/sessions/s1/create")

    assert r.status_code == 200
    assert r.json()["data"]["created"] is True
    kwargs = mocks.skill_store.record_link.await_args.kwargs
    assert (kwargs["state"], kwargs["error"]) == ("failed", "RuntimeError: TurnCall down")


def test_the_first_webhook_bind_links_its_new_project(client, mocks):
    mocks.phones.ensure_workspace_webhook_project.return_value = (
        {"project_id": "wp-ws", "api_key": "tc_wp_ws"}, True,
    )
    mocks.client.bind_phone_number.return_value = {"id": "pn-9"}
    mocks.skill_store.library_project.return_value = "lib-proj"

    r = client.post(
        "/phone-numbers",
        json=_bind_body(routing_type="webhook", agent_id=None, server_url="https://crm.example.com/init"),
    )

    assert r.status_code == 200, r.text
    mocks.client.set_skill_library.assert_awaited_once_with("wp-ws", "lib-proj")
