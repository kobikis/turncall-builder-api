"""A builder-held call-init secret, so a test call can carry credentials.

Managed call-init is the only path a Pipedream token reaches a call (ADR-0020),
and until now the only caller was a bound phone number: TurnCall minted the
secret at bind time and the builder wrote it into the backend's .env without
keeping a copy. A browser test call therefore had to resolve by `agent_id`,
which loads the *stored* config — whose mcp_servers deliberately carry no
credential. Connected apps were unreachable from the Test tab, silently: the
agent simply had no tool to call and said the booking was confirmed anyway.

Holding the secret here lets the WebRTC proxy ask for call-init the same way a
phone call does. A later bind overwrites it with TurnCall's own and updates this
column, so the two never drift.

Existing rows are backfilled; the value reaches a backend when its .env is next
written, which is the same migration cost ADR-0019 recorded for builder_token.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-13
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_STATEMENTS = [
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS call_init_secret text",
    """
    UPDATE agent_backends
       SET call_init_secret = replace(gen_random_uuid()::text, '-', '')
                           || replace(gen_random_uuid()::text, '-', '')
     WHERE call_init_secret IS NULL
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE agent_backends DROP COLUMN IF EXISTS call_init_secret")
