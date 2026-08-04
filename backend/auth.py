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


async def authorize_doctor(
    consultation_id: str, assertion: ActorAssertion, db: AsyncSession
) -> ActorContext:
    consultation = await crud.get_consultation_or_404(db, consultation_id)

    # TODO(ecosystem-auth): replace this block with JWT validation + claim
    # extraction once Tachafy's ecosystem auth service is wired in. Every
    # downstream route depends only on ActorContext, so nothing else needs
    # to change when this happens.
    if assertion.role != Role.doctor or assertion.participant_name != consultation.doctor_name:
        raise HTTPException(status_code=403, detail={"code": "NOT_ASSIGNED_DOCTOR"})

    return ActorContext(
        consultation_id=consultation_id,
        participant_name=assertion.participant_name,
        role=assertion.role,
    )


async def get_authorized_doctor(
    consultation_id: str,
    body: ActorAssertion,
    db: AsyncSession = Depends(get_db),
) -> ActorContext:
    return await authorize_doctor(consultation_id, body, db)


async def authorize_participant(
    consultation_id: str, assertion: ActorAssertion, db: AsyncSession
) -> ActorContext:
    """For routes any admitted participant (doctor or patient) can call, e.g. chat."""
    consultation = await crud.get_consultation_or_404(db, consultation_id)

    valid_doctor = assertion.role == Role.doctor and assertion.participant_name == consultation.doctor_name
    valid_patient = assertion.role == Role.patient and assertion.participant_name == consultation.patient_name

    # TODO(ecosystem-auth): same replacement note as authorize_doctor.
    if assertion.role == Role.doctor and not valid_doctor:
        raise HTTPException(status_code=403, detail={"code": "NOT_ASSIGNED_DOCTOR"})
    if assertion.role == Role.patient and not valid_patient:
        raise HTTPException(status_code=403, detail={"code": "NOT_ASSIGNED_PATIENT"})
    if assertion.role not in (Role.doctor, Role.patient):
        raise HTTPException(status_code=403, detail={"code": "NOT_ASSIGNED_PARTICIPANT"})

    return ActorContext(
        consultation_id=consultation_id,
        participant_name=assertion.participant_name,
        role=assertion.role,
    )


async def get_authorized_participant(
    consultation_id: str,
    body: ActorAssertion,
    db: AsyncSession = Depends(get_db),
) -> ActorContext:
    return await authorize_participant(consultation_id, body, db)
