"""The deployment's Pipedream client credential, at rest (ADR-0018).

One row, one credential, per deployment — Pipedream Connect identifies tenants by
`external_user_id` (the Workspace), not by separate client credentials, so there
is nothing per-Workspace to keep here.

Env configures it and the row is the encrypted copy the mint path (#25) reads
back. The plaintext secret never crosses this boundary as a stored value: callers
hand one in to save and get one back to use, but what lands in Postgres is always
ciphertext.

It also holds each Connected-app authorization under way until Pipedream sends
the admin back (`pipedream_connect_flows`, #101): who started it, so the
callback can audit the app in their name without carrying them in the URL.
"""

from __future__ import annotations

import hashlib
import logging

import asyncpg

from . import pipedream
from .integration_store import IntegrationError

logger = logging.getLogger("turncall_builder")

# Singleton: a CHECK on the table keeps it that way.
_ID = 1


async def save_credential(
    pool: asyncpg.Pool,
    *,
    client_id: str,
    client_secret: str,
    project_id: str,
    environment: str,
) -> None:
    """Store (or replace) the deployment credential. Called at startup from env."""
    await pool.execute(
        """
        INSERT INTO pipedream_credentials
            (id, client_id, client_secret_encrypted, project_id, environment)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id) DO UPDATE
            SET client_id               = EXCLUDED.client_id,
                client_secret_encrypted = EXCLUDED.client_secret_encrypted,
                project_id              = EXCLUDED.project_id,
                environment             = EXCLUDED.environment,
                updated_at              = now()
        """,
        _ID,
        client_id,
        pipedream.encrypt_secret(client_secret),
        project_id,
        environment,
    )


async def get_credential(
    pool: asyncpg.Pool, workspace_id: str | None = None
) -> pipedream.Credential | None:
    """Decrypt the credential for use: the Workspace's own Pipedream project
    when it has connected one, else the deployment's. None if neither.

    Raises if an encryption key changed — a loud failure, never a junk secret
    that turns into a confusing 401 from Pipedream.
    """
    if workspace_id:
        own = await workspace_credential(pool, workspace_id)
        if own is not None:
            return own
    row = await pool.fetchrow(
        "SELECT client_id, client_secret_encrypted, project_id, environment "
        "FROM pipedream_credentials WHERE id = $1",
        _ID,
    )
    if row is None:
        return None
    return pipedream.Credential(
        client_id=row["client_id"],
        client_secret=pipedream.decrypt_secret(row["client_secret_encrypted"]),
        project_id=row["project_id"],
        environment=row["environment"],
    )


async def mcp_target(pool: asyncpg.Pool, workspace_id: str | None) -> dict | None:
    """What a catalog-added `mcp_servers` entry points at, for this Workspace.

    None when integrations are unconfigured, when the agent predates Workspace
    scoping, or when the credential cannot be read. Never raises: an unreadable
    Pipedream credential must not be able to block an unrelated prompt edit — the
    attached apps drop out of the config and everything else still applies.
    """
    if not workspace_id:
        return None
    try:
        cred = await get_credential(pool, workspace_id)
    except (pipedream.PipedreamError, IntegrationError):
        logger.exception("pipedream credential unreadable — attached apps dropped")
        return None
    if cred is None:
        return None
    return {
        "project_id": cred.project_id,
        "environment": cred.environment,
        "external_user_id": workspace_id,
    }


# --- a Workspace's own Pipedream project ------------------------------------


async def workspace_credential(pool: asyncpg.Pool, workspace_id: str) -> pipedream.Credential | None:
    from .integration_store import BOX

    row = await pool.fetchrow(
        "SELECT client_id, client_secret_encrypted, project_id, environment "
        "FROM workspace_pipedream WHERE workspace_id = $1",
        workspace_id,
    )
    if row is None:
        return None
    return pipedream.Credential(
        client_id=row["client_id"],
        client_secret=BOX.decrypt(row["client_secret_encrypted"]),
        project_id=row["project_id"],
        environment=row["environment"],
    )


async def workspace_status(pool: asyncpg.Pool, workspace_id: str) -> dict | None:
    """What the card shows — never the secret."""
    row = await pool.fetchrow(
        "SELECT client_id, project_id, environment, updated_at "
        "FROM workspace_pipedream WHERE workspace_id = $1",
        workspace_id,
    )
    return dict(row) if row else None


async def save_workspace_credential(
    pool: asyncpg.Pool,
    workspace_id: str,
    *,
    client_id: str,
    client_secret: str,
    project_id: str,
    environment: str,
) -> None:
    from .integration_store import BOX

    await pool.execute(
        """
        INSERT INTO workspace_pipedream
            (workspace_id, client_id, client_secret_encrypted, project_id, environment)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (workspace_id) DO UPDATE
            SET client_id = EXCLUDED.client_id,
                client_secret_encrypted = EXCLUDED.client_secret_encrypted,
                project_id = EXCLUDED.project_id,
                environment = EXCLUDED.environment,
                updated_at = now()
        """,
        workspace_id,
        client_id,
        BOX.encrypt(client_secret),
        project_id,
        environment,
    )


async def delete_workspace_credential(pool: asyncpg.Pool, workspace_id: str) -> None:
    await pool.execute("DELETE FROM workspace_pipedream WHERE workspace_id = $1", workspace_id)


async def has_deployment_credential(pool: asyncpg.Pool) -> bool:
    return bool(await pool.fetchval("SELECT 1 FROM pipedream_credentials WHERE id = $1", _ID))


async def delete_credential(pool: asyncpg.Pool) -> bool:
    """Remove the deployment's copy (its env was unset). True if one existed."""
    result = await pool.execute("DELETE FROM pipedream_credentials WHERE id = $1", _ID)
    return result.endswith(" 1")


# --- Connected-app flows under way (#101, ADR-0028) --------------------------


def _digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


async def start_connect_flow(
    pool: asyncpg.Pool,
    *,
    nonce: str,
    workspace_id: str,
    app: str,
    actor: dict[str, str],
    known_account_ids: list[str],
    ttl_seconds: int,
) -> None:
    """Hold one authorization under way until its callback: who started it
    (`actor`: id, email, role) and which of the app's accounts the Workspace
    already had. Keyed by the nonce's digest; the nonce itself is never stored.
    Flows nobody came back from are swept here, as new ones start."""
    await pool.execute("DELETE FROM pipedream_connect_flows WHERE expires_at < now()")
    await pool.execute(
        """
        INSERT INTO pipedream_connect_flows
            (nonce_digest, workspace_id, app, user_id, user_email, role,
             known_account_ids, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now() + make_interval(secs => $8))
        """,
        _digest(nonce),
        workspace_id,
        app,
        str(actor["id"]),
        str(actor.get("email") or ""),
        str(actor["role"]),
        list(known_account_ids),
        float(ttl_seconds),
    )


async def take_connect_flow(pool: asyncpg.Pool, nonce: str) -> dict | None:
    """The flow this nonce started, removed as it is read: a callback is
    answered once, and a replay (or an expired flow) finds nothing."""
    row = await pool.fetchrow(
        """
        DELETE FROM pipedream_connect_flows WHERE nonce_digest = $1
        RETURNING workspace_id, app, user_id, user_email, role, known_account_ids,
                  created_at, expires_at > now() AS live
        """,
        _digest(nonce),
    )
    if row is None or not row["live"]:
        return None
    return {
        "workspace_id": str(row["workspace_id"]),
        "app": row["app"],
        "actor": {"id": row["user_id"], "email": row["user_email"], "role": row["role"]},
        "known_account_ids": list(row["known_account_ids"]),
        "started_at": row["created_at"],
    }
