"""Real-DB test for the multi-workspace store (#33): list + create + isolation.

Exercises the actual SQL against a throwaway Postgres upgraded to head, proving
what the seam test mocks: create grants admin and makes the workspace show up in
the caller's list; resolve_membership then accepts that workspace_id (the gate
acceptance); and a second user sees none of the first's Workspaces.

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
_TEST_DB = "builder_wstest"


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
        # signup makes a User + their first Workspace (admin) — the starting point.
        alice = await auth_store.signup(
            pool, email="alice@x.com", password="password123", workspace_name="Alice WS"
        )
        bob = await auth_store.signup(
            pool, email="bob@x.com", password="password123", workspace_name="Bob WS"
        )

        # create: a second Workspace for Alice, she becomes its admin.
        created = await auth_store.create_workspace(pool, alice["id"], "Acme")
        assert created["role"] == "admin"

        # list: Alice now sees both her Workspaces, each with her role.
        alice_ws = await auth_store.list_workspaces_for_user(pool, alice["id"])
        by_name = {w["name"]: w for w in alice_ws}
        assert set(by_name) == {"Alice WS", "Acme"}
        assert by_name["Acme"]["role"] == "admin"
        assert by_name["Acme"]["id"] == created["id"]

        # gate acceptance: the new workspace_id resolves to admin for Alice...
        role = await auth_store.resolve_membership(pool, alice["id"], created["id"])
        assert role == "admin", "creator must pass the gate for the new Workspace"

        # ...but Bob (no Membership) can neither see it nor act in it.
        bob_ws = await auth_store.list_workspaces_for_user(pool, bob["id"])
        assert created["id"] not in {w["id"] for w in bob_ws}
        assert await auth_store.resolve_membership(pool, bob["id"], created["id"]) is None
    finally:
        await pool.close()


def test_workspace_list_create_and_isolation():
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
