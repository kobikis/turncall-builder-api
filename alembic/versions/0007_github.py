"""GitHub connections + linked repos (ADR-0013).

A GitHub connection is per-User, not per-Workspace: the credential is authorized
by a person against an account they control, and pushes are attributed to them.
The token is stored encrypted — it is the first credential the builder must read
back rather than hash, so a database dump alone must not yield write access to
anyone's repositories.

A linked repo is where one Agent Backend pushes: owner, repo, branch and an
optional path inside it. The user chooses all four; nothing is created for them.
The path is what lets a workspace keep every agent in one repository under
`agents/<slug>/` instead of one repository each.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_STATEMENTS = [
    # token_encrypted holds Fernet ciphertext, never the token. github_login and
    # expires_at are plaintext on purpose: the console shows both, and neither is
    # a secret. expires_at is what lets us warn before pushes start failing
    # quietly, which is the standing cost of choosing a token over an App.
    """
    CREATE TABLE IF NOT EXISTS github_connections (
        id              uuid PRIMARY KEY,
        user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        github_login    text NOT NULL,
        token_encrypted text NOT NULL,
        expires_at      timestamptz,
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    # One connection per user: reconnecting replaces rather than accumulates.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS github_connections_user_uniq
        ON github_connections (user_id)
    """,
    # The linked repo lives on the backend it pushes: 1:1, and it dies with the
    # backend row rather than outliving it as an orphan.
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_owner text",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_repo text",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_branch text",
    # NULL/'' = repo root. Otherwise the subtree this agent owns.
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_path text",
    # Which connection owns the link — the one that made it, and whose token
    # later pushes use. ON DELETE SET NULL: losing the connection must not lose
    # the link. Pushing stops; another user can take it over.
    """
    ALTER TABLE agent_backends
        ADD COLUMN IF NOT EXISTS github_connection_id uuid
        REFERENCES github_connections(id) ON DELETE SET NULL
    """,
    # Content hash of what we last pushed. Compared against the remote subtree
    # before the next push, so an upstream edit is detected rather than silently
    # overwritten — this column is what makes "fail loudly on divergence" real.
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_tree_hash text",
    # Last push outcome, surfaced in the console. Divergence is a state the user
    # resolves in their own repo — we record it, we never force past it.
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_pushed_at timestamptz",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS github_push_error text",
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for column in (
        "github_tree_hash",
        "github_push_error",
        "github_pushed_at",
        "github_connection_id",
        "github_path",
        "github_branch",
        "github_repo",
        "github_owner",
    ):
        op.execute(f"ALTER TABLE agent_backends DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS github_connections")
