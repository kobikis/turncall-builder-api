"""A phone number's carrier: twilio, telnyx or asterisk (#73, turncall ADR-0022).

Every number bound before this was a Twilio number, so the default is the
backfill.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-09
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS carrier text NOT NULL DEFAULT 'twilio'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS carrier")
