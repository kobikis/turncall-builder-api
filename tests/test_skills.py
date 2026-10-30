"""A Workspace's Skills and its Skill library (#131), on the
TestClient seam over a MockTransport TurnCall: what each route asks TurnCall,
with which key, and what it answers the Console."""

from __future__ import annotations

import pytest

from app.routers.auth import COOKIE_NAME as COOKIE
from tests.conftest import TEST_WORKSPACE_ID, audit_entries
from tests.turncall_fake import PLATFORM_KEY, FakeLibraryStore, FakeTurnCall

WS = {"X-Workspace-Id": TEST_WORKSPACE_ID}
LIBRARY_KEY = "Bearer tc_proj-1"

VERIFY = {
    "name": "verify_caller",
    "description": "When the caller asks about their account",
    "instructions": "Ask for the account number and the postcode.",
    "tools": [{
        "name": "check_account",
        "description": "Look the account up",
        "webhook_url": "https://tools.example.com/check",
        "webhook_secret": "s" * 32,
    }],
}


@pytest.fixture
def tc(mocks):
    """The fake TurnCall, and the library store as its SQL behaves."""
    fake = FakeTurnCall()
    fake.install()
    fake.store = FakeLibraryStore(mocks)
    return fake


def _create(client, **over):
    return client.post("/skills", json={**VERIFY, **over})


# --- the library ------------------------------------------------------------------


def test_the_first_skill_makes_the_library(client, mocks, tc):
    r = _create(client)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["name"] == "verify_caller"
    # The platform bootstrap: a project, then an admin key to it.
    (project,) = tc.sent("POST", "/v1/projects")
    assert project.headers["X-Platform-Key"] == PLATFORM_KEY
    (key,) = tc.sent("POST", "/v1/api-keys")
    assert key.url.params["project_id"] == "proj-1"
    # The Skill itself, in the library, on its key.
    (skill,) = tc.sent("POST", "/v1/skills")
    assert skill.headers["Authorization"] == LIBRARY_KEY


def test_a_second_skill_reuses_the_library(client, tc):
    assert _create(client).status_code == 200
    assert _create(client, name="take_payment").status_code == 200
    assert len(tc.sent("POST", "/v1/projects")) == 1
    assert len(tc.sent("POST", "/v1/skills")) == 2


def test_listing_before_any_skill_asks_turncall_nothing(client, tc):
    r = client.get("/skills")
    assert r.json()["data"] == {"library": None, "skills": []}
    assert tc.requests == []


def test_no_platform_key_is_a_502_and_makes_nothing(client, mocks, tc):
    tc.refuse("POST", "/v1/projects", 401, {"success": False, "error": "platform key"})
    r = _create(client)
    assert r.status_code == 502
    assert "PLATFORM_API_KEY" in r.json()["detail"]
    assert tc.sent("POST", "/v1/skills") == []
    assert audit_entries(mocks) == []


# --- every route reaches TurnCall's on the library's key ---------------------------


def test_every_route_reaches_turncalls_skill_route_on_the_library_key(client, tc):
    v1 = _create(client).json()["data"]["id"]
    assert client.post(f"/skill-versions/{v1}/publish").status_code == 200
    v2 = _create(client, instructions="Ask for the postcode only.").json()["data"]["id"]
    assert client.put(f"/skill-versions/{v2}", json={"description": "Billing callers"}).status_code == 200
    assert client.post(f"/skill-versions/{v2}/publish").status_code == 200
    assert client.post(f"/skill-versions/{v1}/rollback").status_code == 200
    assert client.get(f"/skill-versions/{v2}").status_code == 200

    listed = client.get("/skills").json()["data"]
    assert listed["library"] == {"project_id": "proj-1"}
    (summary,) = listed["skills"]
    assert summary["name"] == "verify_caller"
    assert summary["live"] == {"id": v1, "version": 1}
    assert summary["draft"] is None
    assert summary["latest"] == {"id": v2, "version": 2, "state": "archived"}
    assert summary["versions"] == 2

    one = client.get("/skills/verify_caller").json()["data"]
    assert [v["version"] for v in one["versions"]] == [2, 1]
    assert one["live"] == {"id": v1, "version": 1}

    assert client.delete(f"/skill-versions/{v2}").status_code == 200

    skill_calls = [r for r in tc.requests if r.url.path.startswith("/v1/skills")]
    assert all(r.headers["Authorization"] == LIBRARY_KEY for r in skill_calls)
    paths = {(r.method, r.url.path) for r in skill_calls}
    assert ("PUT", f"/v1/skills/{v2}") in paths
    assert ("POST", f"/v1/skills/{v2}/publish") in paths
    assert ("POST", f"/v1/skills/{v1}/rollback") in paths
    assert ("DELETE", f"/v1/skills/{v2}") in paths
    assert ("GET", "/v1/skills") in paths
    # An edit sends only what changed: TurnCall keeps the rest.
    (edit,) = tc.sent("PUT", f"/v1/skills/{v2}")
    assert edit.read() == b'{"description":"Billing callers"}'


def test_an_unknown_skill_is_a_404(client, tc):
    _create(client)
    assert client.get("/skills/nothing_here").status_code == 404


def test_a_name_that_is_not_a_skill_name_never_reaches_turncall(client, tc):
    _create(client)
    before = len(tc.requests)
    assert client.get("/skills/Not..A-Name").status_code == 422
    assert client.get("/skills/..%2Fagents").status_code in (404, 422)
    assert len(tc.requests) == before


def test_a_version_route_before_the_library_is_a_404(client, tc):
    r = client.get("/skill-versions/00000000-0000-0000-0000-000000000001")
    assert r.status_code == 404
    assert tc.requests == []


# --- TurnCall's refusals name the field ---------------------------------------------


def test_turncalls_422_comes_back_naming_the_tool(client, mocks, tc):
    tc.refuse("POST", "/v1/skills", 422, {
        "success": False,
        "error": "Validation error",
        "code": "validation_error",
        "details": {"errors": [{
            "loc": ["body", "tools"],
            "msg": "Value error, tools[0] 'check_account' webhook_url: plain http is not "
            "allowed; tools[1] 'end_call' name: is a built-in",
        }]},
    })
    r = _create(client)
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "validation_error"
    assert body["errors"] == [
        {"field": "tools[0]", "message": "tools[0] 'check_account' webhook_url: plain http is not allowed"},
        {"field": "tools[1]", "message": "tools[1] 'end_call' name: is a built-in"},
    ]
    # The library was made and is recorded; the refused Skill is not.
    assert [e["resource_type"] for e in audit_entries(mocks)] == ["skill_library"]


def test_a_field_turncall_names_by_location(client, tc):
    tc.refuse("POST", "/v1/skills", 422, {
        "success": False, "error": "Validation error", "code": "validation_error",
        "details": {"errors": [{"loc": ["body", "name"], "msg": "String should match pattern"}]},
    })
    r = _create(client, name="Verify")
    assert r.json()["errors"] == [{"field": "name", "message": "String should match pattern"}]


def test_editing_a_published_version_is_turncalls_409(client, tc):
    v1 = _create(client).json()["data"]["id"]
    client.post(f"/skill-versions/{v1}/publish")
    tc.refuse("PUT", f"/v1/skills/{v1}", 409, {
        "success": False, "error": "Can only edit a draft skill version", "code": "conflict",
    })
    r = client.put(f"/skill-versions/{v1}", json={"description": "x"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Can only edit a draft skill version"


# --- roles --------------------------------------------------------------------------


def _login_as(mocks, gate_client, role):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = role
    gate_client.cookies.set(COOKIE, "live")


def test_a_viewer_reads_and_cannot_write(gate_client, mocks, tc):
    _login_as(mocks, gate_client, "viewer")
    assert gate_client.get("/skills", headers=WS).status_code == 200
    assert gate_client.post("/skills", json=VERIFY, headers=WS).status_code == 403
    v = "00000000-0000-0000-0000-000000000001"
    assert gate_client.put(f"/skill-versions/{v}", json={}, headers=WS).status_code == 403
    assert gate_client.post(f"/skill-versions/{v}/publish", headers=WS).status_code == 403
    assert gate_client.post(f"/skill-versions/{v}/rollback", headers=WS).status_code == 403
    assert gate_client.delete(f"/skill-versions/{v}", headers=WS).status_code == 403
    assert tc.requests == []


def test_an_editor_reads_and_writes(gate_client, mocks, tc):
    _login_as(mocks, gate_client, "editor")
    created = gate_client.post("/skills", json=VERIFY, headers=WS)
    assert created.status_code == 200
    v1 = created.json()["data"]["id"]
    assert gate_client.post(f"/skill-versions/{v1}/publish", headers=WS).status_code == 200
    assert gate_client.get("/skills", headers=WS).status_code == 200


# --- the audit trail ---------------------------------------------------------------


def test_every_write_is_an_audit_entry_with_the_user(client, mocks, tc):
    v1 = _create(client).json()["data"]["id"]
    client.put(f"/skill-versions/{v1}", json={"instructions": "Postcode first."})
    client.post(f"/skill-versions/{v1}/publish")
    v2 = _create(client).json()["data"]["id"]
    client.delete(f"/skill-versions/{v2}")

    entries = audit_entries(mocks)
    assert [(e["resource_type"], e["action"]) for e in entries] == [
        ("skill_library", "created"),
        ("skill", "created"),
        ("skill", "updated"),
        ("skill", "published"),
        ("skill", "created"),
        ("skill", "deleted"),
    ]
    assert all(e["user_id"] == "u-test" and e["role"] == "admin" for e in entries)
    created = entries[1]
    assert created["resource_id"] == v1
    assert created["resource_name"] == "verify_caller"
    # A tool's secret is never written down.
    assert created["changes"]["/tools/0/webhook_secret"] == {"before": None, "after": "***", "secret": True}
    assert entries[2]["changes"] == {
        "/instructions": {"before": VERIFY["instructions"], "after": "Postcode first."}
    }
    assert entries[3]["changes"] == {"/state": {"before": "draft", "after": "published"}}
    # Each links to what TurnCall recorded of it, in the library's project.
    assert entries[0]["engine_links"][0]["project_id"] == "proj-1"
    assert entries[1]["engine_links"]
