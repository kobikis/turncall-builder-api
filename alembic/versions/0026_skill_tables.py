"""The skill library's tables by the name the Console gives them (ADR-0029, addendum).

0024 and 0025 named them after the Console's first name for a TurnCall skill,
Capability. Renamed, constraints too. The audit log is not touched: its entries
keep the resource types they were written under.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-30
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# old table -> (new table, its constraints' suffixes)
_TABLES = {
    "capability_libraries": ("skill_libraries", ("pkey", "workspace_id_fkey")),
    "capability_links": ("skill_links", ("pkey", "workspace_id_fkey", "state_check")),
}


def _rename(old: str, new: str, suffixes: tuple[str, ...]) -> None:
    op.execute(f"ALTER TABLE {old} RENAME TO {new}")
    for suffix in suffixes:
        op.execute(f"ALTER TABLE {new} RENAME CONSTRAINT {old}_{suffix} TO {new}_{suffix}")


def upgrade() -> None:
    for old, (new, suffixes) in _TABLES.items():
        _rename(old, new, suffixes)


def downgrade() -> None:
    for old, (new, suffixes) in _TABLES.items():
        _rename(new, old, suffixes)
