"""Workspace-authored Builder skills (ADR-0015).

A Skill teaches the Builder how to build a kind of agent — the four that ship
with the builder cover tools, guardrails, speech-to-speech and reading attached
documents. These are written by a Workspace admin and mounted alongside them.

Scoped to the Workspace, not the Agent: a skill is "how we build agents here".
Per-agent guidance is just that agent's system prompt, which already exists.

Body holds markdown WITHOUT frontmatter — name and description are columns, and
the SKILL.md frontmatter is rendered from them at materialise time. Nobody
should have to hand-write YAML to teach the builder something, and a malformed
frontmatter block makes a skill vanish silently rather than fail.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-07
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS workspace_skills (
        id           uuid PRIMARY KEY,
        workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        name         text NOT NULL,
        description  text NOT NULL,
        body         text NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    # deepagents keys skills by name and lets a later source win, so two skills
    # with one name inside a workspace would silently shadow each other.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS workspace_skills_name_uniq
        ON workspace_skills (workspace_id, name)
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_skills")
