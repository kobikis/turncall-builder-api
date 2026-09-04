"""Composer -> Builder rename: sessions.composer_* columns become builder_*.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions RENAME COLUMN composer_provider TO builder_provider")
    op.execute("ALTER TABLE sessions RENAME COLUMN composer_model TO builder_model")


def downgrade() -> None:
    op.execute("ALTER TABLE sessions RENAME COLUMN builder_model TO composer_model")
    op.execute("ALTER TABLE sessions RENAME COLUMN builder_provider TO composer_provider")
