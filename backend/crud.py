import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from encryption import blind_index
from e2ee import current_e2ee_key_version
from models import AuditEvent, ChatMessage, Consultation, ProcessedWebhookEvent, WaitingRoomEntry


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
    expires_at: datetime,
) -> Consultation:
    consultation = Consultation(
        consultation_id=consultation_id,
        room_name=room_name,
        doctor_name=doctor_name,
        patient_name=patient_name,
        e2ee_key_version=current_e2ee_key_version(),
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


async def set_consultation_locked(
    session: AsyncSession, consultation: Consultation, locked: bool
) -> bool:
    """Idempotent. Returns False (no-op) if consultation.locked is
    already in the requested state, True if it changed — mirrors the
    early-return style of set_consultation_ended_state."""
    if consultation.locked == locked:
        return False
    consultation.locked = locked
    await session.flush()
    return True


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
    participant_name_hash = blind_index(participant_name)
    stmt = select(WaitingRoomEntry).where(
        WaitingRoomEntry.consultation_id == consultation_id,
        WaitingRoomEntry.participant_name_hash == participant_name_hash,
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
        participant_name_hash=blind_index(participant_name),
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
    session: AsyncSession, entry: WaitingRoomEntry, status: str, *, refresh_timestamp: bool = False
) -> WaitingRoomEntry:
    entry.status = status
    if refresh_timestamp:
        entry.requested_at = utc_now()
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


async def get_latest_row_hash(session: AsyncSession) -> str | None:
    """Most recent row_hash by id (id is monotonic and race-safer than
    timestamp for this purpose — two inserts in the same instant would
    otherwise be ambiguous to order)."""
    stmt = select(AuditEvent.row_hash).order_by(AuditEvent.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_audit_event(
    session: AsyncSession, event_type: str, **details: Any
) -> AuditEvent:
    consultation_id = details.pop("consultation_id", None)
    cleaned_details = {
        key: _clean_audit_value(value)
        for key, value in details.items()
        if value is not None
    }
    cleaned_details = cleaned_details or None

    timestamp = utc_now()  # set explicitly (overrides server_default) so the
                            # hash can include the exact value being stored,
                            # not a value the DB assigns after the fact

    prev_hash = await get_latest_row_hash(session)
    # Hash over PLAINTEXT content, not ciphertext — EncryptedJSON encrypts
    # with a random nonce per call, so hashing post-encryption bytes would
    # make the same content hash differently every time and the chain
    # unverifiable against actual content.
    payload = "|".join([
        event_type,
        consultation_id or "",
        timestamp.isoformat(),
        json.dumps(cleaned_details, sort_keys=True, default=str),
    ])
    row_hash = hashlib.sha256(f"{prev_hash or ''}{payload}".encode()).hexdigest()

    event = AuditEvent(
        event_type=event_type,
        consultation_id=consultation_id,
        details=cleaned_details,
        timestamp=timestamp,
        prev_row_hash=prev_hash,
        row_hash=row_hash,
    )
    session.add(event)
    await session.flush()
    return event

# TODO(pre-production): anchor row_hash checkpoints to external storage
# (e.g. OVHcloud Object Storage with Object Lock) on a schedule, using a
# credential separate from the app's runtime DB role, so tampering by a
# fully privileged DB actor is detectable — not just tampering by the app
# role. Without this, someone with elevated DB access could still rewrite
# the entire chain forward after altering one row, since the chain alone
# only proves internal consistency, not consistency with anything outside
# the database.

# TODO(concurrency): get_latest_row_hash + insert is a read-then-write
# without row locking. Concurrent audit writes (e.g. two routes firing
# audit events in the same instant) could both read the same prev_hash and
# produce two rows claiming the same predecessor, forking the chain rather
# than corrupting it outright. Acceptable for current single-node
# dev/low-concurrency use; revisit (e.g. SELECT ... FOR UPDATE on a
# sentinel row, or a Postgres advisory lock) before production traffic.


async def list_audit_events_for_consultation(
    session: AsyncSession,
    consultation_id: str,
    *,
    limit: int = 50,
    cursor: int | None = None,
) -> list[AuditEvent]:
    """Most-recent-first, keyset-paginated by id. `cursor` is the id of the
    last event from the previous page; pass None for the first page."""
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.consultation_id == consultation_id)
        .order_by(AuditEvent.id.desc())
        .limit(limit)
    )
    if cursor is not None:
        stmt = stmt.where(AuditEvent.id < cursor)
    return list((await session.execute(stmt)).scalars().all())


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


async def get_active_consultations(session: AsyncSession) -> list[Consultation]:
    """Return all consultations with status='active'."""
    stmt = select(Consultation).where(Consultation.status == "active")
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# Chat messages
# --------------------------------------------------------------------------

async def create_chat_message(
    session: AsyncSession,
    *,
    consultation_id: str,
    sender_identity: str,
    sender_name: str,
    sender_role: str,
    body: str,
    client_message_id: str | None = None,
) -> ChatMessage:
    if client_message_id is None:
        # No idempotency key supplied (e.g. legacy caller) — plain insert,
        # unchanged from before this task.
        message = ChatMessage(
            consultation_id=consultation_id,
            sender_identity=sender_identity,
            sender_name=sender_name,
            sender_role=sender_role,
            body=body,
            client_message_id=None,
        )
        session.add(message)
        await session.flush()
        return message

    # Idempotent path: ON CONFLICT DO NOTHING against the partial unique
    # index on (consultation_id, client_message_id) from migration 0009.
    # This is atomic at the DB level — correct even if two requests with
    # the same client_message_id arrive concurrently, which a
    # check-then-insert in application code can never guarantee (there's
    # always a race window between the check and the insert).
    #
    # ASSUMPTION: a retry is assumed to carry the same body as the
    # original send under the same client_message_id. If a duplicate ID
    # ever arrives with different content, this returns the ORIGINAL
    # row's content, not the new one — that's the correct behavior for a
    # genuine retry, not a bug.
    stmt = (
        pg_insert(ChatMessage)
        .values(
            consultation_id=consultation_id,
            sender_identity=sender_identity,
            sender_name=sender_name,
            sender_role=sender_role,
            body=body,
            client_message_id=client_message_id,
        )
        .on_conflict_do_nothing(
            index_elements=["consultation_id", "client_message_id"],
            # Must match migration 0009's postgresql_where predicate
            # exactly (textually), or Postgres won't recognize this as
            # the same conflict target and the statement will error.
            index_where=sa.text("client_message_id IS NOT NULL"),
        )
        .returning(ChatMessage)
    )
    result = await session.execute(stmt)
    inserted = result.scalar_one_or_none()
    await session.flush()

    if inserted is not None:
        return inserted

    # Conflict: a message with this client_message_id already exists for
    # this consultation (a retry of an already-successful send). Return
    # the existing row rather than raising — same request, same response,
    # no duplicate, no crash.
    existing_stmt = select(ChatMessage).where(
        ChatMessage.consultation_id == consultation_id,
        ChatMessage.client_message_id == client_message_id,
    )
    return (await session.execute(existing_stmt)).scalar_one()


async def list_chat_messages(session: AsyncSession, consultation_id: str) -> list[ChatMessage]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.consultation_id == consultation_id)
        .order_by(ChatMessage.sent_at.asc(), ChatMessage.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())