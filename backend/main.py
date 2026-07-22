import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from livekit import api
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import crud
from database import AsyncSessionLocal, get_db
from models import Consultation, WaitingRoomEntry as WaitingRoomEntryModel

load_dotenv()

TOKEN_TTL_SECONDS = 2 * 60
DEPARTURE_TIMEOUT = 120
CONSULTATION_TTL_MINUTES = 60
AUDIT_EVENTS_LIMIT = 200
TRACK_TYPE_LABELS = {
    0: "audio",
    1: "video",
}
TRACK_SOURCE_LABELS = {
    0: "unknown",
    1: "camera",
    2: "microphone",
    3: "screen_share",
    4: "screen_share_audio",
}
TRACK_ENCRYPTION_LABELS = {
    0: "none",
    1: "gcm",
    2: "custom",
}

logger = logging.getLogger(__name__)

Role = Literal["doctor", "patient", "observer"]
ConsultationStatus = Literal["active", "ended"]
WaitingRoomStatus = Literal["waiting", "admitted", "denied"]


async def background_sync_loop():
    """Background task that periodically syncs consultations with LiveKit.
    
    This runs every 5 minutes and ensures the database stays synchronized with
    the actual LiveKit room state, handling expirations and missing rooms.
    """
    while True:
        try:
            async with AsyncSessionLocal() as session:
                try:
                    result = await sync_consultations_with_livekit(session)
                    logger.info(
                        "Background sync completed: total_active=%d expired=%d failed=%d",
                        result["total_active"],
                        result["expired"],
                        result["failed"],
                    )
                    if result["errors"]:
                        logger.warning("Background sync errors: %s", result["errors"])
                except Exception as e:
                    logger.exception("Background sync failed: %s", str(e))
                    await session.rollback()
        except Exception as e:
            logger.exception("Background sync session creation failed: %s", str(e))
        
        # Sleep for 5 minutes before next sync
        await asyncio.sleep(5 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage background tasks for the FastAPI application."""
    # Startup: start the background sync task
    sync_task = asyncio.create_task(background_sync_loop())
    try:
        yield
    finally:
        # Shutdown: cancel the background task
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Tachafy Teleconsultation Demo", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateConsultationRequest(BaseModel):
    doctor_name: str = Field(default="Doctor", min_length=1, max_length=80)
    patient_name: str = Field(default="Patient", min_length=1, max_length=80)


class CreateConsultationResponse(BaseModel):
    consultation_id: str
    room_name: str
    expires_at: str
    token_ttl_seconds: int
    status: ConsultationStatus
    ended_at: str | None


class ValidateJoinRequest(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Role


class ValidateJoinResponse(BaseModel):
    consultation_id: str
    room_name: str
    participant_name: str
    role: Role
    expires_at: str
    status: ConsultationStatus


class TokenRequest(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Role


class TokenResponse(BaseModel):
    token: str
    consultation_id: str
    room_name: str
    participant_name: str
    role: Role
    expires_in_seconds: int
    e2ee_key: str


class EndConsultationRequest(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Role


class EndConsultationResponse(BaseModel):
    consultation_id: str
    room_name: str
    status: ConsultationStatus
    ended_at: str
    ended_by: str


class WaitingRoomRequestPayload(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Role


class WaitingRoomEntry(BaseModel):
    participant_name: str
    role: Role
    status: WaitingRoomStatus
    requested_at: str


class WaitingRoomActionPayload(BaseModel):
    actor_name: str = Field(min_length=1, max_length=80)
    actor_role: Role


class ModerationActionPayload(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Role


class LockConsultationResponse(BaseModel):
    consultation_id: str
    locked: bool


class ModerationActionResponse(BaseModel):
    status: str
    tracks_muted: int | None = None


class SendChatMessagePayload(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Literal["doctor", "patient", "observer"]
    body: str = Field(min_length=1, max_length=2000)


class ChatMessageResponse(BaseModel):
    sender_identity: str
    sender_name: str
    sender_role: str
    body: str
    sent_at: str


class ParticipantInfo(BaseModel):
    participant_id: str
    identity: str
    role: str | None
    name: str | None
    tag: str | None = None
    state: str | None
    joined_at: str | None
    metadata: dict[str, Any] | None = None
    is_publisher: bool | None
    tracks: list[dict[str, Any]] | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _proto_message_to_dict(proto: Any) -> dict[str, Any] | None:
    if not isinstance(proto, Message):
        return None
    return MessageToDict(
        proto,
        preserving_proto_field_name=True,
        use_integers_for_enums=False,
    )


def _is_room_termination_event(event_type: str) -> bool:
    normalized = (event_type or "").lower()
    return normalized.startswith("room.") and any(
        keyword in normalized
        for keyword in ("ended", "closed", "destroyed", "expired", "finished")
    )


def parse_livekit_identity(identity: str) -> tuple[str | None, str | None, str | None]:
    # LiveKit identities are expected to look like role:name:randomsuffix.
    # A participant name that contains ":" will misparse with this format.
    if not identity:
        return None, None, None

    parts = identity.split(":")
    if len(parts) >= 3:
        return parts[0], ":".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return None, identity, None


def _build_participant_track_data(tracks: Any) -> list[dict[str, Any]] | None:
    if not tracks:
        return None

    return [
        {
            "track_id": getattr(track, "track_id", None),
            "name": getattr(track, "name", None),
            "type": getattr(track, "type", None),
            "source": getattr(track, "source", None),
            "muted": getattr(track, "muted", None),
            "simulcast": getattr(track, "simulcast", None),
            "metadata": getattr(track, "metadata", None),
        }
        for track in tracks
        if track is not None
    ]


def _build_participant_audit_data(
    *,
    participant_id: Any,
    identity: Any,
    role: Any,
    name: Any,
    tag: Any,
    state: Any,
    joined_at: Any,
    metadata: Any,
    is_publisher: Any,
    tracks: Any = None,
) -> dict[str, Any]:
    participant_data: dict[str, Any] = {
        "participant_id": participant_id,
        "identity": identity,
        "role": role,
        "name": name,
        "tag": tag,
        "state": state,
        "joined_at": joined_at,
        "metadata": metadata,
        "is_publisher": is_publisher,
    }

    if tracks is not None:
        participant_data["tracks"] = tracks

    return {k: v for k, v in participant_data.items() if v is not None}


def _parse_room_metadata(metadata: str | None) -> Any:
    if not metadata:
        return None

    try:
        parsed = json.loads(metadata)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_room_audit_data(room: Any) -> dict[str, Any] | None:
    if room is None:
        return None

    room_data = _proto_message_to_dict(room)
    if room_data is None:
        return None

    if "sid" in room_data:
        room_data["room_id"] = room_data.pop("sid")
    elif "id" in room_data:
        room_data["room_id"] = room_data.pop("id")

    if "metadata" in room_data:
        room_data["metadata"] = _parse_room_metadata(room_data["metadata"])

    return room_data


def _build_participant_audit_data_from_participant(participant: Any) -> dict[str, Any] | None:
    if participant is None:
        return None

    if isinstance(participant, Message):
        participant_dict = _proto_message_to_dict(participant)
        if participant_dict is None:
            return None

        identity = participant_dict.get("identity")
        role, participant_name, participant_tag = parse_livekit_identity(identity)
        return _build_participant_audit_data(
            participant_id=participant_dict.get("sid") or participant_dict.get("id"),
            identity=identity,
            role=role,
            name=participant_name or participant_dict.get("name"),
            tag=participant_tag,
            state=participant_dict.get("state"),
            joined_at=participant_dict.get("joined_at"),
            metadata=_parse_room_metadata(participant_dict.get("metadata")),
            is_publisher=participant_dict.get("is_publisher"),
            tracks=participant_dict.get("tracks") if "tracks" in participant_dict else None,
        )

    identity = getattr(participant, "identity", None)
    role, participant_name, participant_tag = parse_livekit_identity(identity)

    raw_state = getattr(participant, "state", None)
    return _build_participant_audit_data(
        participant_id=getattr(participant, "sid", None) or getattr(participant, "id", None),
        identity=identity,
        role=role,
        name=participant_name or getattr(participant, "name", None),
        tag=participant_tag,
        state=raw_state,
        joined_at=getattr(participant, "joined_at", None),
        metadata=_parse_room_metadata(getattr(participant, "metadata", None)),
        is_publisher=getattr(participant, "is_publisher", None),
        tracks=_build_participant_track_data(getattr(participant, "tracks", None)),
    )


async def _verify_and_parse_webhook(
    session: AsyncSession, request: Request, authorization: str | None
) -> Any | None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")

    api_key, api_secret = require_livekit_credentials()
    token_verifier = api.TokenVerifier(api_key=api_key, api_secret=api_secret)
    webhook_receiver = api.WebhookReceiver(token_verifier)

    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    try:
        event = webhook_receiver.receive(body_str, authorization)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_id = getattr(event, "id", None)
    if isinstance(event_id, str):
        is_new = await crud.remember_webhook_event_id(session, event_id)
        if not is_new:
            return None

    return event


async def _resolve_consultation_from_event(
    session: AsyncSession, event: Any
) -> Consultation | None:
    room = getattr(event, "room", None)
    room_data = _build_room_audit_data(room)
    room_name = None
    if room_data is not None:
        room_name = room_data.get("room_name") or room_data.get("name")

    if room_name:
        consultation = await crud.find_consultation_by_room_name(session, room_name)
        if consultation is not None:
            return consultation

    room_metadata = room_data.get("metadata") if room_data else None
    if room_metadata is not None:
        return await crud.find_consultation_by_room_metadata(session, room_metadata)

    return None


async def _handle_termination_if_applicable(
    session: AsyncSession, event: Any, consultation: Consultation | None
) -> None:
    # Webhook room termination events are now expected ephemeral behavior
    # and must not kill the database record. This function is a no-op.
    pass


def _build_track_labels(track: Any) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if track is None:
        return None, None, None

    raw_track_type = getattr(track, "type", None)
    raw_track_source = getattr(track, "source", None)
    raw_encryption = getattr(track, "encryption", None)

    track_type_label = None
    track_source_label = None
    encryption_label = None

    if isinstance(raw_track_type, int):
        track_type_label = TRACK_TYPE_LABELS.get(raw_track_type)
    elif raw_track_type is not None:
        track_type_label = str(raw_track_type).lower()

    if isinstance(raw_track_source, int):
        track_source_label = TRACK_SOURCE_LABELS.get(raw_track_source)
    elif raw_track_source is not None:
        track_source_label = str(raw_track_source).lower()

    if isinstance(raw_encryption, int):
        encryption_label = TRACK_ENCRYPTION_LABELS.get(raw_encryption)
    elif raw_encryption is not None:
        encryption_label = str(raw_encryption).lower()

    return track_type_label, track_source_label, {
        "type": track_type_label,
        "source": track_source_label,
        "encryption": encryption_label,
    }


async def _record_webhook_audit(
    session: AsyncSession, event: Any, consultation: Consultation | None
) -> None:
    event_type = getattr(event, "event", "unknown")
    event_label = f"livekit.{event_type}"

    room = getattr(event, "room", None)
    room_data = _build_room_audit_data(room)
    participant = getattr(event, "participant", None)
    has_participant = _event_field_present(event, "participant", participant)
    track = getattr(event, "track", None)
    has_track = _event_field_present(event, "track", track)

    participant_data = None
    if has_participant and participant is not None:
        participant_data = _build_participant_audit_data_from_participant(participant)

    track_info = None
    track_type_label = None
    track_source_label = None
    if has_track and track is not None:
        track_type_label, track_source_label, track_info = _build_track_labels(track)

    if track_type_label or track_source_label:
        label_parts = []
        if track_type_label:
            label_parts.append(track_type_label)
        if track_source_label:
            label_parts.append(track_source_label)
        event_label = f"{event_label} ({': '.join(label_parts)})"

    await crud.create_audit_event(
        session,
        event_label,
        consultation_id=consultation.consultation_id if consultation else None,
        room=room_data,
        participant=participant_data,
        track=track_info,
        source="webhook",
    )


def require_livekit_credentials() -> tuple[str, str]:
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="LiveKit credentials missing")

    return api_key, api_secret


def resolve_livekit_api_url() -> str:
    configured = (
        os.getenv("LIVEKIT_API_URL")
        or os.getenv("LIVEKIT_URL")
        or "http://localhost:7880"
    ).strip()

    normalized = configured
    if configured.startswith("ws://"):
        normalized = "http://" + configured[len("ws://"):]
    elif configured.startswith("wss://"):
        normalized = "https://" + configured[len("wss://"):]
    elif not configured.startswith("http://") and not configured.startswith("https://"):
        normalized = f"http://{configured}"

    return normalized.rstrip("/")


def ensure_role_allowed_for_consultation(
    consultation: Consultation,
    *,
    participant_name: str,
    role: Role,
) -> None:
    if role == "doctor" and participant_name != consultation.doctor_name:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "NOT_ASSIGNED_DOCTOR",
                "message": "Participant is not assigned as doctor",
            },
        )

    if role == "patient" and participant_name != consultation.patient_name:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "NOT_ASSIGNED_PATIENT",
                "message": "Participant is not assigned as patient",
            },
        )


def require_doctor_actor(consultation: Consultation, payload: EndConsultationRequest) -> None:
    if payload.role != "doctor":
        raise HTTPException(status_code=403, detail="Only doctor can end a consultation")

    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.participant_name,
        role=payload.role,
    )


def require_doctor_for_moderation(
    consultation: Consultation, participant_name: str, role: Role
) -> None:
    """Check that the actor is the assigned doctor for moderation actions."""
    if role != "doctor":
        raise HTTPException(status_code=403, detail="Only doctor can perform moderation actions")

    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=participant_name,
        role=role,
    )


def _event_field_present(event: Any, field_name: str, fallback_value: Any) -> bool:
    if hasattr(event, "HasField"):
        try:
            return event.HasField(field_name)
        except Exception:
            return fallback_value is not None

    return fallback_value is not None


async def terminate_room(room_name: str) -> None:
    livekit_api_url = resolve_livekit_api_url()

    try:
        async with api.LiveKitAPI(url=livekit_api_url) as lkapi:
            participants = await lkapi.room.list_participants(api.ListParticipantsRequest(room=room_name))
            for participant in participants.participants:
                await lkapi.room.remove_participant(
                    api.RoomParticipantIdentity(room=room_name, identity=participant.identity)
                )

            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:
        logger.exception(
            "Failed to terminate LiveKit room: room_name=%s livekit_api_url=%s",
            room_name,
            livekit_api_url,
        )


async def sync_consultations_with_livekit(session: AsyncSession) -> dict[str, Any]:
    """Synchronize database consultation status with actual LiveKit room state.
    
    This function handles hard expirations: if expires_at < utc_now(), terminates the room
    and marks as ended. Missing rooms from LiveKit are not considered errors since rooms
    spin down dynamically and can be JIT provisioned on join.
    
    Returns a summary of the synchronization results.
    """
    active_consultations = await crud.get_active_consultations(session)
    
    expired_count = 0
    failed_count = 0
    errors = []
    
    now = utc_now()
    
    # Handle hard expirations
    expired_consultations = [c for c in active_consultations if c.expires_at < now]
    
    for consultation in expired_consultations:
        room_name = consultation.room_name
        try:
            # Terminate the room in LiveKit
            await terminate_room(room_name)
            
            # Mark as ended in database
            await crud.mark_consultation_ended_by_system(session, consultation)
            expired_count += 1
            
            await crud.create_audit_event(
                session,
                "consultation.expired_terminated",
                consultation_id=consultation.consultation_id,
                room_name=room_name,
                reason="hard_expiration",
            )
            
            logger.info(
                "Expired and terminated consultation: consultation_id=%s room_name=%s",
                consultation.consultation_id,
                room_name,
            )
        except Exception as e:
            failed_count += 1
            error_msg = f"Failed to expire consultation {room_name}: {str(e)}"
            errors.append(error_msg)
            logger.exception(
                "Failed to expire consultation: consultation_id=%s room_name=%s",
                consultation.consultation_id,
                room_name,
            )
    
    await session.commit()
    
    return {
        "total_active": len(active_consultations),
        "expired": expired_count,
        "failed": failed_count,
        "errors": errors,
    }


async def _is_doctor_in_room(room_name: str) -> bool:
    """Check whether any participant with a doctor identity is in the room."""
    livekit_api_url = resolve_livekit_api_url()
    try:
        async with api.LiveKitAPI(url=livekit_api_url) as lkapi:
            participants = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            for participant in participants.participants:
                role, _name, _tag = parse_livekit_identity(participant.identity)
                if role == "doctor":
                    return True
    except Exception:
        logger.exception(
            "Failed to list participants for doctor check: room_name=%s",
            room_name,
        )
    return False


def grants_for(role: Role, room_name: str) -> api.VideoGrants:
    if role == "observer":
        return api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=False,
            hidden=True,  # Reverted from False to match original behavior
        )

    return api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_update_own_metadata=True,
    )


async def ensure_room_active_for_consultation(
    session: AsyncSession, consultation: Consultation
) -> None:
    room_name = consultation.room_name
    livekit_api_url = resolve_livekit_api_url()

    try:
        async with api.LiveKitAPI(url=livekit_api_url) as lkapi:
            response = await lkapi.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
            if not getattr(response, "rooms", []):
                # Room doesn't exist, dynamically recreate it with JIT provisioning
                room_metadata = json.dumps(
                    {
                        "consultation_id": consultation.consultation_id,
                        "doctor_name": consultation.doctor_name,
                        "patient_name": consultation.patient_name,
                    },
                    separators=(",", ":"),
                )
                
                await lkapi.room.create_room(
                    api.CreateRoomRequest(
                        name=room_name,
                        empty_timeout=600,
                        departure_timeout=120,
                        metadata=room_metadata,
                    )
                )
                
                await crud.create_audit_event(
                    session,
                    "consultation.room_jit_created",
                    consultation_id=consultation.consultation_id,
                    room_name=room_name,
                )
                
                logger.info(
                    "JIT provisioned room: consultation_id=%s room_name=%s",
                    consultation.consultation_id,
                    room_name,
                )
    except Exception:
        await crud.create_audit_event(
            session,
            "consultation.room_provisioning_failed",
            consultation_id=consultation.consultation_id,
            room_name=room_name,
        )
        logger.exception(
            "Failed to ensure room is active: consultation_id=%s room_name=%s",
            consultation.consultation_id,
            room_name,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to ensure room is active",
        )


def build_token(
    *,
    consultation_id: str,
    room_name: str,
    participant_name: str,
    role: Role,
) -> str:
    require_livekit_credentials()

    metadata = json.dumps(
        {
            "consultation_id": consultation_id,
            "role": role,
        },
        separators=(",", ":"),
    )

    return (
        api.AccessToken()
        .with_identity(f"{role}:{participant_name}:{secrets.token_hex(4)}")
        .with_name(participant_name)
        .with_metadata(metadata)
        .with_ttl(timedelta(seconds=TOKEN_TTL_SECONDS))
        .with_grants(grants_for(role, room_name))
        .to_jwt()
    )


def _build_consultation_ended_response(consultation: Consultation) -> EndConsultationResponse:
    return EndConsultationResponse(
        consultation_id=consultation.consultation_id,
        room_name=consultation.room_name,
        status="ended",
        ended_at=consultation.ended_at.isoformat(),
        ended_by=consultation.ended_by,
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    health_status: dict[str, Any] = {
        "api": "ok",
        "livekit": "unknown",
        "livekit_api_url": resolve_livekit_api_url(),
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(health_status["livekit_api_url"], timeout=2.0)
            health_status["livekit"] = "online" if response.status_code == 200 else "degraded"
            health_status["livekit_http_status"] = response.status_code
    except httpx.RequestError:
        health_status["livekit"] = "offline"

    return health_status


@app.post("/api/consultations", response_model=CreateConsultationResponse)
async def create_consultation(
    payload: CreateConsultationRequest,
    session: AsyncSession = Depends(get_db),
) -> CreateConsultationResponse:
    require_livekit_credentials()

    consultation_id = secrets.token_urlsafe(12)
    room_name = f"tachafy-{consultation_id}"
    e2ee_key = secrets.token_urlsafe(32)
    expires_at = utc_now() + timedelta(minutes=CONSULTATION_TTL_MINUTES)

    await crud.create_consultation(
        session,
        consultation_id=consultation_id,
        room_name=room_name,
        doctor_name=payload.doctor_name,
        patient_name=payload.patient_name,
        e2ee_key=e2ee_key,
        expires_at=expires_at,
    )

    await crud.create_audit_event(
        session,
        "consultation.created",
        consultation_id=consultation_id,
        room_name=room_name,
        doctor_name=payload.doctor_name,
        patient_name=payload.patient_name,
    )

    return CreateConsultationResponse(
        consultation_id=consultation_id,
        room_name=room_name,
        expires_at=expires_at.isoformat(),
        token_ttl_seconds=TOKEN_TTL_SECONDS,
        status="active",
        ended_at=None,
    )


@app.post("/api/consultations/{consultation_id}/validate", response_model=ValidateJoinResponse)
async def validate_consultation_join(
    consultation_id: str,
    payload: ValidateJoinRequest,
    session: AsyncSession = Depends(get_db),
) -> ValidateJoinResponse:
    """Upfront check for the prejoin step: confirms the consultation exists,
    hasn't ended/expired, and the participant/role pair is allowed to join.
    Does not mint a LiveKit token or touch the LiveKit API."""
    consultation = await crud.get_consultation_or_404(session, consultation_id)
    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    return ValidateJoinResponse(
        consultation_id=consultation_id,
        room_name=consultation.room_name,
        participant_name=payload.participant_name,
        role=payload.role,
        expires_at=consultation.expires_at.isoformat(),
        status=consultation.status,
    )


@app.post("/api/consultations/{consultation_id}/waiting-room/request", response_model=WaitingRoomEntry)
async def request_waiting_room(
    consultation_id: str,
    payload: WaitingRoomRequestPayload,
    session: AsyncSession = Depends(get_db),
) -> WaitingRoomEntry:
    """Non-doctor participants request access to the consultation waiting room."""
    consultation = await crud.get_consultation_or_404(session, consultation_id)

    if payload.role == "doctor":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "DOCTOR_BYPASS",
                "message": "Doctors do not use the waiting room.",
            },
        )

    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    # If participant already has an entry, return its current state.
    existing = await crud.get_waiting_room_entry(
        session, consultation_id, payload.participant_name
    )
    if existing is not None:
        return WaitingRoomEntry(
            participant_name=payload.participant_name,
            role=existing.role,
            status=existing.status,
            requested_at=existing.requested_at.isoformat(),
        )

    # Check if a doctor is already an active participant in the room.
    doctor_present = await _is_doctor_in_room(consultation.room_name)
    status: WaitingRoomStatus = "admitted" if doctor_present else "waiting"

    entry = await crud.create_waiting_room_entry(
        session,
        consultation_id=consultation_id,
        participant_name=payload.participant_name,
        role=payload.role,
        status=status,
    )

    await crud.create_audit_event(
        session,
        "waiting_room.requested",
        consultation_id=consultation_id,
        participant_name=payload.participant_name,
        role=payload.role,
        status=status,
    )

    if status == "admitted":
        await crud.create_audit_event(
            session,
            "waiting_room.admitted",
            consultation_id=consultation_id,
            participant_name=payload.participant_name,
            role=payload.role,
            auto=True,
        )

    return WaitingRoomEntry(
        participant_name=payload.participant_name,
        role=payload.role,
        status=status,
        requested_at=entry.requested_at.isoformat(),
    )


@app.get("/api/consultations/{consultation_id}/waiting-room", response_model=list[WaitingRoomEntry])
async def list_waiting_room(
    consultation_id: str,
    session: AsyncSession = Depends(get_db),
) -> list[WaitingRoomEntry]:
    """Return all pending (waiting) entries — used by the doctor to poll."""
    await crud.get_consultation_or_404(session, consultation_id)
    entries = await crud.list_waiting_entries(session, consultation_id, status="waiting")

    return [
        WaitingRoomEntry(
            participant_name=entry.participant_name,
            role=entry.role,
            status=entry.status,
            requested_at=entry.requested_at.isoformat(),
        )
        for entry in entries
    ]


@app.post("/api/consultations/{consultation_id}/waiting-room/{participant_name}/admit", response_model=WaitingRoomEntry)
async def admit_participant(
    consultation_id: str,
    participant_name: str,
    payload: WaitingRoomActionPayload,
    session: AsyncSession = Depends(get_db),
) -> WaitingRoomEntry:
    """Doctor admits a waiting participant."""
    consultation = await crud.get_consultation_or_404(session, consultation_id)

    if payload.actor_role != "doctor":
        raise HTTPException(status_code=403, detail="Only doctor can admit participants")

    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.actor_name,
        role=payload.actor_role,
    )

    entry = await crud.get_waiting_room_entry(
        session, consultation_id, participant_name, for_update=True
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Participant not found in waiting room")

    await crud.set_waiting_room_status(session, entry, "admitted")

    await crud.create_audit_event(
        session,
        "waiting_room.admitted",
        consultation_id=consultation_id,
        participant_name=participant_name,
        role=entry.role,
        admitted_by=payload.actor_name,
    )

    return WaitingRoomEntry(
        participant_name=participant_name,
        role=entry.role,
        status="admitted",
        requested_at=entry.requested_at.isoformat(),
    )


@app.post("/api/consultations/{consultation_id}/waiting-room/{participant_name}/deny", response_model=WaitingRoomEntry)
async def deny_participant(
    consultation_id: str,
    participant_name: str,
    payload: WaitingRoomActionPayload,
    session: AsyncSession = Depends(get_db),
) -> WaitingRoomEntry:
    """Doctor denies a waiting participant."""
    consultation = await crud.get_consultation_or_404(session, consultation_id)

    if payload.actor_role != "doctor":
        raise HTTPException(status_code=403, detail="Only doctor can deny participants")

    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.actor_name,
        role=payload.actor_role,
    )

    entry = await crud.get_waiting_room_entry(
        session, consultation_id, participant_name, for_update=True
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Participant not found in waiting room")

    await crud.set_waiting_room_status(session, entry, "denied")

    await crud.create_audit_event(
        session,
        "waiting_room.denied",
        consultation_id=consultation_id,
        participant_name=participant_name,
        role=entry.role,
        denied_by=payload.actor_name,
    )

    return WaitingRoomEntry(
        participant_name=participant_name,
        role=entry.role,
        status="denied",
        requested_at=entry.requested_at.isoformat(),
    )


@app.post("/api/consultations/{consultation_id}/token", response_model=TokenResponse)
async def create_consultation_token(
    consultation_id: str,
    payload: TokenRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    consultation = await crud.get_consultation_or_404(session, consultation_id)
    room_name = consultation.room_name
    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    # Gate: non-doctor participants must be admitted via the waiting room.
    if payload.role != "doctor":
        wr_entry = await crud.get_waiting_room_entry(
            session, consultation_id, payload.participant_name
        )
        if wr_entry is None or wr_entry.status != "admitted":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "NOT_ADMITTED",
                    "message": "You have not been admitted to this consultation. Please request access via the waiting room.",
                },
            )

    # Gate: if consultation is locked, only doctors can join
    if consultation.locked and payload.role != "doctor":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ROOM_LOCKED",
                "message": "The consultation room is locked. Only the doctor can join at this time.",
            },
        )

    await ensure_room_active_for_consultation(session, consultation)

    token = build_token(
        consultation_id=consultation_id,
        room_name=room_name,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    await crud.create_audit_event(
        session,
        "token.issued",
        consultation_id=consultation_id,
        room_name=room_name,
        participant_name=payload.participant_name,
        role=payload.role,
        token_ttl_seconds=TOKEN_TTL_SECONDS,
    )

    return TokenResponse(
        token=token,
        consultation_id=consultation_id,
        room_name=room_name,
        participant_name=payload.participant_name,
        role=payload.role,
        expires_in_seconds=TOKEN_TTL_SECONDS,
        e2ee_key=consultation.e2ee_key,
    )


@app.post(
    "/api/consultations/{consultation_id}/end",
    response_model=EndConsultationResponse,
)
async def end_consultation(
    consultation_id: str,
    payload: EndConsultationRequest,
    session: AsyncSession = Depends(get_db),
) -> EndConsultationResponse:
    consultation = await crud.get_consultation_or_404(
        session, consultation_id, include_ended=True, for_update=True
    )
    require_doctor_actor(consultation, payload)

    if consultation.status == "ended":
        return _build_consultation_ended_response(consultation)

    ended_at = await crud.set_consultation_ended_state(
        session,
        consultation,
        ended_by=payload.participant_name,
    )

    if ended_at is None:
        return _build_consultation_ended_response(consultation)

    await terminate_room(consultation.room_name)

    await crud.create_audit_event(
        session,
        "consultation.ended",
        consultation_id=consultation_id,
        room_name=consultation.room_name,
        ended_by=payload.participant_name,
    )

    return _build_consultation_ended_response(consultation)


@app.post(
    "/api/consultations/{consultation_id}/lock",
    response_model=LockConsultationResponse,
)
async def lock_consultation(
    consultation_id: str,
    payload: ModerationActionPayload,
    session: AsyncSession = Depends(get_db),
) -> LockConsultationResponse:
    consultation = await crud.get_consultation_or_404(
        session, consultation_id, for_update=True
    )
    require_doctor_for_moderation(consultation, payload.participant_name, payload.role)

    changed = await crud.set_consultation_locked(session, consultation, locked=True)
    if changed:
        await crud.create_audit_event(
            session,
            "consultation.locked",
            consultation_id=consultation_id,
            room_name=consultation.room_name,
            locked_by=payload.participant_name,
        )

    return LockConsultationResponse(
        consultation_id=consultation_id,
        locked=True,
    )


@app.post(
    "/api/consultations/{consultation_id}/unlock",
    response_model=LockConsultationResponse,
)
async def unlock_consultation(
    consultation_id: str,
    payload: ModerationActionPayload,
    session: AsyncSession = Depends(get_db),
) -> LockConsultationResponse:
    consultation = await crud.get_consultation_or_404(
        session, consultation_id, for_update=True
    )
    require_doctor_for_moderation(consultation, payload.participant_name, payload.role)

    changed = await crud.set_consultation_locked(session, consultation, locked=False)
    if changed:
        await crud.create_audit_event(
            session,
            "consultation.unlocked",
            consultation_id=consultation_id,
            room_name=consultation.room_name,
            unlocked_by=payload.participant_name,
        )

    return LockConsultationResponse(
        consultation_id=consultation_id,
        locked=False,
    )


@app.post(
    "/api/consultations/{consultation_id}/participants",
    response_model=list[ParticipantInfo],
)
async def list_participants(
    consultation_id: str,
    payload: ModerationActionPayload,
    session: AsyncSession = Depends(get_db),
) -> list[ParticipantInfo]:
    consultation = await crud.get_consultation_or_404(session, consultation_id)
    require_doctor_for_moderation(consultation, payload.participant_name, payload.role)

    livekit_api_url = resolve_livekit_api_url()
    try:
        async with api.LiveKitAPI(url=livekit_api_url) as lkapi:
            participants = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=consultation.room_name)
            )
            
            participant_infos = []
            for participant in participants.participants:
                participant_data = _build_participant_audit_data_from_participant(participant)
                if not participant_data:
                    continue
                try:
                    participant_infos.append(ParticipantInfo(**participant_data))
                except Exception:
                    logger.exception(
                        "Skipping malformed participant in list_participants: "
                        "consultation_id=%s identity=%s",
                        consultation_id,
                        participant_data.get("identity"),
                    )
                    continue

            return participant_infos
    except Exception as exc:
        logger.exception(
            "Failed to list participants: consultation_id=%s room_name=%s error=%s",
            consultation_id,
            consultation.room_name,
            exc,
        )
        raise HTTPException(status_code=500, detail="Unable to list participants")


@app.post(
    "/api/consultations/{consultation_id}/participants/{identity}/remove",
    response_model=ModerationActionResponse,
)
async def remove_participant(
    consultation_id: str,
    identity: str,
    payload: ModerationActionPayload,
    session: AsyncSession = Depends(get_db),
) -> ModerationActionResponse:
    consultation = await crud.get_consultation_or_404(
        session, consultation_id, for_update=True
    )
    require_doctor_for_moderation(consultation, payload.participant_name, payload.role)

    livekit_api_url = resolve_livekit_api_url()
    try:
        async with api.LiveKitAPI(url=livekit_api_url) as lkapi:
            await lkapi.room.remove_participant(
                api.RoomParticipantIdentity(room=consultation.room_name, identity=identity)
            )

        await crud.create_audit_event(
            session,
            "participant.removed_by_host",
            consultation_id=consultation_id,
            room_name=consultation.room_name,
            removed_by=payload.participant_name,
            target_identity=identity,
        )

        return ModerationActionResponse(status="removed")
    except Exception:
        logger.exception(
            "Failed to remove participant: consultation_id=%s identity=%s",
            consultation_id,
            identity,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to remove participant",
        )


@app.post(
    "/api/consultations/{consultation_id}/participants/{identity}/mute",
    response_model=ModerationActionResponse,
)
async def mute_participant(
    consultation_id: str,
    identity: str,
    payload: ModerationActionPayload,
    session: AsyncSession = Depends(get_db),
) -> ModerationActionResponse:
    consultation = await crud.get_consultation_or_404(
        session, consultation_id, for_update=True
    )
    require_doctor_for_moderation(consultation, payload.participant_name, payload.role)

    livekit_api_url = resolve_livekit_api_url()
    try:
        async with api.LiveKitAPI(url=livekit_api_url) as lkapi:
            # First, list participants to get the target participant's tracks
            participants = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=consultation.room_name)
            )

            target_participant = None
            for participant in participants.participants:
                if participant.identity == identity:
                    target_participant = participant
                    break

            if not target_participant:
                raise HTTPException(status_code=404, detail="Participant not found")

            # Mute all published tracks for the participant
            muted_count = 0
            if hasattr(target_participant, 'tracks') and target_participant.tracks:
                for track in target_participant.tracks:
                    try:
                        await lkapi.room.mute_published_track(
                            api.MuteRoomTrackRequest(
                                room=consultation.room_name,
                                identity=identity,
                                track_sid=track.sid,
                                muted=True,
                            )
                        )
                        muted_count += 1
                    except Exception:
                        logger.warning(
                            "Failed to mute track: consultation_id=%s identity=%s track_sid=%s",
                            consultation_id,
                            identity,
                            track.sid,
                        )

        await crud.create_audit_event(
            session,
            "participant.muted_by_host",
            consultation_id=consultation_id,
            room_name=consultation.room_name,
            muted_by=payload.participant_name,
            target_identity=identity,
            tracks_muted=muted_count,
        )

        return ModerationActionResponse(status="muted", tracks_muted=muted_count)
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to mute participant: consultation_id=%s identity=%s",
            consultation_id,
            identity,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to mute participant",
        )


@app.post("/api/consultations/{consultation_id}/chat")
async def send_chat_message(
    consultation_id: str,
    payload: SendChatMessagePayload,
    session: AsyncSession = Depends(get_db),
) -> ChatMessageResponse:
    consultation = await crud.get_consultation_or_404(session, consultation_id)
    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    message = await crud.create_chat_message(
        session,
        consultation_id=consultation_id,
        sender_identity=payload.participant_name,
        sender_name=payload.participant_name,
        sender_role=payload.role,
        body=payload.body,
    )

    return ChatMessageResponse(
        sender_identity=message.sender_identity,
        sender_name=message.sender_name,
        sender_role=message.sender_role,
        body=message.body,
        sent_at=message.sent_at.isoformat(),
    )


@app.get("/api/consultations/{consultation_id}/chat")
async def list_chat_messages(
    consultation_id: str,
    session: AsyncSession = Depends(get_db),
) -> list[ChatMessageResponse]:
    consultation = await crud.get_consultation_or_404(session, consultation_id, include_ended=True)
    messages = await crud.list_chat_messages(session, consultation_id)

    return [
        ChatMessageResponse(
            sender_identity=msg.sender_identity,
            sender_name=msg.sender_name,
            sender_role=msg.sender_role,
            body=msg.body,
            sent_at=msg.sent_at.isoformat(),
        )
        for msg in messages
    ]


@app.post("/api/webhooks")
async def livekit_webhook(
    request: Request,
    authorization: str = Header(None),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    event = await _verify_and_parse_webhook(session, request, authorization)
    if event is None:
        return {"status": "duplicate ignored"}

    consultation = await _resolve_consultation_from_event(session, event)
    await _handle_termination_if_applicable(session, event, consultation)
    await _record_webhook_audit(session, event, consultation)

    return {"status": "received"}


@app.get("/api/audit-events")
async def list_audit_events(session: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    events = await crud.list_audit_events(session, limit=AUDIT_EVENTS_LIMIT)

    serialized: list[dict[str, Any]] = []
    for event in events:
        payload: dict[str, Any] = {
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type,
        }
        if event.consultation_id is not None:
            payload["consultation_id"] = event.consultation_id
        if event.details:
            payload.update(event.details)
        serialized.append(payload)

    return serialized