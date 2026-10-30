"""Each project's link to its Workspace's Capability library (#132, ADR-0029).

capability_links: one row per TurnCall project the Builder linked (or tried to
link) to the Workspace's library: each agent's project and the Workspace's
webhook project. Keyed by project, not agent, because an agent's project is
linked at Create before the agent exists. `library_project_id` is the library
it was linked to, so a re-run skips projects already holding this one; a failed
link keeps its `error` until a retry links it.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-30
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_links (
            workspace_id       uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id         text NOT NULL,
            library_project_id text NOT NULL,
            state              text NOT NULL CHECK (state IN ('linked', 'failed')),
            error              text,
            updated_at         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (workspace_id, project_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS capability_links")
