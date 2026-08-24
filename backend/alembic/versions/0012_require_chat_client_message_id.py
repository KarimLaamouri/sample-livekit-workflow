"""require chat client message IDs

Revision ID: 0012_require_chat_ids
Revises: 0011_drop_chat_sender_identity
Create Date: 2026-08-24

All chat messages now use client-generated UUIDs for exact deduplication
between LiveKit and persisted history.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_require_chat_ids"
down_revision: Union[str, None] = "0011_drop_chat_sender_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # DESTRUCTIVE: pre-production cleanup for messages created before IDs were mandatory.
    # These rows cannot participate in exact client_message_id deduplication.
    result = op.get_bind().execute(
        sa.text("DELETE FROM chat_messages WHERE client_message_id IS NULL")
    )
    print(f"Deleted {result.rowcount} chat message(s) without client_message_id.")

    op.alter_column(
        "chat_messages",
        "client_message_id",
        existing_type=sa.String(36),
        nullable=False,
    )


def downgrade() -> None:
    # The NULL-ID rows deleted during upgrade cannot be restored.
    op.alter_column(
        "chat_messages",
        "client_message_id",
        existing_type=sa.String(36),
        nullable=True,
    )
