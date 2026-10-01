"""Integrations and the MCP registry against a real Postgres (ADR-0023, #55/#59).

The seam tests mock both stores, so this is what proves the SQL agrees with
migration 0012: values land as ciphertext, read back as plaintext, a replace
bumps the version, and removing the record takes its projection log with it.

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

os.environ.setdefault("INTEGRATIONS_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("PIPEDREAM_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app import integration_store, mcp_registry_store  # noqa: E402
from tests.conftest import __file__ as _conftest_file  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(_conftest_file)))
_TEST_DB = "builder_integrationtest"
_SECRET = "dg_secret_value_9876"


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
        ws = await pool.fetchval("INSERT INTO workspaces (id, name) VALUES (gen_random_uuid(), 't') RETURNING id::text")
        saved = await integration_store.save(
            pool, ws, "deepgram",
            document={"api_key": _SECRET}, last4="9876", validation={"status": "valid"},
        )
        assert saved["last4"] == "9876" and saved["validation"] == {"status": "valid"}
        assert _SECRET not in await pool.fetchval("SELECT secret_encrypted FROM provider_integrations")
        assert await integration_store.get_document(pool, ws, "deepgram") == {"api_key": _SECRET}
        assert await integration_store.api_keys(pool, ws) == {"deepgram": _SECRET}

        again = await integration_store.save(
            pool, ws, "deepgram", document={"api_key": "rotated"}, last4="ated", validation={},
        )
        assert again["version"] != saved["version"]
        assert await pool.fetchval("SELECT count(*) FROM provider_integrations") == 1

        await integration_store.record_projection(
            pool, ws, "deepgram", "a1", state="applied", version=again["version"]
        )
        await integration_store.record_projection(
            pool, ws, "deepgram", "a1", state="failed", version=again["version"], error="x"
        )
        [p] = await integration_store.list_projections(pool, ws, "deepgram")
        assert (p["state"], p["error"], p["version"]) == ("failed", "x", again["version"])

        await integration_store.set_disconnecting(pool, ws, "deepgram")
        assert (await integration_store.get(pool, ws, "deepgram"))["disconnecting"] is True
        assert await integration_store.api_keys(pool, ws) == {}
        await integration_store.delete(pool, ws, "deepgram")
        assert await integration_store.get(pool, ws, "deepgram") is None
        assert await integration_store.list_projections(pool, ws) == []

        server = await mcp_registry_store.create_server(
            pool, ws, name="crm", transport="http", url="https://m.test",
            headers={"Authorization": "Bearer tok"},
        )
        assert server["header_names"] == ["Authorization"]
        assert "tok" not in str(server)
        assert "tok" not in await pool.fetchval("SELECT headers_encrypted FROM workspace_mcp_servers")
        edited = await mcp_registry_store.update_server(
            pool, ws, server["id"], name="crm", transport="sse", url="https://m.test", headers=None,
        )
        assert edited["transport"] == "sse" and edited["header_names"] == ["Authorization"]
        assert await mcp_registry_store.resolve(pool, ws, [server["id"], "gone"]) == [
            {"name": "crm", "transport": "sse", "url": "https://m.test",
             "headers": {"Authorization": "Bearer tok"}}
        ]
        other = await pool.fetchval("INSERT INTO workspaces (id, name) VALUES (gen_random_uuid(), 'o') RETURNING id::text")
        assert await mcp_registry_store.resolve(pool, other, [server["id"]]) == []
        assert await mcp_registry_store.delete_server(pool, ws, server["id"]) is True
        assert await mcp_registry_store.list_servers(pool, ws) == []

        # A number's own Twilio account: sealed in the mirror, kept across an
        # edit that does not re-enter it, never in a read.
        from app.backends import phones

        number = {"id": "pn-1", "e164": "+15550001111", "sid": "PN1", "routing_type": "none", "project_id": None,
                  "workspace_id": ws, "twilio_account_sid": "AC1",
                  "twilio_auth_token_encrypted": integration_store.BOX.encrypt("tok")}
        await phones.record_number(pool, number)
        await phones.record_number(pool, {**number, "twilio_account_sid": None,
                                          "twilio_auth_token_encrypted": None, "sms_enabled": True})
        assert await phones.twilio_account(pool, "pn-1") == ("AC1", "tok")
        read = await phones.get_number(pool, "pn-1", ws)
        assert read["twilio_credentials"] == "number" and "tok" not in str(read)

        # The Workspace's Twilio account: one row, a number bound with it keeps
        # no token of its own and resolves through the account.
        from app import twilio_store

        await twilio_store.save(pool, ws, account_sid="ACws", auth_token="ws-token", validation={})
        assert (await twilio_store.get(pool, ws))["last4"] == "oken"
        assert "ws-token" not in await pool.fetchval("SELECT auth_token_encrypted FROM twilio_accounts")
        await phones.record_number(pool, {**number, "id": "pn-2", "e164": "+15550002222",
                                          "twilio_account_sid": "ACws", "twilio_auth_token_encrypted": None,
                                          "twilio_source": "workspace"})
        assert await phones.twilio_account(pool, "pn-2") == ("ACws", "ws-token")
        assert [n["id"] for n in await phones.on_workspace_twilio(pool, ws)] == ["pn-2"]
        assert (await phones.get_number(pool, "pn-2", ws))["twilio_credentials"] == "workspace"
        await phones.record_number(pool, {**number, "id": "pn-3", "label": "Support line"})
        assert (await phones.get_number(pool, "pn-3", ws))["label"] == "Support line"

        # A Workspace's own Pipedream project wins over the deployment's.
        from app import pipedream_store

        await pipedream_store.save_credential(
            pool, client_id="deploy", client_secret="d", project_id="proj_d", environment="production"
        )
        assert (await pipedream_store.get_credential(pool, ws)).project_id == "proj_d"
        await pipedream_store.save_workspace_credential(
            pool, ws, client_id="own", client_secret="own-secret", project_id="proj_w", environment="development"
        )
        own = await pipedream_store.get_credential(pool, ws)
        assert (own.client_id, own.client_secret, own.project_id) == ("own", "own-secret", "proj_w")
        assert "own-secret" not in await pool.fetchval("SELECT client_secret_encrypted FROM workspace_pipedream")
        assert "own-secret" not in str(await pipedream_store.workspace_status(pool, ws))
        await pipedream_store.delete_workspace_credential(pool, ws)
        assert (await pipedream_store.get_credential(pool, ws)).project_id == "proj_d"
        assert await pipedream_store.delete_credential(pool) is True
        assert await pipedream_store.get_credential(pool, ws) is None
    finally:
        await pool.close()


def test_integrations_and_registry_round_trip_encrypted():
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
