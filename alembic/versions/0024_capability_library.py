"""A Workspace's Capability library (#131, ADR-0029).

capability_libraries: the one TurnCall project per Workspace that holds its
Capabilities (TurnCall's skills), and an admin key to it. The key is sealed
under INTEGRATIONS_ENCRYPTION_KEY, as a Workspace's provider keys are. One row
per Workspace, which the primary key holds even if two first creates race past
the advisory lock that is meant to stop them.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-30
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_libraries (
            workspace_id      uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id        text NOT NULL,
            api_key_encrypted text NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS capability_libraries")
