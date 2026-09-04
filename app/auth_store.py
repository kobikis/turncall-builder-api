"""Identity + login-session persistence (ADR-0011).

Owns users, workspaces, memberships, user_sessions. Passwords are only ever
stored as argon2id hashes and never returned. All SQL lives here so the router
stays thin and the TestClient seam can mock this whole module (see conftest).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_ph = PasswordHasher()  # argon2id defaults

SESSION_TTL = timedelta(days=14)

# A valid argon2id hash of a value nothing matches. Login verifies against this
# when the email is unknown, so the response time doesn't reveal whether an
# account exists (constant-time-ish: the argon2 cost is always paid).
_DUMMY_HASH = _ph.hash("dummy-sentinel-never-matches")


class DuplicateEmail(Exception):
    """Signup with an email that already has a user."""


class InviteNotFound(Exception):
    """Accept called with an unknown or already-used invite token."""


class InviteEmailMismatch(Exception):
    """Accept called by a user whose email isn't the one the invite was for."""


class LastAdmin(Exception):
    """Removing/demoting this member would leave the Workspace with no admin."""


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    # InvalidHashError guards a malformed stored hash (bad DB write, Google-only
    # user) — treat as a failed login, never a 500 that leaks the difference.
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _user_view(row: asyncpg.Record) -> dict[str, Any]:
    """User dict for API/dependency use — never includes password_hash."""
    return {"id": str(row["id"]), "email": row["email"]}


async def signup(
    pool: asyncpg.Pool, *, email: str, password: str, workspace_name: str
) -> dict[str, Any]:
    """Create User + Workspace + admin Membership in one transaction. Raises
    DuplicateEmail if the email is taken."""
    email = email.strip().lower()
    user_id, workspace_id, membership_id = (uuid.uuid4() for _ in range(3))
    pw_hash = hash_password(password)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
                    user_id,
                    email,
                    pw_hash,
                )
                await conn.execute(
                    "INSERT INTO workspaces (id, name) VALUES ($1, $2)",
                    workspace_id,
                    workspace_name,
                )
                await conn.execute(
                    """INSERT INTO memberships (id, user_id, workspace_id, role)
                       VALUES ($1, $2, $3, 'admin')""",
                    membership_id,
                    user_id,
                    workspace_id,
                )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateEmail(email) from exc
    return {"id": str(user_id), "email": email, "workspace_id": str(workspace_id)}


async def upsert_google_user(
    pool: asyncpg.Pool, *, email: str, workspace_name: str
) -> dict[str, Any]:
    """Link-by-verified-email (#32): return the existing User with this email, or
    create one (User + Workspace + admin Membership, no password) — so one email is
    one account across password and Google. Callers must have verified the email."""
    email = email.strip().lower()
    row = await pool.fetchrow("SELECT id, email FROM users WHERE email = $1", email)
    if row is not None:
        return _user_view(row)
    user_id, workspace_id, membership_id = (uuid.uuid4() for _ in range(3))
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # password_hash left NULL — a Google-only user can't password-login
                # (login() rejects a null hash), only re-auth via Google.
                await conn.execute(
                    "INSERT INTO users (id, email) VALUES ($1, $2)", user_id, email
                )
                await conn.execute(
                    "INSERT INTO workspaces (id, name) VALUES ($1, $2)",
                    workspace_id,
                    workspace_name,
                )
                await conn.execute(
                    """INSERT INTO memberships (id, user_id, workspace_id, role)
                       VALUES ($1, $2, $3, 'admin')""",
                    membership_id,
                    user_id,
                    workspace_id,
                )
    except asyncpg.UniqueViolationError:
        # Race: a concurrent first-login created the user between our SELECT and
        # INSERT. Re-read and link to it rather than erroring.
        row = await pool.fetchrow("SELECT id, email FROM users WHERE email = $1", email)
        if row is None:
            # The winning INSERT rolled back after ours conflicted — surface the
            # violation rather than crashing _user_view(None) with a TypeError.
            raise
        return _user_view(row)
    return {"id": str(user_id), "email": email}


async def get_user_by_email(pool: asyncpg.Pool, email: str) -> dict[str, Any] | None:
    """Full row incl. password_hash — for login verification only, never returned
    to a client."""
    row = await pool.fetchrow(
        "SELECT id, email, password_hash FROM users WHERE email = $1",
        email.strip().lower(),
    )
    if row is None:
        return None
    return {"id": str(row["id"]), "email": row["email"], "password_hash": row["password_hash"]}


async def resolve_membership(
    pool: asyncpg.Pool, user_id: str, workspace_id: str
) -> str | None:
    """The caller's role in a Workspace, or None if they aren't a member. Looked
    up per request so a kicked/re-roled member's change takes effect immediately
    (ADR-0011). A malformed workspace_id resolves to None (→ 403), never a 500."""
    try:
        wid = uuid.UUID(workspace_id)
    except (ValueError, TypeError):
        return None
    return await pool.fetchval(
        "SELECT role FROM memberships WHERE user_id = $1 AND workspace_id = $2",
        uuid.UUID(user_id),
        wid,
    )


async def list_workspaces_for_user(
    pool: asyncpg.Pool, user_id: str
) -> list[dict[str, Any]]:
    """Every Workspace the user is a Member of, with their role in each (#33).
    This is the switcher's source of truth — a user only sees Workspaces they
    belong to."""
    rows = await pool.fetch(
        """SELECT w.id, w.name, m.role
             FROM memberships m
             JOIN workspaces w ON w.id = m.workspace_id
            WHERE m.user_id = $1
            ORDER BY w.name""",
        uuid.UUID(user_id),
    )
    return [{"id": str(r["id"]), "name": r["name"], "role": r["role"]} for r in rows]


async def create_workspace(
    pool: asyncpg.Pool, user_id: str, name: str
) -> dict[str, Any]:
    """Create a Workspace and make the caller its admin (#33). The admin
    Membership is what makes the new workspace_id pass the request gate."""
    workspace_id, membership_id = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO workspaces (id, name) VALUES ($1, $2)", workspace_id, name
            )
            await conn.execute(
                """INSERT INTO memberships (id, user_id, workspace_id, role)
                   VALUES ($1, $2, $3, 'admin')""",
                membership_id,
                uuid.UUID(user_id),
                workspace_id,
            )
    return {"id": str(workspace_id), "name": name, "role": "admin"}


async def create_invite(
    pool: asyncpg.Pool, *, workspace_id: str, email: str, role: str, invited_by: str
) -> dict[str, Any]:
    """Create a pending invite and return it (incl. the token — the invite link
    secret). Admin-gated at the router (#34)."""
    invite_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    email = email.strip().lower()
    await pool.execute(
        """INSERT INTO invites (id, workspace_id, email, role, token, invited_by)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        invite_id,
        uuid.UUID(workspace_id),
        email,
        role,
        token,
        uuid.UUID(invited_by),
    )
    return {
        "id": str(invite_id),
        "workspace_id": workspace_id,
        "email": email,
        "role": role,
        "token": token,
    }


async def accept_invite(
    pool: asyncpg.Pool, *, token: str, user_id: str, user_email: str
) -> dict[str, Any]:
    """Redeem an invite: create the Membership in the inviting Workspace and mark
    the invite used. Invite-only — the accepting user's email must be the invited
    one. Idempotent on an existing Membership (keeps the current role).

    Raises InviteNotFound (unknown/used) or InviteEmailMismatch (wrong user)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the row so two concurrent accepts can't both pass the pending
            # check; the second sees accepted_at set and raises InviteNotFound.
            # Expired invites read as not-found (same InviteNotFound → 404).
            invite = await conn.fetchrow(
                """SELECT workspace_id, email, role FROM invites
                    WHERE token = $1 AND accepted_at IS NULL AND expires_at > now()
                    FOR UPDATE""",
                token,
            )
            if invite is None:
                raise InviteNotFound(token)
            if invite["email"] != user_email.strip().lower():
                raise InviteEmailMismatch(user_email)
            # DO NOTHING (not DO UPDATE): an already-member keeps their current role,
            # so a re-invite can't silently change a role and bypass the last-admin
            # guard. RETURNING tells us whether a row was actually inserted.
            inserted_role = await conn.fetchval(
                """INSERT INTO memberships (id, user_id, workspace_id, role)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (user_id, workspace_id) DO NOTHING
                   RETURNING role""",
                uuid.uuid4(),
                uuid.UUID(user_id),
                invite["workspace_id"],
                invite["role"],
            )
            already_member = inserted_role is None
            if already_member:
                # Report their actual (unchanged) role, not the invite's.
                role = await conn.fetchval(
                    "SELECT role FROM memberships WHERE user_id = $1 AND workspace_id = $2",
                    uuid.UUID(user_id),
                    invite["workspace_id"],
                )
            else:
                role = inserted_role
            await conn.execute(
                "UPDATE invites SET accepted_at = now() WHERE token = $1", token
            )
    return {
        "workspace_id": str(invite["workspace_id"]),
        "role": role,
        "already_member": already_member,
    }


async def list_members(pool: asyncpg.Pool, workspace_id: str) -> list[dict[str, Any]]:
    """Members of a Workspace with their roles (#34). Admin-gated at the router."""
    rows = await pool.fetch(
        """SELECT u.id, u.email, m.role
             FROM memberships m
             JOIN users u ON u.id = m.user_id
            WHERE m.workspace_id = $1
            ORDER BY u.email""",
        uuid.UUID(workspace_id),
    )
    return [{"user_id": str(r["id"]), "email": r["email"], "role": r["role"]} for r in rows]


async def _other_admins_exist(conn: Any, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """True if the Workspace has an admin other than `user_id` — used to block the
    change that would leave a Workspace with no admin (an unmanageable lockout).

    Locks every admin row in the Workspace (FOR UPDATE) so two concurrent
    demotions/removals can't both see the other as admin and both proceed — the
    second blocks, then re-reads the post-commit state. `count(*) ... FOR UPDATE`
    is rejected by Postgres (aggregate), so lock the rows and count in Python."""
    rows = await conn.fetch(
        "SELECT user_id FROM memberships WHERE workspace_id = $1 AND role = 'admin' FOR UPDATE",
        workspace_id,
    )
    return any(r["user_id"] != user_id for r in rows)


async def change_member_role(
    pool: asyncpg.Pool, workspace_id: str, user_id: str, role: str
) -> bool:
    """Set a member's role. Returns False if they aren't a member. Raises LastAdmin
    if demoting the Workspace's only admin."""
    wid, uid = uuid.UUID(workspace_id), uuid.UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT role FROM memberships WHERE workspace_id = $1 AND user_id = $2",
                wid,
                uid,
            )
            if current is None:
                return False
            if current == "admin" and role != "admin" and not await _other_admins_exist(
                conn, wid, uid
            ):
                raise LastAdmin(user_id)
            await conn.execute(
                "UPDATE memberships SET role = $3 WHERE workspace_id = $1 AND user_id = $2",
                wid,
                uid,
                role,
            )
    return True


async def remove_member(pool: asyncpg.Pool, workspace_id: str, user_id: str) -> bool:
    """Remove a member. Returns False if they aren't a member. Raises LastAdmin if
    removing the Workspace's only admin."""
    wid, uid = uuid.UUID(workspace_id), uuid.UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT role FROM memberships WHERE workspace_id = $1 AND user_id = $2",
                wid,
                uid,
            )
            if current is None:
                return False
            if current == "admin" and not await _other_admins_exist(conn, wid, uid):
                raise LastAdmin(user_id)
            await conn.execute(
                "DELETE FROM memberships WHERE workspace_id = $1 AND user_id = $2",
                wid,
                uid,
            )
    return True


async def create_login_session(pool: asyncpg.Pool, user_id: str) -> str:
    """Insert a session row and return its opaque token."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    await pool.execute(
        "INSERT INTO user_sessions (token, user_id, expires_at) VALUES ($1, $2, $3)",
        token,
        uuid.UUID(user_id),
        expires_at,
    )
    return token


async def resolve_session(pool: asyncpg.Pool, token: str) -> dict[str, Any] | None:
    """The User behind a session token, or None if unknown/expired."""
    row = await pool.fetchrow(
        """SELECT u.id, u.email
             FROM user_sessions s
             JOIN users u ON u.id = s.user_id
            WHERE s.token = $1 AND s.expires_at > now()""",
        token,
    )
    return _user_view(row) if row else None


async def delete_session(pool: asyncpg.Pool, token: str) -> None:
    await pool.execute("DELETE FROM user_sessions WHERE token = $1", token)
