"""widen waiting_room_entries status check to allow cancelled

Revision ID: 0010_waiting_room_cancelled
Revises: 0009_chat_client_message_id
Create Date: 2026-08-20

The application now supports a "cancelled" state for waiting-room entries
(patient explicitly cancels while waiting for doctor admission). This
migration widens the ck_waiting_room_status CHECK constraint to accept
the new value, matching the WaitingRoomStatus Literal type in main.py
and the POST .../cancel endpoint added alongside this migration.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_waiting_room_cancelled"
down_revision: Union[str, None] = "0009_chat_client_message_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_waiting_room_status", "waiting_room_entries", type_="check")
    op.create_check_constraint(
        "ck_waiting_room_status",
        "waiting_room_entries",
        "status IN ('waiting', 'admitted', 'denied', 'cancelled')",
    )


def downgrade() -> None:
    # NOTE: downgrade will fail with CheckViolationError if any rows
    # already have status='cancelled', since the narrower constraint
    # would reject them. Manually UPDATE or DELETE those rows first.
    op.drop_constraint("ck_waiting_room_status", "waiting_room_entries", type_="check")
    op.create_check_constraint(
        "ck_waiting_room_status",
        "waiting_room_entries",
        "status IN ('waiting', 'admitted', 'denied')",
    )
