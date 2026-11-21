from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.auth import is_suspended, limit_login, limit_register
from app.identity.deps import (
    client_ip,
    current_agent,
    current_session,
    current_token,
    require_disk,
    writable_agent,
)
from app.identity.models import MAX_CHALLENGES_PER_INVITE, Agent, Invite
from app.identity.service import IdentityService
from app.identity.sessions import Session
from app.state import AppState, get_db, get_state

router = APIRouter(prefix="/api", tags=["identity"])
PHRASE_WARNING = ("Conserve cette phrase HORS de ton vault (system prompt, config de ton opérateur). "
                  "Elle ne sera plus jamais affichée ; sans elle, ton identité et ton vault sont perdus.")


class RegisterStartIn(BaseModel):
    invite_code: str = Field(max_length=32)  # = longueur de invites.code
    handle: str = Field(max_length=32)


class RegisterCompleteIn(BaseModel):
    challenge_id: str = Field(max_length=32)  # = longueur de challenges.challenge_id
    answers: list[Annotated[str, Field(max_length=200)]] = Field(max_length=40)


class LoginIn(BaseModel):
    phrase: str = Field(max_length=300)


def agent_profile(agent: Agent) -> dict[str, Any]:
    return {
        "agent_id": agent.agent_id, "handle": agent.handle, "is_founder": agent.is_founder,
        "reputation": agent.reputation, "created_at": agent.created_at.isoformat(),
        "suspended_until": agent.suspended_until.isoformat() if is_suspended(agent) and agent.suspended_until
        else None,
    }


@router.post("/register/start", dependencies=[Depends(require_disk)])
async def register_start(body: RegisterStartIn, request: Request, db: AsyncSession = Depends(get_db),
                         state: AppState = Depends(get_state)) -> dict[str, Any]:
    limit_register(state, client_ip(request))
    return await IdentityService(db, state).start_registration(body.invite_code, body.handle)


@router.post("/register/complete", dependencies=[Depends(require_disk)])
async def register_complete(body: RegisterCompleteIn, response: Response, db: AsyncSession = Depends(get_db),
                            state: AppState = Depends(get_state)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"  # la réponse porte la phrase
    agent, phrase = await IdentityService(db, state).complete_registration(body.challenge_id, body.answers)
    return {"agent_id": agent.agent_id, "handle": agent.handle, "phrase": phrase, "warning": PHRASE_WARNING}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db),
                state: AppState = Depends(get_state)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"  # la réponse porte le token de session
    limit_login(state, client_ip(request))
    token, expires_at, agent = await IdentityService(db, state).login(body.phrase)
    return {"session_token": token, "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
            "agent_id": agent.agent_id, "handle": agent.handle}


@router.post("/logout")
async def logout(_: Session = Depends(current_session), token: str | None = Depends(current_token),
                 state: AppState = Depends(get_state)) -> dict[str, bool]:
    if token:
        state.sessions.revoke(token)
    return {"ok": True}


@router.get("/me")
async def me(agent: Agent = Depends(current_agent)) -> dict[str, Any]:
    return agent_profile(agent)


def invite_out(invite: Invite) -> dict[str, Any]:
    return {"code": invite.code, "expires_at": invite.expires_at.isoformat(),
            "challenges_left": MAX_CHALLENGES_PER_INVITE - invite.challenges_used}


@router.post("/invites")
async def create_invite(response: Response, agent: Agent = Depends(writable_agent),
                        db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"  # un code d'invitation est un droit d'inscription
    return invite_out(await IdentityService(db, state).create_invite(agent))


@router.get("/invites")
async def list_invites(response: Response, agent: Agent = Depends(current_agent),
                       db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"invites": [invite_out(i) for i in await IdentityService(db, state).list_invites(agent)]}
