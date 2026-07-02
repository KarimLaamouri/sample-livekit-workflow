import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

consultations: dict[str, dict[str, Any]] = {}
audit_events: list[dict[str, Any]] = []


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


def audit(event_type: str, **details: Any) -> None:
    audit_events.append(
        {
            "timestamp": utc_now().isoformat(),
            "event_type": event_type,
            **details,
        }
    )
    del audit_events[:-MAX_AUDIT_EVENTS]


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


@app.post("/api/webhooks")
async def livekit_webhook(request: Request) -> dict[str, str]:
    body = await request.body()
    audit(
        "livekit.webhook.received",
        content_type=request.headers.get("content-type"),
        body_size=len(body),
    )
    return {"status": "received"}


@app.get("/api/audit-events")
async def list_audit_events() -> list[dict[str, Any]]:
    return audit_events[-MAX_AUDIT_EVENTS:]
