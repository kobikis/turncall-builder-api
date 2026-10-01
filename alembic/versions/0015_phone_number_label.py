"""A label on a phone number ("Support line"), shown in the console.

Builder-side only: TurnCall routes by number and has no use for a name.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS label text")


def downgrade() -> None:
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS label")
