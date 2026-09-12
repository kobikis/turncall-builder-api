"""The deployment's Pipedream client credential (ADR-0018).

One row. Pipedream Connect identifies tenants by `external_user_id` — which is
the Workspace id — so there is no per-Workspace credential to keep, and there is
no local mirror of what is connected either: Pipedream is the system of record
for that, and a mirror would quietly disagree the moment someone disconnects from
Pipedream's own UI.

client_secret_encrypted holds Fernet ciphertext, never the secret. client_id,
project_id and environment are plaintext on purpose — none is a secret, and the
console needs to be able to say which project and environment it is talking to.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS pipedream_credentials (
        id                      int PRIMARY KEY,
        client_id               text NOT NULL,
        client_secret_encrypted text NOT NULL,
        project_id              text NOT NULL,
        environment             text NOT NULL DEFAULT 'development',
        created_at              timestamptz NOT NULL DEFAULT now(),
        updated_at              timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pipedream_credentials_singleton CHECK (id = 1)
    )
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pipedream_credentials")
