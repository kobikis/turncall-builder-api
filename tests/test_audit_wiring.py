"""Every change a person makes through the Console is audited (ADR-0028).

What this guards:

- Every mutating route the app serves either takes an audit trail or is named
  below, with the reason it is out of ADR-0028's scope. A new route that does
  neither fails here, by name; so does an exemption whose route is gone.
- A route that takes a trail and returns normally without recording is a 500,
  not a change saved unrecorded. Because of that, every endpoint test of an
  audited route that expects a 2xx also proves the route recorded.
- The audit routes only read: the log, and what TurnCall recorded of an entry.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import audit, main
from app.audit import MUTATING_METHODS, Audit, api_routes, uses_audit
from app.deps import AuthContext

_DRAFT = "a Session is a draft conversation: nothing it holds reaches an agent until Create or Apply, which are audited"
_CALL = "places or tests a call: the audit log records configuration, never calls"
_OUT_OF_SCOPE = "not one of ADR-0028's resources (TurnCall's own log leaves these out too)"

# (method, path) -> why it is not audited.
NOT_AUDITED: dict[tuple[str, str], str] = {
    ("POST", "/sessions"): _DRAFT,
    ("POST", "/sessions/{sid}/messages"): _DRAFT,
    ("POST", "/sessions/{sid}/messages/stream"): _DRAFT,
    ("POST", "/sessions/{sid}/documents"): _DRAFT,
    ("DELETE", "/sessions/{sid}/documents/{name}"): _DRAFT,
    ("PUT", "/sessions/{sid}/config"): _DRAFT,
    ("POST", "/agents/{agent_id}/webrtc/connect"): _CALL,
    ("POST", "/agents/{agent_id}/chat"): _CALL,
    ("POST", "/agents/{agent_id}/calls/outbound"): _CALL,
    ("POST", "/agents/{agent_id}/do-not-call"): _OUT_OF_SCOPE,
    ("DELETE", "/agents/{agent_id}/do-not-call/{entry_id}"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/campaigns"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/campaigns/{campaign_id}/{action}"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/eval-scenarios"): _OUT_OF_SCOPE,
    ("PUT", "/agents/{agent_id}/eval-scenarios/{scenario_id}"): _OUT_OF_SCOPE,
    ("DELETE", "/agents/{agent_id}/eval-scenarios/{scenario_id}"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/eval-runs"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/eval-scenarios/from-call"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/eval-scenarios/from-session"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/knowledge/documents"): _OUT_OF_SCOPE,
    ("DELETE", "/agents/{agent_id}/knowledge/documents/{doc_id}"): _OUT_OF_SCOPE,
    ("PUT", "/agents/{agent_id}/knowledge/documents/{doc_id}/retrieval"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/knowledge/search"): "a search: a POST that reads",
    ("POST", "/agents/{agent_id}/takeaways"): _OUT_OF_SCOPE,
    ("PUT", "/agents/{agent_id}/takeaways/{takeaway_id}"): _OUT_OF_SCOPE,
    ("DELETE", "/agents/{agent_id}/takeaways/{takeaway_id}"): _OUT_OF_SCOPE,
    ("POST", "/agents/{agent_id}/ambience/sounds"): _OUT_OF_SCOPE,
    ("DELETE", "/agents/{agent_id}/ambience/sounds/{sound_id}"): _OUT_OF_SCOPE,
    ("POST", "/auth/signup"): "a person's own account and its first Workspace: there is no member to audit yet",
    ("POST", "/auth/login"): "a login session, not a change to a Workspace",
    ("POST", "/auth/logout"): "a login session, not a change to a Workspace",
    ("POST", "/workspaces"): "creates a Workspace rather than changing one; not in ADR-0028's list",
    ("POST", "/github/connection"): "a per-User credential (ADR-0013), in no Workspace",
    ("DELETE", "/github/connection"): "a per-User credential (ADR-0013), in no Workspace",
    ("POST", "/integrations/connect"): "only starts the hosted authorization; the callback records the Connected app once it exists",
    ("POST", "/integrations/token"): "called by an Agent Backend, not a person",
    ("POST", "/integrations/call-config"): "called by an Agent Backend, not a person",
}

# Not mutating by method, but where a change happens: audited all the same.
AUDITED_READS = {("GET", "/integrations/callback")}


def _routes() -> list[tuple[str, str, object]]:
    return [
        (method, path, route)
        for path, route in api_routes(main.app)
        for method in sorted(route.methods or ())
    ]


def test_every_mutating_route_is_audited_or_says_why_not():
    mutating = [
        (method, path, route)
        for method, path, route in _routes()
        if method in MUTATING_METHODS or (method, path) in AUDITED_READS
    ]
    # Sanity: the walk found the routes ADR-0028 names.
    found = {(m, p) for m, p, _ in mutating}
    assert {
        ("POST", "/sessions/{sid}/create"),
        ("POST", "/sessions/{sid}/apply"),
        ("PUT", "/agents/{agent_id}"),
        ("DELETE", "/agents/{agent_id}"),
        ("POST", "/phone-numbers"),
        ("PUT", "/phone-numbers/{phone_id}"),
        ("PUT", "/integrations/providers/{provider}"),
        ("DELETE", "/integrations/{account_id}"),
        ("PUT", "/members/{user_id}"),
        ("PUT", "/skills/{name}"),
        ("POST", "/agents/{agent_id}/github/push"),
        ("PUT", "/compliance"),
        ("PUT", "/agents/{agent_id}/compliance"),
        ("POST", "/compliance/apply"),
        ("POST", "/agents/{agent_id}/compliance/apply"),
    } <= found

    unaccounted = [
        (m, p) for m, p, route in mutating
        if not uses_audit(route) and (m, p) not in NOT_AUDITED
    ]
    assert unaccounted == [], f"mutating routes with no audit trail and no reason: {unaccounted}"
    both = [(m, p) for m, p, route in mutating if uses_audit(route) and (m, p) in NOT_AUDITED]
    assert both == [], f"audited routes still listed as exempt: {both}"


def test_no_exemption_outlives_its_route():
    served = {(m, p) for m, p, _ in _routes()}
    assert set(NOT_AUDITED) - served == set()


def test_the_audit_routes_only_read():
    assert sorted((m, p) for m, p, _ in _routes() if p.startswith("/audit")) == [
        ("GET", "/audit"),
        ("GET", "/audit/{entry_id}/engine"),
    ]


# --- the trail's teardown ------------------------------------------------------

_CTX = AuthContext(user={"id": "u1", "email": "a@x"}, workspace_id="w1", role="admin")


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/forgets")
    async def forgets(audit: Audit) -> dict:
        return {"ok": True}

    @app.post("/records")
    async def records(audit: Audit) -> dict:
        await audit.record(_CTX, "skill", "s", "created", after={"body": "x"})
        return {"ok": True}

    @app.post("/unchanged")
    async def unchanged(audit: Audit) -> dict:
        audit.unchanged()
        return {"ok": True}

    @app.post("/refuses")
    async def refuses(audit: Audit) -> dict:
        raise HTTPException(status_code=409, detail="no")

    return app


def test_a_route_that_returns_without_recording_is_a_500(mocks):
    client = TestClient(_app(), raise_server_exceptions=False)
    assert client.post("/forgets").status_code == 500
    assert client.post("/records").status_code == 200
    assert client.post("/unchanged").status_code == 200
    mocks.audit_store.insert.assert_awaited_once()


def test_a_failed_request_records_nothing_and_keeps_its_status(mocks):
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.post("/refuses")
    assert r.status_code == 409
    mocks.audit_store.insert.assert_not_called()


def test_an_unknown_resource_type_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(audit.AuditTrail().record(_CTX, "spaceship", "x", "created"))
