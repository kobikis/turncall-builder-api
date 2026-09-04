"""Composer model choice per Session (provider + model, both-or-neither).

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS composer_provider text")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS composer_model text")


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS composer_model")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS composer_provider")
