from enum import Enum
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import crud
from database import get_db


class Role(str, Enum):
    doctor = "doctor"
    patient = "patient"
    observer = "observer"


class ActorAssertion(BaseModel):
    participant_name: str
    role: Role


class ActorContext(BaseModel):
    consultation_id: str
    participant_name: str
    role: Role


async def get_authorized_doctor(
    consultation_id: str,
    body: ActorAssertion,
    db: AsyncSession = Depends(get_db),
) -> ActorContext:
    consultation = await crud.get_consultation_or_404(db, consultation_id)

    # TODO(ecosystem-auth): replace this block with JWT validation + claim
    # extraction once Tachafy's ecosystem auth service is wired in. Every
    # downstream route depends only on ActorContext, so nothing else needs
    # to change when this happens.
    if body.role != Role.doctor or body.participant_name != consultation.doctor_name:
        raise HTTPException(status_code=403, detail={"code": "NOT_ASSIGNED_DOCTOR"})

    return ActorContext(consultation_id=consultation_id, participant_name=body.participant_name, role=body.role)


async def get_authorized_participant(
    consultation_id: str,
    body: ActorAssertion,
    db: AsyncSession = Depends(get_db),
) -> ActorContext:
    """For routes any admitted participant (doctor or patient) can call, e.g. chat."""
    consultation = await crud.get_consultation_or_404(db, consultation_id)

    # TODO(ecosystem-auth): same replacement note as get_authorized_doctor.
    valid_doctor = body.role == Role.doctor and body.participant_name == consultation.doctor_name
    valid_patient = body.role == Role.patient and body.participant_name == consultation.patient_name
    if not (valid_doctor or valid_patient):
        raise HTTPException(status_code=403, detail={"code": "NOT_ASSIGNED_PARTICIPANT"})

    return ActorContext(consultation_id=consultation_id, participant_name=body.participant_name, role=body.role)
