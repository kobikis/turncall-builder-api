"""Real-DB test for team management (#34): invite → accept → manage.

Exercises the actual SQL the seam test mocks: an invite becomes a Membership only
for the invited email, acceptance is single-use, roles change and members are
removed, the last admin is protected, and a non-invited user can neither see nor
act in the Workspace.

Needs a reachable Postgres (same contract as the other *_db tests). Skips cleanly
when none is set.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app import auth_store
from tests.conftest import __file__ as _conftest_file

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_memberstest"


def _base_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return url.replace("+asyncpg", "") if url else None


async def _server_reachable(base_url: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn=base_url, database="postgres")
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


async def _recreate_test_db(base_url: str) -> None:
    conn = await asyncpg.connect(dsn=base_url, database="postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    finally:
        await conn.close()


async def _drop_test_db(base_url: str) -> None:
    conn = await asyncpg.connect(dsn=base_url, database="postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
    finally:
        await conn.close()


async def _run_assertions(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        alice = await auth_store.signup(
            pool, email="alice@x.com", password="password123", workspace_name="Alice WS"
        )
        bob = await auth_store.signup(
            pool, email="bob@x.com", password="password123", workspace_name="Bob WS"
        )
        ws = alice["workspace_id"]

        # Bob isn't a member of Alice's Workspace yet — can't act there, and an
        # admin of another Workspace can't manage him here (workspace scoping):
        # both ops report False (not-a-member) rather than reaching across.
        assert await auth_store.resolve_membership(pool, bob["id"], ws) is None
        assert await auth_store.change_member_role(pool, ws, bob["id"], "viewer") is False
        assert await auth_store.remove_member(pool, ws, bob["id"]) is False

        # Alice invites Bob as editor.
        invite = await auth_store.create_invite(
            pool, workspace_id=ws, email="Bob@x.com", role="editor", invited_by=alice["id"]
        )

        # Wrong email can't redeem it (invite-only), and an unknown token 404s.
        with pytest.raises(auth_store.InviteEmailMismatch):
            await auth_store.accept_invite(
                pool, token=invite["token"], user_id=alice["id"], user_email="alice@x.com"
            )
        with pytest.raises(auth_store.InviteNotFound):
            await auth_store.accept_invite(
                pool, token="bogus", user_id=bob["id"], user_email="bob@x.com"
            )

        # Bob accepts → Membership in Alice's Workspace with the invited role.
        result = await auth_store.accept_invite(
            pool, token=invite["token"], user_id=bob["id"], user_email="bob@x.com"
        )
        assert result == {"workspace_id": ws, "role": "editor", "already_member": False}
        assert await auth_store.resolve_membership(pool, bob["id"], ws) == "editor"

        # Single-use: a second accept of the same token 404s.
        with pytest.raises(auth_store.InviteNotFound):
            await auth_store.accept_invite(
                pool, token=invite["token"], user_id=bob["id"], user_email="bob@x.com"
            )

        # List shows both, each with their role.
        members = await auth_store.list_members(pool, ws)
        assert {m["email"]: m["role"] for m in members} == {
            "alice@x.com": "admin", "bob@x.com": "editor"
        }

        # Role change takes effect on the next membership resolve.
        assert await auth_store.change_member_role(pool, ws, bob["id"], "viewer") is True
        assert await auth_store.resolve_membership(pool, bob["id"], ws) == "viewer"

        # Re-inviting an existing member consumes the invite but does NOT silently
        # change their role — so a re-invite can't escalate a viewer to admin (which
        # would also dodge the last-admin guard). already_member reports the no-op.
        reinvite = await auth_store.create_invite(
            pool, workspace_id=ws, email="bob@x.com", role="admin", invited_by=alice["id"]
        )
        r2 = await auth_store.accept_invite(
            pool, token=reinvite["token"], user_id=bob["id"], user_email="bob@x.com"
        )
        assert r2 == {"workspace_id": ws, "role": "viewer", "already_member": True}
        assert await auth_store.resolve_membership(pool, bob["id"], ws) == "viewer"

        # Last-admin guard: Alice is the only admin — she can't be demoted or removed.
        with pytest.raises(auth_store.LastAdmin):
            await auth_store.change_member_role(pool, ws, alice["id"], "viewer")
        with pytest.raises(auth_store.LastAdmin):
            await auth_store.remove_member(pool, ws, alice["id"])

        # Remove Bob → gone from the Workspace, can no longer act.
        assert await auth_store.remove_member(pool, ws, bob["id"]) is True
        assert await auth_store.resolve_membership(pool, bob["id"], ws) is None
        assert {m["email"] for m in await auth_store.list_members(pool, ws)} == {"alice@x.com"}

        # Removing a non-member reports False (not an error).
        assert await auth_store.remove_member(pool, ws, bob["id"]) is False
    finally:
        await pool.close()


def test_invite_accept_and_manage():
    base_url = _base_url()
    if not base_url or not asyncio.run(_server_reachable(base_url)):
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL to run)")

    test_dsn = base_url.rsplit("/", 1)[0] + f"/{_TEST_DB}"
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_dsn
    try:
        asyncio.run(_recreate_test_db(base_url))
        command.upgrade(cfg, "head")
        asyncio.run(_run_assertions(test_dsn))
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        asyncio.run(_drop_test_db(base_url))
