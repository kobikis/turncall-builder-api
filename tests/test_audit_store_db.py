"""Real-DB test for the audit log of people (ADR-0028, migration 0020).

Exercises the SQL the endpoint tests mock: entries are read back newest first,
Workspace-scoped, filtered by member, agent and time and paged with a total;
the table refuses UPDATE, DELETE and TRUNCATE to everything but the retention
janitor, which removes only what is past retention; an entry written in a
change's transaction (db.transaction) commits and rolls back with it; and the
migration goes down and up again.

Needs a reachable Postgres (same contract as the other *_db tests). Skips cleanly
when none is set.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app import audit_store, auth_store, db, workspace_skills
from tests.conftest import __file__ as _conftest_file

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_audittest"


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


def _entry(workspace_id: str, user_id: str, **over) -> dict:
    entry = {
        "id": str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "user_id": user_id,
        "user_email": "alice@x.com",
        "role": "admin",
        "resource_type": "agent",
        "resource_id": "a1",
        "resource_name": "Front desk",
        "action": "updated",
        "agent_ids": ["a1"],
        "changes": {"/system_prompt": {"before": "a", "after": "b"}},
        "request_id": str(uuid.uuid4()),
    }
    entry.update(over)
    return entry


async def _backdate(pool, entry_id: str, days: int) -> None:
    """Write an entry as if it were `days` old: an INSERT, which the table allows."""
    row = await pool.fetchrow("SELECT * FROM audit_entries WHERE id = $1", uuid.UUID(entry_id))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(f"SET LOCAL {audit_store.RETENTION_GUC} = 'on'")
        await conn.execute("DELETE FROM audit_entries WHERE id = $1", row["id"])
    cols = list(row.keys())
    values = [row[c] for c in cols]
    values[cols.index("created_at")] = datetime.now(timezone.utc) - timedelta(days=days)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    await pool.execute(f"INSERT INTO audit_entries ({', '.join(cols)}) VALUES ({placeholders})", *values)


async def _run_assertions(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)
    try:
        alice = await auth_store.signup(pool, email="alice@x.com", password="password123", workspace_name="A")
        bob = await auth_store.signup(pool, email="bob@x.com", password="password123", workspace_name="B")
        ws, other_ws = alice["workspace_id"], bob["workspace_id"]

        # --- append and read back ------------------------------------------------
        first = await audit_store.insert(pool, _entry(ws, alice["id"]))
        assert first["changes"] == {"/system_prompt": {"before": "a", "after": "b"}}
        assert first["agent_ids"] == ["a1"]
        assert first["turncall_project_id"] is None and first["engine_audit_id"] is None
        second = await audit_store.insert(
            pool, _entry(ws, bob["id"], role="editor", resource_type="phone_number",
                         resource_id="pn1", agent_ids=["a1", "a2"], action="updated"),
        )
        await audit_store.insert(pool, _entry(ws, alice["id"], resource_type="skill", resource_id="s", agent_ids=[]))
        await audit_store.insert(pool, _entry(other_ws, bob["id"]))  # another Workspace's

        entries, total = await audit_store.list_entries(pool, ws)
        assert total == 3
        # Newest first.
        assert [e["resource_type"] for e in entries] == ["skill", "phone_number", "agent"]
        assert {e["workspace_id"] for e in entries} == {ws}

        by_bob, total = await audit_store.list_entries(pool, ws, user_id=bob["id"])
        assert total == 1 and by_bob[0]["id"] == second["id"] and by_bob[0]["role"] == "editor"
        assert (await audit_store.list_entries(pool, ws, user_id="not-a-uuid")) == ([], 0)

        about_a2, total = await audit_store.list_entries(pool, ws, agent_id="a2")
        assert [e["id"] for e in about_a2] == [second["id"]]
        about_a1, total = await audit_store.list_entries(pool, ws, agent_id="a1")
        assert total == 2

        page, total = await audit_store.list_entries(pool, ws, page=2, limit=2)
        assert total == 3 and [e["resource_type"] for e in page] == ["agent"]

        created = datetime.fromisoformat(first["created_at"])
        assert (await audit_store.list_entries(pool, ws, until=created))[1] == 0
        assert (await audit_store.list_entries(pool, ws, since=created + timedelta(hours=1)))[1] == 0
        assert (await audit_store.list_entries(pool, ws, since=created))[1] == 3

        # --- append-only ---------------------------------------------------------
        for sql in (
            "UPDATE audit_entries SET action = 'tampered'",
            "DELETE FROM audit_entries",
            "TRUNCATE audit_entries",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                await pool.execute(sql)
        assert (await audit_store.list_entries(pool, ws))[1] == 3

        # --- retention -------------------------------------------------------------
        await _backdate(pool, first["id"], days=40)
        assert await audit_store.purge_older_than(pool, 30, batch=1) == 1
        remaining, total = await audit_store.list_entries(pool, ws)
        assert total == 2 and first["id"] not in {e["id"] for e in remaining}
        assert await audit_store.purge_older_than(pool, 30) == 0
        # The janitor's mark is the transaction's own: once it ends, refused again.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.execute("DELETE FROM audit_entries")

        # --- the change's own transaction -----------------------------------------
        skill_entry = _entry(ws, alice["id"], resource_type="skill", resource_id="orders", agent_ids=[])
        with pytest.raises(RuntimeError):
            async with db.transaction(pool) as conn:
                await workspace_skills.save_skill(conn, ws, name="orders", description="d", body="b")
                await audit_store.insert(conn, skill_entry)
                raise RuntimeError("the change fails after its entry was written")
        assert await workspace_skills.get_skill(pool, ws, "orders") is None
        assert (await audit_store.list_entries(pool, ws, user_id=alice["id"]))[1] == 1

        async with db.transaction(pool) as conn:
            await workspace_skills.save_skill(conn, ws, name="orders", description="d", body="b")
            await audit_store.insert(conn, {**skill_entry, "id": str(uuid.uuid4())})
        assert (await workspace_skills.get_skill(pool, ws, "orders"))["body"] == "b"
        assert (await audit_store.list_entries(pool, ws, user_id=alice["id"]))[1] == 2

        # A store's own transaction nests inside (a savepoint): its refusal rolls
        # back only itself, and the caller decides about the rest.
        async with db.transaction(pool) as conn:
            with pytest.raises(auth_store.LastAdmin):
                await auth_store.remove_member(conn, ws, alice["id"])
        assert await auth_store.resolve_membership(pool, alice["id"], ws) == "admin"
    finally:
        await pool.close()


def test_audit_entries_against_postgres():
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
        # The migration goes down cleanly (table, triggers and function) and up again.
        command.downgrade(cfg, "0019")
        command.upgrade(cfg, "head")
        asyncio.run(_run_assertions(test_dsn))
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        asyncio.run(_admin(base_url, f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)'))
