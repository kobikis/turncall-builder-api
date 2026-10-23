"""The Avatar picker's catalog (#126): /providers/avatar/{provider}/avatars.

Through the app's test client, with LiveAvatar and Tavus answered by an HTTP
mock transport. Response shapes follow each vendor's published API reference:
LiveAvatar's `GET /v1/avatars` (and `/v1/avatars/public`) answer
`{code, data: {count, next, previous, results: [{id, name, status, ...}]}, message}`;
Tavus's `GET /v2/faces` answers `{data: [{face_id, face_name, status, ...}], total_count}`.
"""

from __future__ import annotations

import httpx
import pytest

from app import provider_catalog
from app.routers import sessions


def _keys(monkeypatch, keys: dict[str, str]) -> None:
    async def workspace_keys(ctx):
        return keys

    monkeypatch.setattr(sessions, "_workspace_keys", workspace_keys)


def _serving(monkeypatch, handler) -> list[httpx.Request]:
    """Answer every provider request with `handler`; return what was asked."""
    asked: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return handler(request)

    provider_catalog._CACHE.clear()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        provider_catalog.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(recording), **kw),
    )
    return asked


@pytest.fixture(autouse=True)
def _no_builder_keys(monkeypatch):
    monkeypatch.delenv("HEYGEN_LIVE_AVATAR_API_KEY", raising=False)
    monkeypatch.delenv("TAVUS_API_KEY", raising=False)


def _liveavatar_page(*avatars: dict) -> dict:
    return {
        "code": 1000,
        "data": {"count": len(avatars), "next": None, "previous": None, "results": list(avatars)},
        "message": "ok",
    }


def _avatars(client, provider: str) -> list[dict[str, str]]:
    r = client.get(f"/providers/avatar/{provider}/avatars")
    assert r.status_code == 200, r.text
    return r.json()["data"]["avatars"]


def test_heygen_lists_the_accounts_avatars_by_name_with_the_workspaces_key(client, monkeypatch):
    def handler(request):
        assert request.url.host == "api.liveavatar.com"
        assert request.headers["x-api-key"] == "workspace-key"
        if request.url.path == "/v1/avatars":
            return httpx.Response(200, json=_liveavatar_page(
                {"id": "a-2", "name": "Zoe", "status": "ACTIVE"},
                {"id": "a-1", "name": "Ann", "status": "ACTIVE"},
            ))
        return httpx.Response(200, json=_liveavatar_page())

    _keys(monkeypatch, {"heygen": "workspace-key"})
    _serving(monkeypatch, handler)
    assert _avatars(client, "heygen") == [
        {"value": "a-1", "label": "Ann"},
        {"value": "a-2", "label": "Zoe"},
    ]


def test_heygen_offers_the_stock_avatars_after_the_accounts_own(client, monkeypatch):
    # Custom avatars are a paid add-on, so most accounts run a stock one — the
    # Sandbox avatar among them. The account's own come first.
    def handler(request):
        if request.url.path == "/v1/avatars/public":
            return httpx.Response(200, json=_liveavatar_page(
                {"id": "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a", "name": "Sandbox Wayne", "status": "ACTIVE"},
                {"id": "s-1", "name": "Ann Therapist", "status": "ACTIVE"},
            ))
        return httpx.Response(200, json=_liveavatar_page({"id": "own", "name": "Our Spokesperson"}))

    _keys(monkeypatch, {"heygen": "k"})
    asked = _serving(monkeypatch, handler)
    assert _avatars(client, "heygen") == [
        {"value": "own", "label": "Our Spokesperson"},
        {"value": "s-1", "label": "Ann Therapist"},
        {"value": "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a", "label": "Sandbox Wayne"},
    ]
    assert all(r.url.params["page_size"] == "100" for r in asked)  # LiveAvatar's maximum


def test_heygen_leaves_out_avatars_that_cannot_run_a_call(client, monkeypatch):
    def handler(request):
        if request.url.path == "/v1/avatars/public":
            return httpx.Response(200, json=_liveavatar_page())
        return httpx.Response(200, json=_liveavatar_page(
            {"id": "ready", "name": "Ready", "status": "ACTIVE", "is_expired": False},
            {"id": "training", "name": "Training", "status": "INIT"},
            {"id": "failed", "name": "Failed", "status": "FAILED"},
            {"id": "expired", "name": "Expired", "status": "ACTIVE", "is_expired": True},
        ))

    _keys(monkeypatch, {"heygen": "k"})
    _serving(monkeypatch, handler)
    assert _avatars(client, "heygen") == [{"value": "ready", "label": "Ready"}]


def test_tavus_lists_the_accounts_replicas_by_name_with_the_workspaces_key(client, monkeypatch):
    # Tavus renamed replicas to faces; /v2/replicas and `replica_id` stay as
    # aliases, and the ID TurnCall takes is still `replica_id`.
    def handler(request):
        assert request.url.host == "tavusapi.com"
        assert request.url.path == "/v2/faces"
        assert request.url.params["limit"] == "100"
        assert request.headers["x-api-key"] == "workspace-key"
        return httpx.Response(200, json={"data": [
            {"face_id": "r-2", "face_name": "Zed", "status": "completed"},
            {"face_id": "r-1", "face_name": "Anna", "status": "completed"},
            {"face_id": "r-3", "face_name": "Still Training", "status": "started"},
            {"face_id": "r-4", "face_name": "Broken", "status": "error"},
            {"replica_id": "r-5", "replica_name": "Legacy Row"},
        ], "total_count": 5})

    _keys(monkeypatch, {"tavus": "workspace-key"})
    _serving(monkeypatch, handler)
    assert _avatars(client, "tavus") == [
        {"value": "r-1", "label": "Anna"},
        {"value": "r-5", "label": "Legacy Row"},
        {"value": "r-2", "label": "Zed"},
    ]


@pytest.mark.parametrize("provider", ["heygen", "tavus"])
def test_no_key_asks_nothing_and_lists_nothing(client, monkeypatch, provider):
    def handler(request):
        raise AssertionError("no key: nothing is asked of the provider")

    _keys(monkeypatch, {})
    _serving(monkeypatch, handler)
    assert _avatars(client, provider) == []


@pytest.mark.parametrize(("provider", "env"), [
    ("heygen", "HEYGEN_LIVE_AVATAR_API_KEY"),
    ("tavus", "TAVUS_API_KEY"),
])
def test_without_a_workspace_key_the_builders_own_is_used(client, monkeypatch, provider, env):
    def handler(request):
        assert request.headers["x-api-key"] == "builder-key"
        if provider == "tavus":
            return httpx.Response(200, json={"data": [{"face_id": "r", "face_name": "R"}]})
        if request.url.path == "/v1/avatars/public":
            return httpx.Response(200, json=_liveavatar_page())
        return httpx.Response(200, json=_liveavatar_page({"id": "a", "name": "A"}))

    monkeypatch.setenv(env, "builder-key")
    _keys(monkeypatch, {})
    _serving(monkeypatch, handler)
    assert len(_avatars(client, provider)) == 1


@pytest.mark.parametrize("provider", ["heygen", "tavus"])
def test_a_refused_key_lists_nothing(client, monkeypatch, provider):
    _keys(monkeypatch, {provider: "bad"})
    _serving(monkeypatch, lambda request: httpx.Response(401, json={"message": "Invalid access token"}))
    assert _avatars(client, provider) == []


def test_the_list_is_cached_per_key_so_a_new_key_repopulates_at_once(client, monkeypatch):
    keys: dict[str, str] = {"tavus": "old"}

    def handler(request):
        if request.headers["x-api-key"] == "old":
            return httpx.Response(401, json={"message": "Invalid access token"})
        return httpx.Response(200, json={"data": [{"face_id": "r", "face_name": "Replica"}]})

    async def workspace_keys(ctx):
        return dict(keys)

    monkeypatch.setattr(sessions, "_workspace_keys", workspace_keys)
    asked = _serving(monkeypatch, handler)
    assert _avatars(client, "tavus") == []
    assert _avatars(client, "tavus") == []
    assert len(asked) == 1  # the refusal is cached under the old key

    keys["tavus"] = "new"
    assert _avatars(client, "tavus") == [{"value": "r", "label": "Replica"}]
    assert _avatars(client, "tavus") == [{"value": "r", "label": "Replica"}]
    assert len(asked) == 2


def test_a_transient_failure_serves_the_last_list(client, monkeypatch):
    up = {"ok": True}

    def handler(request):
        if not up["ok"]:
            raise httpx.ConnectError("tavus is down")
        return httpx.Response(200, json={"data": [{"face_id": "r", "face_name": "Replica"}]})

    _keys(monkeypatch, {"tavus": "k"})
    _serving(monkeypatch, handler)
    assert _avatars(client, "tavus") == [{"value": "r", "label": "Replica"}]

    up["ok"] = False
    for entry in provider_catalog._CACHE:  # the hour is up
        provider_catalog._CACHE[entry] = (0.0, provider_catalog._CACHE[entry][1])
    assert _avatars(client, "tavus") == [{"value": "r", "label": "Replica"}]


def test_an_unknown_provider_is_refused(client, monkeypatch):
    def handler(request):
        raise AssertionError("nothing is asked for a provider that is not an avatar's")

    _keys(monkeypatch, {"openai": "k"})
    _serving(monkeypatch, handler)
    r = client.get("/providers/avatar/openai/avatars")
    assert r.status_code == 422
    assert "heygen" in r.text and "tavus" in r.text


def test_any_member_may_read_it_and_nobody_else(gate_client, mocks, monkeypatch):
    from app.routers.auth import COOKIE_NAME
    from tests.conftest import TEST_WORKSPACE_ID

    _keys(monkeypatch, {})
    url, header = "/providers/avatar/tavus/avatars", {"X-Workspace-Id": TEST_WORKSPACE_ID}
    assert gate_client.get(url, headers=header).status_code == 401

    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}
    mocks.auth_store.resolve_membership.return_value = "viewer"
    gate_client.cookies.set(COOKIE_NAME, "live")
    assert gate_client.get(url, headers=header).status_code == 200
