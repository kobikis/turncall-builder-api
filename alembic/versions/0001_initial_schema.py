"""Initial schema — sessions, agent_backends, phone_numbers, webhook_project.

Uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so a pre-Alembic dev database
(previously created by db.py at startup) adopts this baseline cleanly: running
`alembic upgrade head` on it is a no-op that just stamps the version.

One statement per op.execute — asyncpg can't prepare multi-statement strings.

Revision ID: 0001
Revises:
Create Date: 2026-07-04
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id         uuid PRIMARY KEY,
        history    jsonb NOT NULL DEFAULT '[]'::jsonb,
        config     jsonb,
        agent_id   text,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # One row per generated Agent Backend (ADR-0004). The builder is a router +
    # proxy; events themselves live in each backend's own store (ADR-0005).
    """
    CREATE TABLE IF NOT EXISTS agent_backends (
        agent_id       text PRIMARY KEY,
        project_id     text,
        api_key        text,
        slug           text NOT NULL,
        port           int  NOT NULL UNIQUE,
        service_dir    text NOT NULL,
        status         text NOT NULL DEFAULT 'generating',
        config         jsonb,
        webhook_secret text,     -- event/tool signing secret (also in the backend's .env)
        tool_statuses  jsonb,    -- per-tool provenance: {"name": "generated" | "stub"}
        deleted        boolean NOT NULL DEFAULT false,  -- teardown hides, port stays reserved
        created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Adoption path for pre-Alembic databases missing newer columns.
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS project_id text",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS api_key text",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS config jsonb",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS webhook_secret text",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS tool_statuses jsonb",
    "ALTER TABLE agent_backends ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false",
    # Mirror of every phone number the builder bound (ADR-0006). Source of truth
    # for the console's Phone Numbers list — one query, not an N-project scan.
    """
    CREATE TABLE IF NOT EXISTS phone_numbers (
        id                text PRIMARY KEY,   -- TurnCall phone id, or a local uuid when unassigned
        e164              text NOT NULL,
        sid               text NOT NULL,
        routing_type      text NOT NULL,      -- 'agent' | 'agent_call_init' | 'webhook' | 'none'
        agent_id          text,               -- null unless routed to an agent
        project_id        text,               -- null when unassigned (not bound in TurnCall)
        server_url        text,               -- null unless call-init routed
        server_url_secret text,
        sms_enabled       boolean NOT NULL DEFAULT false,
        created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE phone_numbers ALTER COLUMN project_id DROP NOT NULL",
    "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS server_url_secret text",
    # Singleton project + key for webhook-routed (call-init) numbers (ADR-0006).
    """
    CREATE TABLE IF NOT EXISTS webhook_project (
        id         int PRIMARY KEY DEFAULT 1,
        project_id text NOT NULL,
        api_key    text NOT NULL
    )
    """,
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in ("webhook_project", "phone_numbers", "agent_backends", "sessions"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
