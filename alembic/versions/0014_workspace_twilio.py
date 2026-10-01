"""A Workspace's Twilio account, connected once on the Integrations page.

One per Workspace: numbers bound "with the workspace's Twilio account" are
marked in the phone mirror (`twilio_source = 'workspace'`) so a token rotation
can reach each of them, and their own sealed copy is not kept — the account row
is the copy. TurnCall still stores the pair per number (its unit is the number).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS twilio_accounts (
        workspace_id         uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
        account_sid          text NOT NULL,
        auth_token_encrypted text NOT NULL,
        last4                text NOT NULL DEFAULT '',
        validation           jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at           timestamptz NOT NULL DEFAULT now(),
        updated_at           timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS twilio_source text",
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS twilio_source")
    op.execute("DROP TABLE IF EXISTS twilio_accounts")
