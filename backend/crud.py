from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditEvent, Consultation, ProcessedWebhookEvent, WaitingRoomEntry

DEFAULT_AUDIT_LIMIT = 200


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Consultations
# --------------------------------------------------------------------------

async def create_consultation(
    session: AsyncSession,
    *,
    consultation_id: str,
    room_name: str,
    doctor_name: str,
    patient_name: str,
    e2ee_key: str,
    expires_at: datetime,
) -> Consultation:
    consultation = Consultation(
        consultation_id=consultation_id,
        room_name=room_name,
        doctor_name=doctor_name,
        patient_name=patient_name,
        e2ee_key=e2ee_key,
        expires_at=expires_at,
        status="active",
    )
    session.add(consultation)
    await session.flush()
    return consultation


async def get_consultation_or_404(
    session: AsyncSession,
    consultation_id: str,
    *,
    include_ended: bool = False,
    for_update: bool = False,
) -> Consultation:
    """Mirrors the original in-memory lookup semantics:
    404 if unknown, 410 if past expiry, 409 if ended (unless include_ended).

    Note: unlike the original dict-backed implementation, expired
    consultations are NOT deleted here -- they're left in place so the
    audit trail (and any FK-less audit rows referencing them) stays intact.
    A periodic cleanup job can archive/purge old rows separately if needed.
    """
    stmt = select(Consultation).where(Consultation.consultation_id == consultation_id)
    if for_update:
        stmt = stmt.with_for_update()

    consultation = (await session.execute(stmt)).scalar_one_or_none()

    if consultation is None:
        raise HTTPException(status_code=404, detail="Consultation not found")

    if consultation.expires_at < utc_now():
        await create_audit_event(
            session, "consultation.expired", consultation_id=consultation_id
        )
        raise HTTPException(status_code=410, detail="Consultation expired")

    if not include_ended and consultation.status == "ended":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONSULTATION_ENDED",
                "message": "Consultation has ended",
            },
        )

    return consultation


async def find_consultation_by_room_name(
    session: AsyncSession, room_name: str
) -> Consultation | None:
    stmt = select(Consultation).where(Consultation.room_name == room_name)
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_consultation_by_room_metadata(
    session: AsyncSession, room_metadata: Any
) -> Consultation | None:
    if not isinstance(room_metadata, dict):
        return None
    consultation_id = room_metadata.get("consultation_id")
    if not isinstance(consultation_id, str):
        return None
    stmt = select(Consultation).where(Consultation.consultation_id == consultation_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_consultation_ended_state(
    session: AsyncSession,
    consultation: Consultation,
    *,
    ended_by: str,
) -> str | None:
    """Idempotent transition to 'ended'. Returns the ended_at timestamp, or
    None if the consultation was already ended (no-op, matching the
    original semantics used to distinguish "I ended it" vs "already
    ended by someone/something else")."""
    if consultation.status == "ended":
        return None

    ended_at = utc_now()
    consultation.status = "ended"
    consultation.ended_at = ended_at
    consultation.ended_by = ended_by
    await session.flush()
    return ended_at.isoformat()


async def mark_consultation_ended_by_system(
    session: AsyncSession, consultation: Consultation
) -> bool:
    ended_at = await set_consultation_ended_state(session, consultation, ended_by="system")
    if ended_at is None:
        return False

    await create_audit_event(
        session,
        "consultation.ended",
        consultation_id=consultation.consultation_id,
        room_name=consultation.room_name,
        ended_by="system",
        source="webhook",
    )
    return True


# --------------------------------------------------------------------------
# Waiting room
# --------------------------------------------------------------------------

async def get_waiting_room_entry(
    session: AsyncSession,
    consultation_id: str,
    participant_name: str,
    *,
    for_update: bool = False,
) -> WaitingRoomEntry | None:
    stmt = select(WaitingRoomEntry).where(
        WaitingRoomEntry.consultation_id == consultation_id,
        WaitingRoomEntry.participant_name == participant_name,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_waiting_room_entry(
    session: AsyncSession,
    *,
    consultation_id: str,
    participant_name: str,
    role: str,
    status: str,
) -> WaitingRoomEntry:
    entry = WaitingRoomEntry(
        consultation_id=consultation_id,
        participant_name=participant_name,
        role=role,
        status=status,
        requested_at=utc_now(),
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_waiting_entries(
    session: AsyncSession, consultation_id: str, *, status: str = "waiting"
) -> list[WaitingRoomEntry]:
    stmt = select(WaitingRoomEntry).where(
        WaitingRoomEntry.consultation_id == consultation_id,
        WaitingRoomEntry.status == status,
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_waiting_room_status(
    session: AsyncSession, entry: WaitingRoomEntry, status: str
) -> WaitingRoomEntry:
    entry.status = status
    await session.flush()
    return entry


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

def _clean_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, sub_value in value.items():
            cleaned_value = _clean_audit_value(sub_value)
            if cleaned_value is not None:
                cleaned[key] = cleaned_value
        return cleaned or None
    if isinstance(value, list):
        cleaned_list = [_clean_audit_value(item) for item in value]
        cleaned_list = [item for item in cleaned_list if item is not None]
        return cleaned_list or None
    return value


async def create_audit_event(
    session: AsyncSession, event_type: str, **details: Any
) -> AuditEvent:
    consultation_id = details.pop("consultation_id", None)
    cleaned_details = {
        key: _clean_audit_value(value)
        for key, value in details.items()
        if value is not None
    }

    event = AuditEvent(
        event_type=event_type,
        consultation_id=consultation_id,
        details=cleaned_details or None,
    )
    session.add(event)
    await session.flush()
    return event


async def list_audit_events(
    session: AsyncSession, limit: int = DEFAULT_AUDIT_LIMIT
) -> list[AuditEvent]:
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
        .limit(limit)
    )
    events = list((await session.execute(stmt)).scalars().all())
    events.reverse()  # oldest-first, matching the original list's append order
    return events


# --------------------------------------------------------------------------
# Webhook idempotency
# --------------------------------------------------------------------------

async def remember_webhook_event_id(session: AsyncSession, event_id: str) -> bool:
    """Returns True if this event_id was newly recorded (i.e. should be
    processed), False if it's a duplicate delivery we've already seen."""
    stmt = (
        pg_insert(ProcessedWebhookEvent)
        .values(event_id=event_id)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount > 0