"""Real-DB test for revision 0003 (Default Workspace backfill + admin seed).

Runs the actual Alembic upgrade against a throwaway Postgres database: upgrade to
0002, insert legacy rows with no workspace_id, upgrade to 0003, then assert every
row was adopted into Default, the column is now NOT NULL, and the admin was seeded.

Needs a reachable Postgres. Set TEST_DATABASE_URL (or DATABASE_URL) to a server
where the user can CREATE/DROP DATABASE; the test creates `builder_migtest_0003`
and drops it. Skips cleanly when no server is reachable (e.g. plain unit runs).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.conftest import __file__ as _conftest_file  # anchor to the repo

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_migtest_0003"
_DEFAULT_WORKSPACE_ID = "d0000000-0000-0000-0000-000000000001"
_ADMIN_EMAIL = "admin@example.test"  # seeded via INITIAL_ADMIN_EMAIL below


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


async def _seed_legacy_rows(dsn: str) -> None:
    """Rows written by the pre-migration builder — no workspace_id column value."""
    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Two sessions so the backfill assertion genuinely covers multiple rows.
        await conn.execute("INSERT INTO sessions (id) VALUES ($1)", uuid.uuid4())
        await conn.execute("INSERT INTO sessions (id) VALUES ($1)", uuid.uuid4())
        await conn.execute(
            "INSERT INTO agent_backends (agent_id, slug, port, service_dir) "
            "VALUES ('a1', 'agent', 9999, '/srv/agent')"
        )
        await conn.execute(
            "INSERT INTO phone_numbers (id, e164, sid, routing_type) "
            "VALUES ('pn1', '+15550001', 'SM1', 'agent')"
        )
    finally:
        await conn.close()


async def _assertions(dsn: str) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Backfill: no legacy row left unset, and every row points at Default.
        for table in ("sessions", "agent_backends", "phone_numbers"):
            wrong = await conn.fetchval(
                f"SELECT count(*) FROM {table} "
                "WHERE workspace_id IS NULL OR workspace_id::text != $1",
                _DEFAULT_WORKSPACE_ID,
            )
            assert wrong == 0, f"{table} not fully backfilled to Default"

        # NOT NULL applied to all three columns.
        nullable = await conn.fetch(
            "SELECT table_name, is_nullable FROM information_schema.columns "
            "WHERE column_name = 'workspace_id' AND table_schema = 'public' "
            "AND table_name IN ('sessions', 'agent_backends', 'phone_numbers')"
        )
        assert len(nullable) == 3
        assert all(r["is_nullable"] == "NO" for r in nullable), "workspace_id still nullable"

        # Admin seed: user with no password + admin membership in Default.
        user = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1", _ADMIN_EMAIL
        )
        assert user is not None, "admin user not seeded"
        assert user["password_hash"] is None, "seeded admin should have no password"
        role = await conn.fetchval(
            "SELECT role FROM memberships WHERE user_id = $1 AND workspace_id = $2",
            user["id"],
            uuid.UUID(_DEFAULT_WORKSPACE_ID),
        )
        assert role == "admin", "admin membership missing in Default"
    finally:
        await conn.close()



async def _assert_no_admin_seeded(dsn: str) -> None:
    """Default self-hosted path: Default exists, but nobody owns it yet."""
    conn = await asyncpg.connect(dsn)
    try:
        ws = await conn.fetchval(
            "SELECT name FROM workspaces WHERE id = $1",
            uuid.UUID(_DEFAULT_WORKSPACE_ID),
        )
        assert ws == "Default", "Default workspace should still be created"
        users = await conn.fetchval("SELECT count(*) FROM users")
        assert users == 0, f"no admin should be seeded, found {users} user(s)"
        members = await conn.fetchval(
            "SELECT count(*) FROM memberships WHERE workspace_id = $1",
            uuid.UUID(_DEFAULT_WORKSPACE_ID),
        )
        assert members == 0, "Default should be ownerless until first login"
    finally:
        await conn.close()


def test_upgrade_backfills_and_seeds_admin():
    base_url = _base_url()
    if not base_url or not asyncio.run(_server_reachable(base_url)):
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL to run)")

    test_dsn = base_url.rsplit("/", 1)[0] + f"/{_TEST_DB}"
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    prev = os.environ.get("DATABASE_URL")
    prev_admin = os.environ.get("INITIAL_ADMIN_EMAIL")
    os.environ["DATABASE_URL"] = test_dsn
    # 0003 only seeds an admin when this is set; the assertions below expect one.
    os.environ["INITIAL_ADMIN_EMAIL"] = _ADMIN_EMAIL
    try:
        asyncio.run(_recreate_test_db(base_url))
        command.upgrade(cfg, "0002")  # base + auth tables, no workspace_id yet
        asyncio.run(_seed_legacy_rows(test_dsn))
        command.upgrade(cfg, "0003")  # add column, seed, backfill, NOT NULL
        asyncio.run(_assertions(test_dsn))
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        if prev_admin is None:
            os.environ.pop("INITIAL_ADMIN_EMAIL", None)
        else:
            os.environ["INITIAL_ADMIN_EMAIL"] = prev_admin
        asyncio.run(_drop_test_db(base_url))


def test_upgrade_without_initial_admin_email_seeds_no_admin():
    """INITIAL_ADMIN_EMAIL unset is the default for a fresh self-hosted install.

    The backfill must still run; only the admin seed is skipped, leaving Default
    ownerless for the first authenticated user to claim.
    """
    base_url = _base_url()
    if not base_url or not asyncio.run(_server_reachable(base_url)):
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL to run)")

    test_dsn = base_url.rsplit("/", 1)[0] + f"/{_TEST_DB}"
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    prev = os.environ.get("DATABASE_URL")
    prev_admin = os.environ.get("INITIAL_ADMIN_EMAIL")
    os.environ["DATABASE_URL"] = test_dsn
    os.environ.pop("INITIAL_ADMIN_EMAIL", None)
    try:
        asyncio.run(_recreate_test_db(base_url))
        command.upgrade(cfg, "0002")
        asyncio.run(_seed_legacy_rows(test_dsn))
        command.upgrade(cfg, "0003")
        asyncio.run(_assert_no_admin_seeded(test_dsn))
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        if prev_admin is not None:
            os.environ["INITIAL_ADMIN_EMAIL"] = prev_admin
        asyncio.run(_drop_test_db(base_url))
