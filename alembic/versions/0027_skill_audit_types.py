"""The audit log's resource types by the Console's names (ADR-0029, addendum).

`skill` was a Builder skill, `capability` and `capability_library` a skill and
the skill library. They become `builder_skill`, `skill` and `skill_library`, in
the entries already written too, so one type means one thing across the log.
Each entry keeps everything else it recorded. The append-only trigger refuses
every UPDATE, so this migration, as the table's owner, disables it for the one
statement and enables it again, in the same transaction.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-30
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_RENAMED = (("skill", "builder_skill"), ("capability", "skill"), ("capability_library", "skill_library"))


def _retype(pairs: tuple[tuple[str, str], ...]) -> None:
    # One UPDATE, so `skill` -> `builder_skill` and `capability` -> `skill`
    # never run into each other.
    cases = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in pairs)
    olds = ", ".join(f"'{old}'" for old, _ in pairs)
    op.execute("ALTER TABLE audit_entries DISABLE TRIGGER audit_entries_append_only")
    op.execute(
        f"UPDATE audit_entries SET resource_type = CASE resource_type {cases} END "
        f"WHERE resource_type IN ({olds})"
    )
    op.execute("ALTER TABLE audit_entries ENABLE TRIGGER audit_entries_append_only")


def upgrade() -> None:
    _retype(_RENAMED)


def downgrade() -> None:
    _retype(tuple((new, old) for old, new in _RENAMED))
