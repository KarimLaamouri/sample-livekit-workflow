"""add chat messages table

Revision ID: 0003_add_chat_messages
Revises: 0002_add_locked
Create Date: 2026-07-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_chat_messages"
down_revision: Union[str, None] = "0002_add_locked"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("consultation_id", sa.String(32), nullable=False),
        sa.Column("sender_identity", sa.String(160), nullable=False),
        sa.Column("sender_name", sa.String(80), nullable=False),
        sa.Column("sender_role", sa.String(16), nullable=False),
        sa.Column("body", sa.String(2000), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["consultation_id"], ["consultations.consultation_id"], ondelete="CASCADE"),
        sa.CheckConstraint("sender_role IN ('doctor', 'patient', 'observer')", name="ck_chat_message_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_consultation_sent_at", "chat_messages", ["consultation_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_consultation_sent_at", table_name="chat_messages")
    op.drop_table("chat_messages")
