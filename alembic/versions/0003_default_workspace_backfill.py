"""Default Workspace + backfill — scope existing data, seed the initial admin.

Turns single-tenant data multi-tenant (spec #28). Adds a nullable workspace_id
to the three Workspace-owned tables, creates one "Default" Workspace, backfills
every pre-existing row to it, then makes the column mandatory — so nothing is
orphaned once scoping turns on. Also seeds an admin User (no password) + admin
Membership so the migrated data has an owner who can claim it later via
"Sign in with Google" on the seeded email (ticket #32) — but only when
INITIAL_ADMIN_EMAIL is set. Left unset (the default for a fresh self-hosted
install), no admin is seeded and the first authenticated user claims Default.

Only builder-api tables are touched — TurnCall projects/keys are untouched.

One statement per op.execute — asyncpg can't prepare multi-statement strings.
Every write is idempotent (IF NOT EXISTS / ON CONFLICT / WHERE ... IS NULL) so a
re-run or a partially-applied upgrade converges.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-10
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Fixed sentinel UUIDs so the seed is deterministic and idempotent across runs.
_DEFAULT_WORKSPACE_ID = "d0000000-0000-0000-0000-000000000001"
_ADMIN_USER_ID = "a0000000-0000-0000-0000-000000000001"
_ADMIN_MEMBERSHIP_ID = "b0000000-0000-0000-0000-000000000001"


def _admin_email() -> str:
    """Address to seed as the initial admin, or "" to seed none.

    Empty by default: a self-hoster gets an ownerless Default workspace and the
    first authenticated user claims it. Set INITIAL_ADMIN_EMAIL to pre-seed an
    owner instead (scripted deploys). Stored lowercased, matching auth_store.

    Read here rather than at import time — Alembic imports every revision module
    to build the version graph, so a module-level read would latch whatever the
    environment held at import and ignore later changes.
    """
    return os.environ.get("INITIAL_ADMIN_EMAIL", "").strip().lower()

_SCOPED_TABLES = ("sessions", "agent_backends", "phone_numbers")


def upgrade() -> None:
    admin_email = _admin_email()

    # 1. Nullable FK first — an ADD COLUMN NOT NULL would reject the existing rows.
    for table in _SCOPED_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            "workspace_id uuid REFERENCES workspaces(id)"
        )

    # 2. The Default Workspace every pre-existing row is adopted into.
    op.execute(
        f"INSERT INTO workspaces (id, name) VALUES ('{_DEFAULT_WORKSPACE_ID}', 'Default') "
        "ON CONFLICT (id) DO NOTHING"
    )

    # 3. Optionally seed an initial admin. Skipped unless INITIAL_ADMIN_EMAIL is
    #    set, leaving the Default workspace ownerless for the first authenticated
    #    user to claim. No password_hash — the account is claimed by signing in
    #    with Google on this verified email (ticket #32).
    # Bare ON CONFLICT DO NOTHING: swallow either unique clash (the fixed id or the
    # email) so a re-run converges no matter which one already exists.
    if admin_email:
        op.execute(
            sa.text(
                "INSERT INTO users (id, email, password_hash) "
                f"VALUES ('{_ADMIN_USER_ID}', :email, NULL) "
                "ON CONFLICT DO NOTHING"
            ).bindparams(email=admin_email)
        )
        # Bind the admin to Default. SELECT by email (not the fixed id) so this
        # still attaches if that email already existed under a different user id.
        op.execute(
            sa.text(
                "INSERT INTO memberships (id, user_id, workspace_id, role) "
                f"SELECT '{_ADMIN_MEMBERSHIP_ID}', u.id, "
                f"'{_DEFAULT_WORKSPACE_ID}', 'admin' "
                "FROM users u WHERE u.email = :email "
                "ON CONFLICT (user_id, workspace_id) DO NOTHING"
            ).bindparams(email=admin_email)
        )

    # 4. Backfill, then lock the column mandatory now that no row is null.
    for table in _SCOPED_TABLES:
        op.execute(
            f"UPDATE {table} SET workspace_id = '{_DEFAULT_WORKSPACE_ID}' "
            "WHERE workspace_id IS NULL"
        )
    for table in _SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN workspace_id SET NOT NULL")


def downgrade() -> None:
    for table in _SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS workspace_id")
    # Remove only the rows this revision seeded.
    op.execute(f"DELETE FROM memberships WHERE id = '{_ADMIN_MEMBERSHIP_ID}'")
    op.execute(f"DELETE FROM users WHERE id = '{_ADMIN_USER_ID}'")
    op.execute(f"DELETE FROM workspaces WHERE id = '{_DEFAULT_WORKSPACE_ID}'")
