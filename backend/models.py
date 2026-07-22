from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Consultation(Base):
    __tablename__ = "consultations"

    consultation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    room_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    doctor_name: Mapped[str] = mapped_column(String(80), nullable=False)
    patient_name: Mapped[str] = mapped_column(String(80), nullable=False)
    e2ee_key: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    waiting_room_entries: Mapped[list["WaitingRoomEntry"]] = relationship(
        back_populates="consultation",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'ended')", name="ck_consultation_status"),
        Index("ix_consultations_expires_at", "expires_at"),
    )


class WaitingRoomEntry(Base):
    __tablename__ = "waiting_room_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    consultation_id: Mapped[str] = mapped_column(
        ForeignKey("consultations.consultation_id", ondelete="CASCADE"), nullable=False
    )
    participant_name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    consultation: Mapped["Consultation"] = relationship(back_populates="waiting_room_entries")

    __table_args__ = (
        UniqueConstraint("consultation_id", "participant_name", name="uq_waiting_room_participant"),
        CheckConstraint(
            "status IN ('waiting', 'admitted', 'denied')", name="ck_waiting_room_status"
        ),
        CheckConstraint("role IN ('doctor', 'patient', 'observer')", name="ck_waiting_room_role"),
        Index("ix_waiting_room_consultation_status", "consultation_id", "status"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # Intentionally nullable + no FK enforcement: audit rows must survive
    # even if we can't resolve a consultation (e.g. malformed webhook), and
    # must never be lost if a consultation row is ever purged.
    consultation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_audit_events_timestamp", "timestamp"),
        Index("ix_audit_events_consultation_id", "consultation_id"),
    )


class ProcessedWebhookEvent(Base):
    """Idempotency ledger for LiveKit webhook deliveries."""

    __tablename__ = "processed_webhook_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    consultation_id: Mapped[str] = mapped_column(
        ForeignKey("consultations.consultation_id", ondelete="CASCADE"), nullable=False
    )
    sender_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(80), nullable=False)
    sender_role: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("sender_role IN ('doctor', 'patient', 'observer')", name="ck_chat_message_role"),
        Index("ix_chat_messages_consultation_sent_at", "consultation_id", "sent_at"),
    )