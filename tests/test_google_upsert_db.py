"""Real-DB test for auth_store.upsert_google_user (#32): link-by-verified-email.

Where the seam test (test_google_login.py) mocks the store, this exercises the
actual SQL against a throwaway Postgres upgraded to head — proving the three
branches that matter: create-new, link-existing, and the seeded admin claiming
the Default Workspace on first Google sign-in.

Needs a reachable Postgres (same contract as test_migration_default_workspace).
Skips cleanly when none is set.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app import auth_store
from tests.conftest import __file__ as _conftest_file

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_googletest"
_DEFAULT_WORKSPACE_ID = "d0000000-0000-0000-0000-000000000001"
# Must match the admin seeded by migration 0003 (same constant there). If the seed
# changes, the "seeded admin claims Default" assertion silently tests create-new
# instead — keep these in sync.
_ADMIN_EMAIL = "admin@example.test"


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
        # --- create-new: unknown email mints User + Workspace + admin membership ---
        before = await pool.fetchval("SELECT count(*) FROM workspaces")
        created = await auth_store.upsert_google_user(
            pool, email="Fresh@Example.com", workspace_name="Fresh's Workspace"
        )
        assert created["email"] == "fresh@example.com"  # normalised
        after = await pool.fetchval("SELECT count(*) FROM workspaces")
        assert after == before + 1, "create-new should add exactly one Workspace"
        row = await pool.fetchrow(
            "SELECT password_hash FROM users WHERE id = $1", uuid.UUID(created["id"])
        )
        assert row["password_hash"] is None, "Google user has no password"
        role = await pool.fetchval(
            "SELECT role FROM memberships WHERE user_id = $1", uuid.UUID(created["id"])
        )
        assert role == "admin"

        # --- link-existing: same email returns same user, no new Workspace ---
        before = await pool.fetchval("SELECT count(*) FROM workspaces")
        again = await auth_store.upsert_google_user(
            pool, email="fresh@example.com", workspace_name="ignored"
        )
        assert again["id"] == created["id"], "second sign-in must link, not duplicate"
        after = await pool.fetchval("SELECT count(*) FROM workspaces")
        assert after == before, "link-existing must not create a Workspace"

        # --- seeded admin (from 0003) claims Default on first Google sign-in ---
        admin = await auth_store.upsert_google_user(
            pool, email=_ADMIN_EMAIL, workspace_name="ignored"
        )
        default_role = await pool.fetchval(
            "SELECT role FROM memberships WHERE user_id = $1 AND workspace_id = $2",
            uuid.UUID(admin["id"]),
            uuid.UUID(_DEFAULT_WORKSPACE_ID),
        )
        assert default_role == "admin", "admin must reach the Default Workspace"
    finally:
        await pool.close()


def test_upsert_google_user_link_and_create():
    base_url = _base_url()
    if not base_url or not asyncio.run(_server_reachable(base_url)):
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL to run)")

    test_dsn = base_url.rsplit("/", 1)[0] + f"/{_TEST_DB}"
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    prev = os.environ.get("DATABASE_URL")
    prev_admin = os.environ.get("INITIAL_ADMIN_EMAIL")
    os.environ["DATABASE_URL"] = test_dsn
    # 0003 only seeds the admin + Default membership when this is set, and the
    # assertions below require that seed to exist.
    os.environ["INITIAL_ADMIN_EMAIL"] = _ADMIN_EMAIL
    try:
        asyncio.run(_recreate_test_db(base_url))
        command.upgrade(cfg, "head")  # full schema + seeded admin/Default (0003)
        asyncio.run(_run_assertions(test_dsn))
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
