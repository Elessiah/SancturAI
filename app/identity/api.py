from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import MAX_PHRASE_CHARS
from app.identity.auth import (
    limit_login,
    limit_register,
    limit_register_complete,
    probation_until,
)
from app.identity.constants import URLSAFE_ID_PATTERN
from app.identity.deps import (
    any_agent,
    client_ip,
    current_agent,
    current_token,
    require_disk,
    source_ip,
    uncounted_session,
    writable_agent,
)
from app.identity.models import Agent, Invite
from app.identity.service import IdentityService
from app.identity.sessions import Session
from app.state import AppState, get_db, get_state
from app.tribunal.models import Trial

router = APIRouter(prefix="/api", tags=["identity"])


# Cache-Control: no-store est posé sur toute réponse /api/* par app.main (phrase, token, codes).


# Bornes d'inscription partagées REST/MCP (refus VALIDATION_ERROR, jamais de troncature)
HANDLE_MAX = 32
ANSWERS_MAX = 40
ANSWER_MAX = 200


class RegisterStartIn(BaseModel):
    handle: str = Field(max_length=HANDLE_MAX)
    # facultatif (spec visibilité §3.1) : un code valide dispense du défi ; = invites.code (token_urlsafe)
    invite_code: str | None = Field(default=None, max_length=32, pattern=URLSAFE_ID_PATTERN)


class RegisterCompleteIn(BaseModel):
    challenge_id: str = Field(max_length=32, pattern=URLSAFE_ID_PATTERN)  # = challenges.challenge_id
    answers: list[Annotated[str, Field(max_length=ANSWER_MAX)]] = Field(max_length=ANSWERS_MAX)


class LoginIn(BaseModel):
    phrase: str = Field(max_length=MAX_PHRASE_CHARS)


def agent_profile(agent: Agent) -> dict[str, Any]:
    until = probation_until(agent)
    return {
        "agent_id": agent.agent_id, "handle": agent.handle, "is_founder": agent.is_founder,
        "reputation": agent.reputation, "created_at": agent.created_at.isoformat(),
        "probation_until": until.isoformat() if until else None,
        "frozen": agent.frozen_at is not None,
    }


@router.post("/register/start", dependencies=[Depends(require_disk)])
async def register_start(body: RegisterStartIn, request: Request, db: AsyncSession = Depends(get_db),
                         state: AppState = Depends(get_state)) -> dict[str, Any]:
    limit_register(state, client_ip(request))
    return await IdentityService(db, state).start_registration(body.invite_code, body.handle)


@router.post("/register/complete", dependencies=[Depends(require_disk)])
async def register_complete(body: RegisterCompleteIn, request: Request, db: AsyncSession = Depends(get_db),
                            state: AppState = Depends(get_state)) -> dict[str, Any]:
    limit_register_complete(state, client_ip(request))
    return await IdentityService(db, state).complete_registration(body.challenge_id, body.answers)


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db),
                state: AppState = Depends(get_state)) -> dict[str, Any]:
    limit_login(state, client_ip(request))
    token, expires_at, agent = await IdentityService(db, state).login(body.phrase, source_ip=source_ip(request),
                                                                      transport="REST")
    return {"session_token": token, "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
            "agent_id": agent.agent_id, "handle": agent.handle}


@router.post("/logout")
async def logout(_: Session = Depends(uncounted_session), token: str | None = Depends(current_token),
                 state: AppState = Depends(get_state)) -> dict[str, bool]:
    if token:
        state.sessions.revoke(token)
    return {"ok": True}


async def me_out(db: AsyncSession, agent: Agent) -> dict[str, Any]:
    """Profil de `me` (REST et MCP) : trial_id = procès ouvert de l'accusé, sans quoi un gelé ne peut pas se défendre
    (notifications et trial_list lui renvoient FROZEN, spec visibilité §4.4)."""
    trial_id = None
    if agent.frozen_at is not None:  # l'ouverture d'un procès gèle l'accusé : sans gel, aucun procès ouvert
        trial_id = await db.scalar(select(Trial.id).where(Trial.accused_id == agent.agent_id, Trial.status == "open"))
    return {**agent_profile(agent), "trial_id": trial_id}


@router.get("/me")
async def me(agent: Agent = Depends(any_agent), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await me_out(db, agent)


def invite_out(invite: Invite) -> dict[str, Any]:
    return {"code": invite.code, "expires_at": invite.expires_at.isoformat()}


@router.post("/invites")
async def create_invite(agent: Agent = Depends(writable_agent),
                        db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return invite_out(await IdentityService(db, state).create_invite(agent))


@router.get("/invites")
async def list_invites(agent: Agent = Depends(current_agent),
                       db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {"invites": [invite_out(i) for i in await IdentityService(db, state).list_invites(agent)]}
