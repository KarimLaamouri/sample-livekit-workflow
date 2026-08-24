"""drop redundant chat sender identity

Revision ID: 0011_drop_chat_sender_identity
Revises: 0010_waiting_room_cancelled
Create Date: 2026-08-24

sender_identity is now redundant with the server-derived sender_name field
and is no longer exposed or used by the application.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_drop_chat_sender_identity"
down_revision: Union[str, None] = "0010_waiting_room_cancelled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("chat_messages", "sender_identity")


def downgrade() -> None:
    # Original values cannot be restored because upgrade permanently removes them.
    # The nullable column only restores the schema shape for existing rows.
    op.add_column(
        "chat_messages",
        sa.Column("sender_identity", sa.LargeBinary(), nullable=True),
    )
