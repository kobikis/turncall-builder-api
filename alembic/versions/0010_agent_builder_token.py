"""A per-Agent-Backend credential for calling builder-api (ADR-0019).

An Agent Backend needs a short-lived Pipedream access token at call time and
must not hold a credential of its own (ADR-0018), so it asks the builder. This
column is how it proves which Agent it is.

Deliberately NOT the webhook_secret. That one is held by TurnCall as well —
it signs TurnCall's requests *into* the backend — and reusing it would make
TurnCall able to mint Pipedream tokens for every Workspace it serves. Different
direction, different trust, different secret.

Existing rows are backfilled so the column is usable everywhere at once; the
value only becomes reachable to a backend when its .env is next written, which
is what the ADR records as the migration cost.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-12
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_STATEMENTS = [
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS builder_token text",
    # gen_random_uuid() is built in from PG13 — no pgcrypto dependency for a
    # backfill that only has to be unguessable, not cryptographically exotic.
    """
    UPDATE agent_backends
       SET builder_token = replace(gen_random_uuid()::text, '-', '')
                        || replace(gen_random_uuid()::text, '-', '')
     WHERE builder_token IS NULL
    """,
    # The token IS the identity: a lookup by it must never find two agents.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_backends_builder_token_uniq
        ON agent_backends (builder_token)
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agent_backends_builder_token_uniq")
    op.execute("ALTER TABLE agent_backends DROP COLUMN IF EXISTS builder_token")
