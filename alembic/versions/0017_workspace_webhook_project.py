"""A webhook project per Workspace (#65).

Webhook-routed numbers (a customer's own call-init server) were bound in one
builder-owned project shared by every Workspace, so no Workspace's provider
keys could be projected into it without lending them to the others — those
numbers ran on the platform's keys. Each Workspace now gets its own, created
on its first webhook bind; the old shared row (id 1, no workspace) stays for
the numbers already bound in it, and for the platform-wide `/v1/providers`
read.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE webhook_project ADD COLUMN IF NOT EXISTS workspace_id uuid "
        "REFERENCES workspaces(id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS webhook_project_id_seq START 2 "
        "OWNED BY webhook_project.id"
    )
    op.execute(
        "ALTER TABLE webhook_project ALTER COLUMN id "
        "SET DEFAULT nextval('webhook_project_id_seq')"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_project_workspace "
        "ON webhook_project (workspace_id)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM webhook_project WHERE workspace_id IS NOT NULL")
    op.execute("DROP INDEX IF EXISTS uq_webhook_project_workspace")
    op.execute("ALTER TABLE webhook_project ALTER COLUMN id SET DEFAULT 1")
    op.execute("DROP SEQUENCE IF EXISTS webhook_project_id_seq")
    op.execute("ALTER TABLE webhook_project DROP COLUMN IF EXISTS workspace_id")
