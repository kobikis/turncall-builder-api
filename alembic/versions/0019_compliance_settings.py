"""Workspace compliance settings: redaction and retention (#103, ADR-0028).

workspace_compliance: the Workspace's master setting, one row per Workspace.
`redaction` is {"enabled", "entities"} and `retention` is {"recordings_days",
"transcripts_days", "events_days"}, both in TurnCall's own shape. No row means
the engine's defaults: no redaction, everything kept.

agent_compliance: one Agent's override of it. A null block, or a null
retention field, takes the Workspace's.

compliance_projections: per-target state of writing the effective setting into
that Agent's TurnCall project (or the Workspace's webhook project). `fingerprint`
is a digest of the setting written, so a re-run can skip a project already
holding it.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-17
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS workspace_compliance (
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
        redaction    jsonb NOT NULL,
        retention    jsonb NOT NULL,
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_compliance (
        agent_id     text PRIMARY KEY,
        workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        redaction    jsonb,
        retention    jsonb,
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS compliance_projections (
        workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        agent_id     text NOT NULL,
        state        text NOT NULL,
        fingerprint  text,
        error        text,
        updated_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, agent_id)
    )
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS compliance_projections")
    op.execute("DROP TABLE IF EXISTS agent_compliance")
    op.execute("DROP TABLE IF EXISTS workspace_compliance")
