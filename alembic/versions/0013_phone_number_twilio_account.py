"""A phone number's own Twilio account, mirrored so a rebind can re-send it.

The builder rebinds a number when it moves between TurnCall projects, and
TurnCall never returns the Auth Token — so the builder keeps its own sealed copy
(INTEGRATIONS_ENCRYPTION_KEY). The Account SID is an identifier, kept plain.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-01
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_STATEMENTS = [
    "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS twilio_account_sid text",
    "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS twilio_auth_token_encrypted text",
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS twilio_auth_token_encrypted")
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS twilio_account_sid")
