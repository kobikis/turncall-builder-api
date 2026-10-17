"""Compliance settings against a real Postgres (#103).

The seam tests mock the store, so this is what proves its SQL agrees with
migration 0019: settings round-trip as TurnCall's shape, an all-null override is
no row at all, projections upsert per target, and the migration comes down and
goes back up clean.

Same contract as the other *_db tests: skips cleanly with no reachable Postgres.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app import compliance_store
from tests.conftest import __file__ as _conftest_file

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_compliancetest"
_TABLES = ("workspace_compliance", "agent_compliance", "compliance_projections")


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
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        ws = await pool.fetchval(
            "INSERT INTO workspaces (id, name) VALUES (gen_random_uuid(), 't') RETURNING id::text"
        )
        assert await compliance_store.get_workspace(pool, ws) is None

        redaction = {"enabled": True, "entities": ["PHONE_NUMBER"]}
        retention = {"recordings_days": 30, "transcripts_days": None, "events_days": 365}
        saved = await compliance_store.save_workspace(pool, ws, redaction=redaction, retention=retention)
        assert (saved["redaction"], saved["retention"]) == (redaction, retention)
        again = await compliance_store.save_workspace(
            pool, ws, redaction={"enabled": False, "entities": None}, retention=retention
        )
        assert again["redaction"] == {"enabled": False, "entities": None}
        assert await pool.fetchval("SELECT count(*) FROM workspace_compliance") == 1

        override = {"recordings_days": 7, "transcripts_days": None, "events_days": None}
        await compliance_store.save_override(pool, ws, "a1", redaction=None, retention=override)
        assert await compliance_store.get_override(pool, ws, "a1") == {"redaction": None, "retention": override}
        assert await compliance_store.list_overrides(pool, ws) == {
            "a1": {"redaction": None, "retention": override}
        }
        other = await pool.fetchval(
            "INSERT INTO workspaces (id, name) VALUES (gen_random_uuid(), 'o') RETURNING id::text"
        )
        assert await compliance_store.get_override(pool, other, "a1") is None
        await compliance_store.save_override(pool, ws, "a1", redaction=None, retention=None)
        assert await compliance_store.list_overrides(pool, ws) == {}

        await compliance_store.record_projection(pool, ws, "a1", state="applied", fingerprint="f1")
        await compliance_store.record_projection(pool, ws, "a1", state="failed", fingerprint="f2", error="x")
        [(agent, p)] = (await compliance_store.list_projections(pool, ws)).items()
        assert (agent, p["state"], p["fingerprint"], p["error"]) == ("a1", "failed", "f2", "x")
        assert await compliance_store.list_projections(pool, other) == {}
        one = await compliance_store.get_projection(pool, ws, "a1")
        assert (one["state"], one["fingerprint"], one["error"]) == ("failed", "f2", "x")
        assert await compliance_store.get_projection(pool, ws, "a2") is None
        assert await compliance_store.get_projection(pool, other, "a1") is None

        # A deleted Agent's override and projection go; another Agent's stay.
        await compliance_store.save_override(pool, ws, "a1", redaction=None, retention=override)
        await compliance_store.save_override(pool, ws, "a2", redaction=None, retention=override)
        await compliance_store.record_projection(pool, ws, "a2", state="applied", fingerprint="f3")
        await compliance_store.forget_agent(pool, ws, "a1")
        assert await compliance_store.get_override(pool, ws, "a1") is None
        assert await compliance_store.get_projection(pool, ws, "a1") is None
        assert list(await compliance_store.list_overrides(pool, ws)) == ["a2"]
        assert list(await compliance_store.list_projections(pool, ws)) == ["a2"]

        # A Workspace's removal takes all of it along.
        await pool.execute("DELETE FROM workspaces WHERE id = $1", ws)
        for table in _TABLES:
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    finally:
        await pool.close()


def test_compliance_store_round_trip_and_migration_down_up():
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
        command.downgrade(cfg, "0018")
        assert asyncio.run(_tables(test_dsn)) == set()
        command.upgrade(cfg, "head")
        assert asyncio.run(_tables(test_dsn)) == set(_TABLES)
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        asyncio.run(_admin(base_url, f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)'))
