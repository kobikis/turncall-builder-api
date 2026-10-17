"""The audit log of people (#101, ADR-0028): `audit_entries`.

One row per change a person made through the Console: the User (and their
email as it was), the Workspace, the Role they acted under, the resource, the
action, what changed, and when.

`engine_links` links an entry to TurnCall's own audit log (#102): one
`{project_id, engine_audit_id}` per TurnCall request the change made that the
engine audited, in the order made, empty when it never reached TurnCall. A
list, not one column pair: one person's change is often many requests across
projects (Create makes the project, its key and the agent and projects the
Workspace's credentials and compliance settings into it; a compliance or
credential change is written into every Agent's project).

`outcome` is `completed`, or `partial` for a change of several steps that
failed after some of them had landed, with `error` saying why: the entry
records what happened, so the change is in the log even though the request
failed.

No foreign keys: an entry outlives the member who made it, the agent it is
about and the Workspace's other rows. `agent_ids` holds every Agent an entry is
about (a number re-pointed from one to another is about both), so "what
happened to this agent" is one indexed lookup.

Append-only in the database too: a trigger refuses every UPDATE and TRUNCATE,
and every DELETE except the retention janitor's, which marks its transaction
with `SET LOCAL builder.audit_retention = 'on'`. It guards against the
application and anything connecting as it, not against the table's owner, who
can drop the trigger.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-17
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_entries (
            id                  uuid PRIMARY KEY,
            workspace_id        uuid NOT NULL,
            user_id             uuid NOT NULL,
            user_email          text NOT NULL,
            role                text NOT NULL,
            resource_type       text NOT NULL,
            resource_id         text NOT NULL,
            resource_name       text,
            action              text NOT NULL,
            agent_ids           text[] NOT NULL DEFAULT '{}',
            changes             jsonb NOT NULL DEFAULT '{}'::jsonb,
            request_id          uuid NOT NULL,
            engine_links        jsonb NOT NULL DEFAULT '[]'::jsonb,
            outcome             text NOT NULL DEFAULT 'completed'
                                CHECK (outcome IN ('completed', 'partial')),
            error               text,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_audit_entries_workspace_created "
        "ON audit_entries (workspace_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_entries_workspace_user "
        "ON audit_entries (workspace_id, user_id, created_at DESC)"
    )
    op.execute("CREATE INDEX ix_audit_entries_agent_ids ON audit_entries USING gin (agent_ids)")
    op.execute("CREATE INDEX ix_audit_entries_created_at ON audit_entries (created_at)")
    op.execute(
        """
        CREATE FUNCTION audit_entries_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('builder.audit_retention', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_entries is append-only: % refused', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_entries_append_only BEFORE UPDATE OR DELETE ON audit_entries "
        "FOR EACH ROW EXECUTE FUNCTION audit_entries_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_entries_no_truncate BEFORE TRUNCATE ON audit_entries "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_entries_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_entries")  # its triggers go with it
    op.execute("DROP FUNCTION IF EXISTS audit_entries_append_only()")
