import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel, Field

load_dotenv()

TOKEN_TTL_SECONDS = 15 * 60
CONSULTATION_TTL_MINUTES = 60
MAX_AUDIT_EVENTS = 200

Role = Literal["doctor", "patient", "observer"]

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


def get_consultation_or_404(consultation_id: str) -> dict[str, Any]:
    consultation = consultations.get(consultation_id)

    if consultation is None:
        raise HTTPException(status_code=404, detail="Consultation not found")

    if consultation["expires_at"] < utc_now():
        audit("consultation.expired", consultation_id=consultation_id)
        consultations.pop(consultation_id, None)
        raise HTTPException(status_code=410, detail="Consultation expired")

    return consultation


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
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
    )


@app.post("/api/consultations/{consultation_id}/token", response_model=TokenResponse)
async def create_consultation_token(
    consultation_id: str,
    payload: TokenRequest,
) -> TokenResponse:
    consultation = get_consultation_or_404(consultation_id)
    room_name = consultation["room_name"]

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


@app.get("/api/get-token")
async def get_token(
    room_name: str,
    participant_name: str,
    role: Role = "doctor",
) -> dict[str, str | int]:
    token = build_token(
        consultation_id="legacy-manual-test",
        room_name=room_name,
        participant_name=participant_name,
        role=role,
    )

    audit(
        "token.issued.legacy",
        consultation_id="legacy-manual-test",
        room_name=room_name,
        participant_name=participant_name,
        role=role,
        token_ttl_seconds=TOKEN_TTL_SECONDS,
    )

    return {"token": token, "expires_in_seconds": TOKEN_TTL_SECONDS}


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
