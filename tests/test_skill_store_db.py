"""The Skill library at rest, against a real Postgres (#131).

The seam tests mock the store, so this is what proves its SQL: the key is
sealed, two first creates racing make one library (one TurnCall project), and
the migration comes down and goes back up clean.

Same contract as the other *_db tests: skips cleanly with no reachable Postgres.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app import skill_store
from tests.conftest import __file__ as _conftest_file

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_skilltest"
_TABLES = ("skill_libraries", "skill_links")


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


async def _admin(base_url: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn=base_url, database="postgres")
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _tables(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ANY($1::text[])",
            list(_TABLES),
        )
    finally:
        await conn.close()
    return {r["table_name"] for r in rows}


async def _run_assertions(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    try:
        ws = await pool.fetchval(
            "INSERT INTO workspaces (id, name) VALUES (gen_random_uuid(), 't') RETURNING id::text"
        )
        assert await skill_store.get(pool, ws) is None

        made: list[str] = []

        async def create() -> tuple[str, str]:
            await asyncio.sleep(0.05)  # long enough for the other create to arrive
            made.append(f"proj-{len(made) + 1}")
            return made[-1], "tc_secret_key"

        first, second = await asyncio.gather(
            skill_store.ensure(pool, ws, create),
            skill_store.ensure(pool, ws, create),
        )
        # One TurnCall project, one row, and both callers handed it.
        assert made == ["proj-1"]
        assert sorted([first[1], second[1]]) == [False, True]
        assert first[0]["project_id"] == second[0]["project_id"] == "proj-1"
        assert await pool.fetchval("SELECT count(*) FROM skill_libraries") == 1

        library = await skill_store.get(pool, ws)
        assert (library["project_id"], library["api_key"]) == ("proj-1", "tc_secret_key")
        sealed = await pool.fetchval("SELECT api_key_encrypted FROM skill_libraries")
        assert "tc_secret_key" not in sealed

        assert await skill_store.library_project(pool, ws) == "proj-1"

        # One record per project, rewritten in place.
        await skill_store.record_link(
            pool, ws, "p1", library_project_id="proj-1", state="failed", error="down"
        )
        await skill_store.record_link(pool, ws, "p1", library_project_id="proj-1", state="linked")
        links = await skill_store.list_links(pool, ws)
        assert list(links) == ["p1"]
        assert (links["p1"]["state"], links["p1"]["error"]) == ("linked", None)

        # A Workspace's removal takes its library and links along.
        await pool.execute("DELETE FROM workspaces WHERE id = $1", ws)
        for table in _TABLES:
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    finally:
        await pool.close()


def test_skill_store_round_trip_and_migration_down_up():
    base_url = _base_url()
    if not base_url or not asyncio.run(_server_reachable(base_url)):
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL to run)")

    test_dsn = base_url.rsplit("/", 1)[0] + f"/{_TEST_DB}"
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_dsn
    try:
        asyncio.run(_admin(base_url, f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)'))
        asyncio.run(_admin(base_url, f'CREATE DATABASE "{_TEST_DB}"'))
        command.upgrade(cfg, "head")
        asyncio.run(_run_assertions(test_dsn))
        command.downgrade(cfg, "0023")
        assert asyncio.run(_tables(test_dsn)) == set()
        command.upgrade(cfg, "head")
        assert asyncio.run(_tables(test_dsn)) == set(_TABLES)
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        asyncio.run(_admin(base_url, f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)'))
