"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "consultations",
        sa.Column("consultation_id", sa.String(length=32), primary_key=True),
        sa.Column("room_name", sa.String(length=128), nullable=False),
        sa.Column("doctor_name", sa.String(length=80), nullable=False),
        sa.Column("patient_name", sa.String(length=80), nullable=False),
        sa.Column("e2ee_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by", sa.String(length=80), nullable=True),
        sa.UniqueConstraint("room_name", name="uq_consultations_room_name"),
        sa.CheckConstraint("status IN ('active', 'ended')", name="ck_consultation_status"),
    )
    op.create_index(
        "ix_consultations_expires_at", "consultations", ["expires_at"]
    )

    op.create_table(
        "waiting_room_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "consultation_id",
            sa.String(length=32),
            sa.ForeignKey("consultations.consultation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("participant_name", sa.String(length=80), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="waiting"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "consultation_id", "participant_name", name="uq_waiting_room_participant"
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'admitted', 'denied')", name="ck_waiting_room_status"
        ),
        sa.CheckConstraint(
            "role IN ('doctor', 'patient', 'observer')", name="ck_waiting_room_role"
        ),
    )
    op.create_index(
        "ix_waiting_room_consultation_status",
        "waiting_room_entries",
        ["consultation_id", "status"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("consultation_id", sa.String(length=32), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_audit_events_timestamp", "audit_events", ["timestamp"])
    op.create_index(
        "ix_audit_events_consultation_id", "audit_events", ["consultation_id"]
    )

    op.create_table(
        "processed_webhook_events",
        sa.Column("event_id", sa.String(length=128), primary_key=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("processed_webhook_events")
    op.drop_index("ix_audit_events_consultation_id", table_name="audit_events")
    op.drop_index("ix_audit_events_timestamp", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(
        "ix_waiting_room_consultation_status", table_name="waiting_room_entries"
    )
    op.drop_table("waiting_room_entries")
    op.drop_index("ix_consultations_expires_at", table_name="consultations")
    op.drop_table("consultations")