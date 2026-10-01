"""A Workspace's own Pipedream project (the Connected apps card).

Without a row the Workspace's Connected apps live in the deployment's project
(PIPEDREAM_* in the env), as before. With one, they live in the Workspace's own
project — its own billing and limits. The client secret is sealed under
INTEGRATIONS_ENCRYPTION_KEY; the other three fields are identifiers.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_pipedream (
            workspace_id            uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
            client_id               text NOT NULL,
            client_secret_encrypted text NOT NULL,
            project_id              text NOT NULL,
            environment             text NOT NULL DEFAULT 'production',
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_pipedream")
