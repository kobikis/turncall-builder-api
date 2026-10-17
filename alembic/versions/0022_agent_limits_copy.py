"""The builder's copy of each Agent's limits (#76, #101).

`agent_backends.limits`: the limits TurnCall last reported for the Agent's
project, written from its answer to every read and every save. A limits change
is audited with what it replaced (ADR-0028); without a copy, the only way to
know was an extra GET to TurnCall on every save, and a GET that failed left the
entry claiming every limit had been null. The builder holds the only admin key
for an Agent's project, so its copy is the project's limits. Null means never
seen here: the entry then says the previous value is unknown.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS limits jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_backends DROP COLUMN IF EXISTS limits")
