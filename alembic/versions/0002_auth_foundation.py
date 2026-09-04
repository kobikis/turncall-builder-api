"""Auth foundation — users, workspaces, memberships, user_sessions.

Identity lives entirely in builder-api (ADR-0011); TurnCall stays identity-free.
This revision only stands up the identity model + login-session mechanics. Adding
workspace_id to the existing tables and backfilling a Default Workspace is a
separate revision (ticket #30) so this one carries no data migration.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_STATEMENTS = [
    # password_hash null => Google-only user; google_sub filled by ticket #32.
    """
    CREATE TABLE IF NOT EXISTS users (
        id            uuid PRIMARY KEY,
        email         text NOT NULL UNIQUE,   -- stored lowercased
        password_hash text,
        google_sub    text UNIQUE,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id         uuid PRIMARY KEY,
        name       text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # The RBAC unit: one row per (user, workspace) with a role. A user in two
    # workspaces has two memberships; unique keeps it to one role per pair.
    """
    CREATE TABLE IF NOT EXISTS memberships (
        id           uuid PRIMARY KEY,
        user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        role         text NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
        created_at   timestamptz NOT NULL DEFAULT now(),
        UNIQUE (user_id, workspace_id)
    )
    """,
    # Server-side login sessions (NOT the composer `sessions` table). Opaque token
    # in an httpOnly cookie; deleting the row revokes instantly (ADR-0011).
    """
    CREATE TABLE IF NOT EXISTS user_sessions (
        token      text PRIMARY KEY,
        user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in ("user_sessions", "memberships", "workspaces", "users"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
