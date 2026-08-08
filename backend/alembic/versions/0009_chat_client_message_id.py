"""add client_message_id to chat_messages for exact deduplication

Revision ID: 0009_chat_client_message_id
Revises: 0008_app_role_least_privilege
Create Date: 2026-08-08

This migration adds a client_message_id column to chat_messages to enable
exact deduplication between realtime (LiveKit) and persisted (Postgres) chat
messages. The client generates a UUID for each message and sends it with both
the LiveKit data channel and the REST persist call, allowing the frontend to
dedupe by exact ID match instead of heuristic name+body+time-window matching.

The column is nullable to preserve compatibility with existing historical messages
that were persisted before this feature was added. A unique index is scoped per
consultation to prevent duplicate client_message_ids within the same consultation
while allowing the same ID to be reused across different consultations (though
in practice, UUIDs should be globally unique).

Messages with null client_message_id will fall back to the existing heuristic
deduplication logic in the frontend.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_chat_client_message_id"
down_revision: Union[str, None] = "0008_app_role_least_privilege"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add client_message_id column (nullable for backward compatibility)
    op.add_column(
        'chat_messages',
        sa.Column('client_message_id', sa.String(36), nullable=True)
    )
    
    # Create unique index on (consultation_id, client_message_id) for non-null values
    # This prevents duplicate client_message_ids within the same consultation
    op.create_index(
        'ix_chat_messages_consultation_client_message_id',
        'chat_messages',
        ['consultation_id', 'client_message_id'],
        unique=True,
        postgresql_where=sa.text('client_message_id IS NOT NULL')
    )


def downgrade() -> None:
    # Remove the unique index first
    op.drop_index('ix_chat_messages_consultation_client_message_id', table_name='chat_messages')
    
    # Remove the column
    op.drop_column('chat_messages', 'client_message_id')
