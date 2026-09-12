"""The Pipedream credential against a real Postgres (ADR-0018, #24).

The seam tests mock the store, so nothing else proves the SQL agrees with
migration 0009 — a renamed column would ship green. This runs the migration and
the two statements that matter: the secret lands as ciphertext, it reads back as
plaintext, and re-saving replaces rather than accumulating (the row is a
singleton by CHECK, so a second insert would otherwise fail outright).

Same contract as the other *_db tests: skips cleanly with no reachable Postgres.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

os.environ.setdefault("PIPEDREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app import pipedream_store  # noqa: E402
from tests.conftest import __file__ as _conftest_file  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_pdtest"
_SECRET = "pd_secret_value"


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
        assert await pipedream_store.get_credential(pool) is None  # unconfigured

        await pipedream_store.save_credential(
            pool,
            client_id="cid",
            client_secret=_SECRET,
            project_id="proj",
            environment="development",
        )
        cred = await pipedream_store.get_credential(pool)
        assert cred.client_id == "cid"
        assert cred.project_id == "proj"
        assert cred.client_secret == _SECRET

        # A database dump alone must not yield the secret.
        stored = await pool.fetchval(
            "SELECT client_secret_encrypted FROM pipedream_credentials"
        )
        assert _SECRET not in stored

        # Restarting with a new secret replaces the one row.
        await pipedream_store.save_credential(
            pool,
            client_id="cid2",
            client_secret="rotated",
            project_id="proj",
            environment="production",
        )
        assert await pool.fetchval("SELECT count(*) FROM pipedream_credentials") == 1
        cred = await pipedream_store.get_credential(pool)
        assert (cred.client_id, cred.client_secret, cred.environment) == (
            "cid2",
            "rotated",
            "production",
        )
    finally:
        await pool.close()


def test_credential_is_stored_encrypted_and_replaced_in_place():
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
