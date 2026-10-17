"""Connected-app flows under way, held server-side (#101, ADR-0028).

Starting a Connected app's authorization sends the admin to Pipedream, and
Pipedream sends them back to `/integrations/callback`, which is where the app
comes to exist and its audit entry is written, in the admin's name. Who started
it used to ride along in the connect state: signed, not encrypted, so the
admin's id, email and role were readable by Pipedream, every proxy log and the
browser history, and the state stayed valid for its whole ten minutes, so a
replayed callback recorded the connection again.

`pipedream_connect_flows` holds one flow per authorization started: the
Workspace, the app, who started it and under which role, and the ids of the
app's accounts the Workspace already had (what the callback compares against
to tell which account this flow made). The state carries only an opaque nonce,
and the row is keyed by its digest, so the table alone cannot answer a
callback. The callback deletes the row as it reads it: one flow, one entry.

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
        """
        CREATE TABLE pipedream_connect_flows (
            nonce_digest      text PRIMARY KEY,
            workspace_id      uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            app               text NOT NULL,
            user_id           text NOT NULL,
            user_email        text NOT NULL,
            role              text NOT NULL,
            known_account_ids text[] NOT NULL DEFAULT '{}',
            created_at        timestamptz NOT NULL DEFAULT now(),
            expires_at        timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_pipedream_connect_flows_expires ON pipedream_connect_flows (expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pipedream_connect_flows")
