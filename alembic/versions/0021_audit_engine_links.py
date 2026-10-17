"""An audit entry links to every engine audit entry its change caused (#102).

0020 gave each entry one `turncall_project_id` and one `engine_audit_id`. One
person's change is often many TurnCall requests, across projects: Create makes
the project, its key, the agent, and projects the Workspace's credentials and
compliance settings into it; a compliance or credential change is written into
every Agent's project. Each such request has its own engine audit id, so an
entry now holds a list, `engine_links`: one `{project_id, engine_audit_id}` per
request, in the order made, empty when the change never reached TurnCall.

The two columns were never written (#101 left them null), so nothing is lost
going up. Going down drops the links: the append-only trigger refuses the
UPDATE that would copy one back.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE audit_entries "
        "ADD COLUMN engine_links jsonb NOT NULL DEFAULT '[]'::jsonb, "
        "DROP COLUMN turncall_project_id, "
        "DROP COLUMN engine_audit_id"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE audit_entries "
        "DROP COLUMN engine_links, "
        "ADD COLUMN turncall_project_id text, "
        "ADD COLUMN engine_audit_id text"
    )
