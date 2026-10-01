"""Provider Integrations and the Workspace MCP server registry (ADR-0023).

provider_integrations: one row per (Workspace, provider account) — never per
role. `secret_encrypted` is Fernet ciphertext over a JSON document ({"api_key"}
or an AWS credential set); `last4` is the only fragment ever shown. `version`
changes on every write, so projection can tell an agent that already holds this
credential from one that holds an older one.

integration_projections: per-agent state of writing (or removing) a Workspace's
credential into that agent's TurnCall project. Projection is N writes, so it is
recorded, retryable, and visible while incomplete.

workspace_mcp_servers: an MCP server registered once; headers (which carry
bearer tokens) are encrypted. Agents opt in by id in their own config.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-01
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS provider_integrations (
        workspace_id     uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        provider         text NOT NULL,
        secret_encrypted text NOT NULL,
        last4            text NOT NULL DEFAULT '',
        validation       jsonb NOT NULL DEFAULT '{}'::jsonb,
        version          uuid NOT NULL DEFAULT gen_random_uuid(),
        disconnecting    boolean NOT NULL DEFAULT false,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integration_projections (
        workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        provider     text NOT NULL,
        agent_id     text NOT NULL,
        state        text NOT NULL,
        version      uuid,
        error        text,
        updated_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, provider, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspace_mcp_servers (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        workspace_id      uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        name              text NOT NULL,
        transport         text NOT NULL CHECK (transport IN ('http', 'sse')),
        url               text NOT NULL,
        headers_encrypted text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        UNIQUE (workspace_id, name)
    )
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_mcp_servers")
    op.execute("DROP TABLE IF EXISTS integration_projections")
    op.execute("DROP TABLE IF EXISTS provider_integrations")
