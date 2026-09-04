"""Multi-workspace list + create (#33) on the TestClient seam.

These routes are login-gated via `current_user` (session cookie), so the tests
drive the mocked auth_store: resolve_session for the logged-in user, and
list_workspaces_for_user / create_workspace for the data. The SQL behind them
(list reflects memberships, create grants admin + passes the gate) is covered
against a real DB in test_workspaces_db.py.
"""

from app.routers.auth import COOKIE_NAME


def _login(mocks) -> None:
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "u@x"}


def test_list_requires_login(client, mocks):
    r = client.get("/workspaces")  # no cookie
    assert r.status_code == 401
    mocks.auth_store.list_workspaces_for_user.assert_not_called()


def test_create_requires_login(client, mocks):
    r = client.post("/workspaces", json={"name": "New"})
    assert r.status_code == 401
    mocks.auth_store.create_workspace.assert_not_called()


def test_list_reflects_memberships(client, mocks):
    _login(mocks)
    mocks.auth_store.list_workspaces_for_user.return_value = [
        {"id": "w1", "name": "Default", "role": "admin"},
        {"id": "w2", "name": "Acme", "role": "viewer"},
    ]
    client.cookies.set(COOKIE_NAME, "live")
    r = client.get("/workspaces")
    assert r.status_code == 200
    data = r.json()["data"]["workspaces"]
    assert [w["role"] for w in data] == ["admin", "viewer"]
    # Scoped to the caller — the list is built from their user id.
    assert mocks.auth_store.list_workspaces_for_user.await_args.args[1] == "u1"


def test_create_yields_admin(client, mocks):
    _login(mocks)
    mocks.auth_store.create_workspace.return_value = {
        "id": "w-new",
        "name": "Acme",
        "role": "admin",
    }
    client.cookies.set(COOKIE_NAME, "live")
    r = client.post("/workspaces", json={"name": "  Acme  "})
    assert r.status_code == 200
    assert r.json()["data"] == {"id": "w-new", "name": "Acme", "role": "admin"}
    # Name is trimmed before it reaches the store.
    assert mocks.auth_store.create_workspace.await_args.args[1:] == ("u1", "Acme")


def test_create_rejects_blank_name(client, mocks):
    _login(mocks)
    client.cookies.set(COOKIE_NAME, "live")
    r = client.post("/workspaces", json={"name": ""})
    assert r.status_code == 422  # pydantic min_length
    mocks.auth_store.create_workspace.assert_not_called()


def test_create_rejects_whitespace_only_name(client, mocks):
    """"   " is stripped before validation, so min_length rejects it — it must not
    reach the store as an empty name."""
    _login(mocks)
    client.cookies.set(COOKIE_NAME, "live")
    r = client.post("/workspaces", json={"name": "   "})
    assert r.status_code == 422
    mocks.auth_store.create_workspace.assert_not_called()
