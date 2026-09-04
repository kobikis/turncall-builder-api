"""Google OIDC login (#32) on the TestClient seam, provider mocked.

The real Authlib exchange (`auth._exchange`) is swapped for a stub returning
Google claims, and auth_store is mocked (conftest). These drive the callback's
verify → link/create → issue-cookie path without any network or DB: the
link-existing vs create-new distinction is asserted via the mocked
`upsert_google_user` return, and its SQL is covered separately in
test_google_upsert_db.py.
"""

from types import SimpleNamespace

import pytest

from app.routers import auth as auth_router
from app.routers.auth import COOKIE_NAME


@pytest.fixture
def google_configured(monkeypatch):
    """Pretend Google is configured (env-less test runs leave client_id empty)."""
    monkeypatch.setattr(
        auth_router,
        "_settings",
        SimpleNamespace(google_client_id="test-client", public_base_url=""),
    )


def _mock_exchange(monkeypatch, userinfo: dict) -> None:
    async def fake_exchange(request):
        return userinfo

    monkeypatch.setattr(auth_router, "_exchange", fake_exchange)


def test_callback_creates_new_user(client, mocks, google_configured, monkeypatch):
    """Unknown verified email → upsert creates a User+Workspace, session issued."""
    _mock_exchange(monkeypatch, {"email": "New@Example.com", "email_verified": True})
    mocks.auth_store.upsert_google_user.return_value = {"id": "u-new", "email": "new@example.com"}
    mocks.auth_store.create_login_session.return_value = "sess-new"

    r = client.get("/auth/google/callback", follow_redirects=False)

    assert r.status_code == 303
    assert r.cookies.get(COOKIE_NAME) == "sess-new"
    # Email is normalised (lowercased) before it reaches the store.
    assert mocks.auth_store.upsert_google_user.await_args.kwargs["email"] == "new@example.com"
    assert mocks.auth_store.create_login_session.await_args.args[1] == "u-new"


def test_callback_links_existing_user(client, mocks, google_configured, monkeypatch):
    """Known verified email → upsert returns the existing User; same cookie flow.
    No new account: the router logs into whatever id the store resolves."""
    _mock_exchange(monkeypatch, {"email": "admin@x.com", "email_verified": True})
    mocks.auth_store.upsert_google_user.return_value = {"id": "u-existing", "email": "admin@x.com"}
    mocks.auth_store.create_login_session.return_value = "sess-existing"

    r = client.get("/auth/google/callback", follow_redirects=False)

    assert r.status_code == 303
    assert r.cookies.get(COOKIE_NAME) == "sess-existing"
    assert mocks.auth_store.create_login_session.await_args.args[1] == "u-existing"


def test_callback_rejects_unverified_email(client, mocks, google_configured, monkeypatch):
    """An unverified Google email must not claim/create an account."""
    _mock_exchange(monkeypatch, {"email": "spoof@x.com", "email_verified": False})

    r = client.get("/auth/google/callback", follow_redirects=False)

    assert r.status_code == 401
    mocks.auth_store.upsert_google_user.assert_not_called()


def test_callback_rejects_missing_email(client, mocks, google_configured, monkeypatch):
    _mock_exchange(monkeypatch, {"email_verified": True})  # no email claim

    r = client.get("/auth/google/callback", follow_redirects=False)

    assert r.status_code == 401
    mocks.auth_store.upsert_google_user.assert_not_called()


def test_callback_maps_provider_error_to_401(client, mocks, google_configured, monkeypatch):
    from authlib.integrations.starlette_client import OAuthError

    async def boom(request):
        raise OAuthError("bad code")

    monkeypatch.setattr(auth_router, "_exchange", boom)

    r = client.get("/auth/google/callback", follow_redirects=False)

    assert r.status_code == 401
    mocks.auth_store.create_login_session.assert_not_called()


def test_routes_503_when_not_configured(client, monkeypatch):
    """Empty client_id (Google not set up) → the routes refuse cleanly, not 500."""
    monkeypatch.setattr(
        auth_router, "_settings", SimpleNamespace(google_client_id="", public_base_url="")
    )
    assert client.get("/auth/google/callback", follow_redirects=False).status_code == 503
    assert client.get("/auth/google/login", follow_redirects=False).status_code == 503
