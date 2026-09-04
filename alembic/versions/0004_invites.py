"""Invites — team invitations (ticket #34).

An admin invites a teammate by email with a role; the invitee accepts (after
signup/login) to gain a Membership in the inviting Workspace. Joining is
invite-only, so the token is the capability and the email binds who may accept.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-10
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_STATEMENTS = [
    # accepted_at NULL => pending. token is the opaque invite-link secret; email
    # (lowercased) is who may accept it, so a leaked token alone can't join.
    # expires_at bounds the window (recycled/leaked address can't redeem forever).
    """
    CREATE TABLE IF NOT EXISTS invites (
        id           uuid PRIMARY KEY,
        workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        email        text NOT NULL,
        role         text NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
        token        text NOT NULL UNIQUE,
        invited_by   uuid REFERENCES users(id) ON DELETE SET NULL,
        accepted_at  timestamptz,
        expires_at   timestamptz NOT NULL DEFAULT (now() + interval '7 days'),
        created_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS invites")
