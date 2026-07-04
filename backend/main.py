import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from google.protobuf.message import Message
from google.protobuf.json_format import MessageToDict
from livekit import api
from pydantic import BaseModel, Field

load_dotenv()

TOKEN_TTL_SECONDS = 2 * 60
CONSULTATION_TTL_MINUTES = 60
MAX_AUDIT_EVENTS = 200

Role = Literal["doctor", "patient", "observer"]
ConsultationStatus = Literal["active", "ended"]

app = FastAPI(title="Tachafy Teleconsultation Demo")

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

consultations: dict[str, dict[str, Any]] = {}
audit_events: list[dict[str, Any]] = []
processed_webhook_event_ids: set[str] = set()


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


class EndConsultationRequest(BaseModel):
    participant_name: str = Field(min_length=1, max_length=80)
    role: Role


class EndConsultationResponse(BaseModel):
    consultation_id: str
    room_name: str
    status: ConsultationStatus
    ended_at: str
    ended_by: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, sub_value in value.items():
            cleaned_value = _clean_audit_value(sub_value)
            if cleaned_value is not None:
                cleaned[key] = cleaned_value
        return cleaned or None
    if isinstance(value, list):
        cleaned = [_clean_audit_value(item) for item in value]
        cleaned = [item for item in cleaned if item is not None]
        return cleaned or None
    return value


def _proto_message_to_dict(proto: Any) -> dict[str, Any] | None:
    if not isinstance(proto, Message):
        return None
    return MessageToDict(
        proto,
        preserving_proto_field_name=True,
        use_integers_for_enums=False,
    )


def audit(event_type: str, **details: Any) -> None:
    cleaned_details = {
        key: _clean_audit_value(value)
        for key, value in details.items()
        if value is not None
    }
    cleaned_details = {
        key: value
        for key, value in cleaned_details.items()
        if value is not None
    }

    audit_events.append(
        {
            "timestamp": utc_now().isoformat(),
            "event_type": event_type,
            **cleaned_details,
        }
    )
    del audit_events[:-MAX_AUDIT_EVENTS]


def find_consultation_by_room_name(room_name: str) -> dict[str, Any] | None:
    for consultation in consultations.values():
        if consultation["room_name"] == room_name:
            return consultation
    return None


def parse_livekit_identity(identity: str) -> tuple[str | None, str | None, str | None]:
    if not identity:
        return None, None, None

    parts = identity.split(":")
    if len(parts) >= 3:
        return parts[0], ":".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return None, identity, None


def parse_room_metadata(metadata: str | None) -> Any:
    if not metadata:
        return None

    try:
        return json.loads(metadata)
    except ValueError:
        return metadata


def build_room_audit_data(room: Any) -> dict[str, Any] | None:
    if room is None:
        return None

    room_data = _proto_message_to_dict(room)
    if room_data is None:
        return None

    if "sid" in room_data:
        room_data["room_id"] = room_data.pop("sid")
    elif "id" in room_data:
        room_data["room_id"] = room_data.pop("id")

    if "name" in room_data:
        room_data["room_name"] = room_data["name"]

    if "metadata" in room_data:
        room_data["metadata"] = parse_room_metadata(room_data["metadata"])

    return room_data


def build_participant_audit_data(participant: Any) -> dict[str, Any] | None:
    if participant is None:
        return None

    participant_dict: dict[str, Any] | None = None
    if isinstance(participant, Message):
        participant_dict = _proto_message_to_dict(participant)

    if participant_dict is not None:
        identity = participant_dict.get("identity")
        role, participant_name, participant_tag = parse_livekit_identity(identity)

        participant_data: dict[str, Any] = {
            "participant_id": participant_dict.get("sid") or participant_dict.get("id"),
            "identity": identity,
            "role": role,
            "name": participant_name or participant_dict.get("name"),
            "tag": participant_tag,
            "state_name": participant_dict.get("state"),
            "joined_at": participant_dict.get("joined_at"),
            "metadata": parse_room_metadata(participant_dict.get("metadata")),
            "is_publisher": participant_dict.get("is_publisher"),
        }

        if "tracks" in participant_dict:
            participant_data["tracks"] = participant_dict["tracks"]

        return {k: v for k, v in participant_data.items() if v is not None}

    identity = getattr(participant, "identity", None)
    role, participant_name, participant_tag = parse_livekit_identity(identity)

    raw_state = getattr(participant, "state", None)
    participant_state = None
    if raw_state is not None:
        participant_state = {
            0: "JOINING",
            1: "JOINED",
            2: "ACTIVE",
            3: "DISCONNECTED",
        }.get(raw_state, str(raw_state))

    participant_data: dict[str, Any] = {
        "participant_id": getattr(participant, "sid", None) or getattr(participant, "id", None),
        "identity": identity,
        "role": role,
        "name": participant_name or getattr(participant, "name", None),
        "tag": participant_tag,
        "state": raw_state,
        "state_name": participant_state,
        "joined_at": getattr(participant, "joined_at", None),
        "metadata": parse_room_metadata(getattr(participant, "metadata", None)),
        "is_publisher": getattr(participant, "is_publisher", None),
    }

    tracks = getattr(participant, "tracks", None)
    if tracks:
        participant_data["tracks"] = [
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

    return {k: v for k, v in participant_data.items() if v is not None}


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


def get_consultation_or_404(
    consultation_id: str,
    *,
    include_ended: bool = False,
) -> dict[str, Any]:
    consultation = consultations.get(consultation_id)

    if consultation is None:
        raise HTTPException(status_code=404, detail="Consultation not found")

    if consultation["expires_at"] < utc_now():
        audit("consultation.expired", consultation_id=consultation_id)
        consultations.pop(consultation_id, None)
        raise HTTPException(status_code=410, detail="Consultation expired")

    if not include_ended and consultation.get("status") == "ended":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONSULTATION_ENDED",
                "message": "Consultation has ended",
            },
        )

    return consultation


def ensure_role_allowed_for_consultation(
    consultation: dict[str, Any],
    *,
    participant_name: str,
    role: Role,
) -> None:
    if role == "doctor" and participant_name != consultation["doctor_name"]:
        raise HTTPException(status_code=403, detail="Participant is not assigned as doctor")

    if role == "patient" and participant_name != consultation["patient_name"]:
        raise HTTPException(status_code=403, detail="Participant is not assigned as patient")


def require_doctor_actor(consultation: dict[str, Any], payload: EndConsultationRequest) -> None:
    if payload.role != "doctor":
        raise HTTPException(status_code=403, detail="Only doctor can end a consultation")

    if payload.participant_name != consultation["doctor_name"]:
        raise HTTPException(status_code=403, detail="Participant is not assigned as doctor")


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
    except Exception as exc:  # best effort cleanup to avoid blocking consultation closure
        audit(
            "consultation.room_termination_failed",
            room_name=room_name,
            livekit_api_url=livekit_api_url,
            error=str(exc),
        )


def grants_for(role: Role, room_name: str) -> api.VideoGrants:
    if role == "observer":
        return api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=False,
            hidden=True,
        )

    return api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_update_own_metadata=True,
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
async def create_consultation(payload: CreateConsultationRequest) -> CreateConsultationResponse:
    require_livekit_credentials()

    consultation_id = secrets.token_urlsafe(12)
    room_name = f"tachafy-{consultation_id}"
    expires_at = utc_now() + timedelta(minutes=CONSULTATION_TTL_MINUTES)

    consultations[consultation_id] = {
        "consultation_id": consultation_id,
        "room_name": room_name,
        "doctor_name": payload.doctor_name,
        "patient_name": payload.patient_name,
        "created_at": utc_now(),
        "expires_at": expires_at,
        "status": "active",
        "ended_at": None,
        "ended_by": None,
    }

    audit(
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


@app.post("/api/consultations/{consultation_id}/token", response_model=TokenResponse)
async def create_consultation_token(
    consultation_id: str,
    payload: TokenRequest,
) -> TokenResponse:
    consultation = get_consultation_or_404(consultation_id)
    room_name = consultation["room_name"]
    ensure_role_allowed_for_consultation(
        consultation,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    token = build_token(
        consultation_id=consultation_id,
        room_name=room_name,
        participant_name=payload.participant_name,
        role=payload.role,
    )

    audit(
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
    )


@app.post(
    "/api/consultations/{consultation_id}/end",
    response_model=EndConsultationResponse,
)
async def end_consultation(
    consultation_id: str,
    payload: EndConsultationRequest,
) -> EndConsultationResponse:
    consultation = get_consultation_or_404(consultation_id, include_ended=True)
    require_doctor_actor(consultation, payload)

    if consultation["status"] == "ended":
        return EndConsultationResponse(
            consultation_id=consultation_id,
            room_name=consultation["room_name"],
            status="ended",
            ended_at=consultation["ended_at"],
            ended_by=consultation["ended_by"],
        )

    ended_at = utc_now().isoformat()
    consultation["status"] = "ended"
    consultation["ended_at"] = ended_at
    consultation["ended_by"] = payload.participant_name

    await terminate_room(consultation["room_name"])

    audit(
        "consultation.ended",
        consultation_id=consultation_id,
        room_name=consultation["room_name"],
        ended_by=payload.participant_name,
    )

    return EndConsultationResponse(
        consultation_id=consultation_id,
        room_name=consultation["room_name"],
        status="ended",
        ended_at=ended_at,
        ended_by=payload.participant_name,
    )


# @app.post("/api/webhooks")
# async def livekit_webhook(request: Request) -> dict[str, str]:
#     body = await request.body()
#     audit(
#         "livekit.webhook.received",
#         content_type=request.headers.get("content-type"),
#         body_size=len(body),
#     )
#     return {"status": "received"}


@app.post("/api/webhooks")
async def livekit_webhook(
    request: Request,
    authorization: str = Header(None),
) -> dict[str, str]:
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
    if event_id is not None:
        if event_id in processed_webhook_event_ids:
            return {"status": "duplicate ignored"}
        processed_webhook_event_ids.add(event_id)

    event_type = getattr(event, "event", "unknown")
    room = getattr(event, "room", None)
    room_data = build_room_audit_data(room)
    room_name = None
    if room_data is not None:
        room_name = room_data.get("room_name") or room_data.get("name")
    room_sid = room_data.get("room_id") if room_data else None
    room_metadata = room_data.get("metadata") if room_data else None
    
    consultation = find_consultation_by_room_name(room_name) if room_name else None
    consultation_id = consultation["consultation_id"] if consultation else None

    participant = getattr(event, "participant", None)
    participant_data = None
    has_participant = False
    if hasattr(event, "HasField"):
        try:
            has_participant = event.HasField("participant")
        except Exception:
            has_participant = participant is not None
    else:
        has_participant = participant is not None

    if has_participant and participant is not None:
        participant_data = build_participant_audit_data(participant)

    track = getattr(event, "track", None)
    track_info = None
    track_type_label = None
    track_source_label = None

    has_track = False
    if hasattr(event, "HasField"):
        try:
            has_track = event.HasField("track")
        except Exception:
            has_track = track is not None
    else:
        has_track = track is not None

    if has_track and track is not None:
        raw_track_type = getattr(track, "type", None)
        raw_track_source = getattr(track, "source", None)

        track_type_label = None
        track_source_label = None
        if isinstance(raw_track_type, int):
            track_type_label = {
                0: "audio",
                1: "video",
            }.get(raw_track_type)
        elif raw_track_type is not None:
            track_type_label = str(raw_track_type).lower()

        if isinstance(raw_track_source, int):
            track_source_label = {
                0: "unknown",
                1: "camera",
                2: "microphone",
                3: "screen_share",
                4: "screen_share_audio",
            }.get(raw_track_source)
        elif raw_track_source is not None:
            track_source_label = str(raw_track_source).lower()

        track_info = {
            "type": raw_track_type,
            "source": raw_track_source,
        }

    event_label = f"livekit.{event_type}"
    if track_type_label or track_source_label:
        label_parts = []
        if track_type_label:
            label_parts.append(track_type_label)
        if track_source_label:
            label_parts.append(track_source_label)
        event_label = f"{event_label} ({': '.join(label_parts)})"

    audit(
        event_label,
        consultation_id=consultation_id,
        room=room_data,
        participant=participant_data,
        track=track_info,
        source="webhook",
    )

    return {"status": "received"}


@app.get("/api/audit-events")
async def list_audit_events() -> list[dict[str, Any]]:
    return audit_events[-MAX_AUDIT_EVENTS:]
