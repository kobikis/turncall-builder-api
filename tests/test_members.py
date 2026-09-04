"""Team management (#34) on the TestClient seam.

Admin ops (invite / list / change-role / remove) run under `client`, which
overrides require_admin to an admin AuthContext (conftest). Non-admin denials use
`gate_client` (real gate) with resolve_membership returning a lower role → 403.
Accept is login-gated: driven via the mocked resolve_session. The SQL behind all
of it (invite→join, idempotency, last-admin guard, isolation) is covered against
a real DB in test_members_db.py.
"""

from app import auth_store  # exception classes stay real (conftest keeps them)
from app.routers.auth import COOKIE_NAME
from tests.conftest import TEST_WORKSPACE_ID

WS_HEADER = {"X-Workspace-Id": TEST_WORKSPACE_ID}
MEMBER_ID = "11111111-1111-1111-1111-111111111111"  # a valid UUID path param
GHOST_ID = "22222222-2222-2222-2222-222222222222"


def _login(mocks, email="u@x.com") -> None:
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": email}


# --- invite (admin) -----------------------------------------------------------


def test_admin_creates_invite(client, mocks):
    mocks.auth_store.create_invite.return_value = {
        "id": "inv1", "workspace_id": TEST_WORKSPACE_ID,
        "email": "new@x.com", "role": "editor", "token": "tok",
    }
    r = client.post("/members/invites", json={"email": "new@x.com", "role": "editor"})
    assert r.status_code == 200
    kw = mocks.auth_store.create_invite.await_args.kwargs
    assert kw["workspace_id"] == TEST_WORKSPACE_ID
    assert kw["email"] == "new@x.com"
    assert kw["role"] == "editor"
    assert kw["invited_by"] == "u-test"  # from the admin AuthContext


def test_invite_rejects_bad_email(client, mocks):
    r = client.post("/members/invites", json={"email": "nope", "role": "editor"})
    assert r.status_code == 422
    mocks.auth_store.create_invite.assert_not_called()


def test_invite_rejects_bad_role(client, mocks):
    r = client.post("/members/invites", json={"email": "a@x.com", "role": "owner"})
    assert r.status_code == 422
    mocks.auth_store.create_invite.assert_not_called()


# --- accept (login-gated) -----------------------------------------------------


def test_accept_requires_login(client, mocks):
    r = client.post("/invites/accept", json={"token": "tok"})  # no cookie
    assert r.status_code == 401
    mocks.auth_store.accept_invite.assert_not_called()


def test_accept_creates_membership(client, mocks):
    _login(mocks, "new@x.com")
    mocks.auth_store.accept_invite.return_value = {
        "workspace_id": "w9", "role": "editor", "already_member": False,
    }
    client.cookies.set(COOKIE_NAME, "live")
    r = client.post("/invites/accept", json={"token": "tok"})
    assert r.status_code == 200
    assert r.json()["data"]["role"] == "editor"
    kw = mocks.auth_store.accept_invite.await_args.kwargs
    assert kw["token"] == "tok" and kw["user_id"] == "u1" and kw["user_email"] == "new@x.com"


def test_accept_unknown_token_404(client, mocks):
    _login(mocks)
    mocks.auth_store.accept_invite.side_effect = auth_store.InviteNotFound("tok")
    client.cookies.set(COOKIE_NAME, "live")
    assert client.post("/invites/accept", json={"token": "tok"}).status_code == 404


def test_accept_wrong_email_403(client, mocks):
    """Invite-only: a logged-in user whose email isn't the invited one is refused —
    a leaked token alone can't join."""
    _login(mocks, "someoneelse@x.com")
    mocks.auth_store.accept_invite.side_effect = auth_store.InviteEmailMismatch("x")
    client.cookies.set(COOKIE_NAME, "live")
    assert client.post("/invites/accept", json={"token": "tok"}).status_code == 403


# --- list / change / remove (admin) -------------------------------------------


def test_admin_lists_members(client, mocks):
    mocks.auth_store.list_members.return_value = [
        {"user_id": "u1", "email": "a@x.com", "role": "admin"},
    ]
    r = client.get("/members")
    assert r.status_code == 200
    assert r.json()["data"]["members"][0]["role"] == "admin"


def test_admin_changes_role(client, mocks):
    mocks.auth_store.change_member_role.return_value = True
    r = client.put(f"/members/{MEMBER_ID}", json={"role": "viewer"})
    assert r.status_code == 200
    assert mocks.auth_store.change_member_role.await_args.args[1:] == (
        TEST_WORKSPACE_ID, MEMBER_ID, "viewer"
    )


def test_change_role_member_not_found_404(client, mocks):
    mocks.auth_store.change_member_role.return_value = False
    assert client.put(f"/members/{GHOST_ID}", json={"role": "viewer"}).status_code == 404


def test_change_role_last_admin_409(client, mocks):
    mocks.auth_store.change_member_role.side_effect = auth_store.LastAdmin("u2")
    assert client.put(f"/members/{MEMBER_ID}", json={"role": "viewer"}).status_code == 409


def test_admin_removes_member(client, mocks):
    mocks.auth_store.remove_member.return_value = True
    r = client.delete(f"/members/{MEMBER_ID}")
    assert r.status_code == 200
    assert mocks.auth_store.remove_member.await_args.args[1:] == (TEST_WORKSPACE_ID, MEMBER_ID)


def test_remove_member_not_found_404(client, mocks):
    mocks.auth_store.remove_member.return_value = False
    assert client.delete(f"/members/{GHOST_ID}").status_code == 404


def test_remove_last_admin_409(client, mocks):
    mocks.auth_store.remove_member.side_effect = auth_store.LastAdmin("u2")
    assert client.delete(f"/members/{MEMBER_ID}").status_code == 409


# --- non-admin denials (real gate) --------------------------------------------


def _as_editor(mocks) -> None:
    mocks.auth_store.resolve_session.return_value = {"id": "u1", "email": "e@x.com"}
    mocks.auth_store.resolve_membership.return_value = "editor"


def test_editor_cannot_invite(gate_client, mocks):
    _as_editor(mocks)
    gate_client.cookies.set(COOKIE_NAME, "live")
    r = gate_client.post("/members/invites", json={"email": "a@x.com", "role": "viewer"}, headers=WS_HEADER)
    assert r.status_code == 403
    mocks.auth_store.create_invite.assert_not_called()


def test_editor_cannot_list_members(gate_client, mocks):
    _as_editor(mocks)
    gate_client.cookies.set(COOKIE_NAME, "live")
    assert gate_client.get("/members", headers=WS_HEADER).status_code == 403


def test_editor_cannot_change_role(gate_client, mocks):
    _as_editor(mocks)
    gate_client.cookies.set(COOKIE_NAME, "live")
    r = gate_client.put(f"/members/{MEMBER_ID}", json={"role": "admin"}, headers=WS_HEADER)
    assert r.status_code == 403
    mocks.auth_store.change_member_role.assert_not_called()


def test_editor_cannot_remove_member(gate_client, mocks):
    _as_editor(mocks)
    gate_client.cookies.set(COOKIE_NAME, "live")
    assert gate_client.delete(f"/members/{MEMBER_ID}", headers=WS_HEADER).status_code == 403


