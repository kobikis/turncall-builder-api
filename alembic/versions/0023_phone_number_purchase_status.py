"""A bought number's purchase state: pending until its Telnyx order lands (#108, #109).

`phone_numbers.purchase_status`: `'pending'` for a number bought on Telnyx
whose number order had not finished when TurnCall answered, else null. It is
not on the Telnyx account yet, so it can be neither bound nor released; a bind
that succeeds writes a new row without it. `telnyx_order_id` is the Telnyx
number order a Telnyx purchase placed, so the order can be found on the
account. Both are null for every row from before this, and for a number entered
on the form.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-18
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS purchase_status text "
        "CHECK (purchase_status IN ('pending'))"
    )
    op.execute("ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS telnyx_order_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS telnyx_order_id")
    op.execute("ALTER TABLE phone_numbers DROP COLUMN IF EXISTS purchase_status")
