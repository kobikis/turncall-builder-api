"""The deployment's Pipedream client credential, at rest (ADR-0018).

One row, one credential, per deployment — Pipedream Connect identifies tenants by
`external_user_id` (the Workspace), not by separate client credentials, so there
is nothing per-Workspace to keep here.

Env configures it and the row is the encrypted copy the mint path (#25) reads
back. The plaintext secret never crosses this boundary as a stored value: callers
hand one in to save and get one back to use, but what lands in Postgres is always
ciphertext.
"""

from __future__ import annotations

import asyncpg

from . import pipedream

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


async def get_credential(pool: asyncpg.Pool) -> pipedream.Credential | None:
    """Decrypt the stored credential for use, or None if unconfigured.

    Raises if the encryption key changed — a loud failure, never a junk secret
    that turns into a confusing 401 from Pipedream.
    """
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
