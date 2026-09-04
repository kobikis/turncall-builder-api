"""Auth endpoints on the TestClient(main.app) seam: signup / login / logout / me.

auth_store is mocked (conftest), so these assert router behaviour — cookie
issuance, status codes, envelope shape, and that the cookie→User dependency
gates /me — not real DB or hashing."""

from app import auth_store
from app.routers.auth import COOKIE_NAME


def test_signup_creates_user_and_sets_session_cookie(client, mocks):
    mocks.auth_store.signup.return_value = {
        "id": "u1", "email": "jane@acme.com", "workspace_id": "w1"
    }
    mocks.auth_store.create_login_session.return_value = "tok-abc"

    r = client.post("/auth/signup", json={"email": "jane@acme.com", "password": "hunter2xx"})

    assert r.status_code == 200
    assert r.json()["data"]["user"] == {"id": "u1", "email": "jane@acme.com"}
    assert r.cookies.get(COOKIE_NAME) == "tok-abc"
    # Default workspace name derived from the email local part.
    assert mocks.auth_store.signup.call_args.kwargs["workspace_name"] == "jane's Workspace"


def test_signup_never_returns_password(client, mocks):
    mocks.auth_store.signup.return_value = {"id": "u1", "email": "j@acme.com", "workspace_id": "w1"}
    mocks.auth_store.create_login_session.return_value = "t"
    r = client.post("/auth/signup", json={"email": "j@acme.com", "password": "hunter2xx"})
    assert "password" not in r.text and "hash" not in r.text


def test_duplicate_email_signup_rejected(client, mocks):
    mocks.auth_store.signup.side_effect = auth_store.DuplicateEmail("j@acme.com")
    r = client.post("/auth/signup", json={"email": "j@acme.com", "password": "hunter2xx"})
    assert r.status_code == 409


def test_signup_rejects_short_password(client, mocks):
    r = client.post("/auth/signup", json={"email": "j@acme.com", "password": "short"})
    assert r.status_code == 422
    mocks.auth_store.signup.assert_not_called()


def test_signup_rejects_non_email(client, mocks):
    r = client.post("/auth/signup", json={"email": "not-an-email", "password": "hunter2xx"})
    assert r.status_code == 422
    mocks.auth_store.signup.assert_not_called()


def test_login_verifies_password_and_sets_cookie(client, mocks):
    mocks.auth_store.get_user_by_email.return_value = {
        "id": "u1", "email": "j@acme.com", "password_hash": "$argon2id$hash"
    }
    mocks.auth_store.verify_password.return_value = True
    mocks.auth_store.create_login_session.return_value = "tok-xyz"

    r = client.post("/auth/login", json={"email": "j@acme.com", "password": "hunter2xx"})

    assert r.status_code == 200
    assert r.cookies.get(COOKIE_NAME) == "tok-xyz"


def test_login_wrong_password_401(client, mocks):
    mocks.auth_store.get_user_by_email.return_value = {
        "id": "u1", "email": "j@acme.com", "password_hash": "$argon2id$hash"
    }
    mocks.auth_store.verify_password.return_value = False
    r = client.post("/auth/login", json={"email": "j@acme.com", "password": "wrongpass1"})
    assert r.status_code == 401
    assert COOKIE_NAME not in r.cookies


def test_login_unknown_email_401(client, mocks):
    mocks.auth_store.get_user_by_email.return_value = None
    r = client.post("/auth/login", json={"email": "ghost@acme.com", "password": "hunter2xx"})
    assert r.status_code == 401


def test_logout_deletes_session_and_clears_cookie(client, mocks):
    client.cookies.set(COOKIE_NAME, "tok-live")
    r = client.post("/auth/logout")
    assert r.status_code == 200
    mocks.auth_store.delete_session.assert_awaited_once()
    assert mocks.auth_store.delete_session.await_args.args[1] == "tok-live"
    # We emit a deletion cookie so the browser drops it (Max-Age=0, past expiry).
    set_cookie = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in set_cookie and "Max-Age=0" in set_cookie


def test_me_requires_authentication(client, mocks):
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_resolves_current_user_from_cookie(client, mocks):
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "j@acme.com"}
    client.cookies.set(COOKIE_NAME, "tok-live")
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["data"]["user"] == {"id": "u1", "email": "j@acme.com"}


def test_me_rejects_invalid_session_cookie(client, mocks):
    mocks.auth_store.resolve_session.return_value = None
    client.cookies.set(COOKIE_NAME, "stale")
    r = client.get("/auth/me")
    assert r.status_code == 401
