from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_messages.service import MAX_TEXT, AdminMessageService, verify_internal
from app.identity.deps import current_agent, require_disk
from app.identity.models import Agent
from app.state import AppState, get_db, get_state

router = APIRouter(tags=["admin-messages"])


class SendIn(BaseModel):
    text: str = Field(max_length=MAX_TEXT + 100)
    short_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{4}$")


class ReplyIn(BaseModel):
    short_id: str = Field(pattern=r"^[0-9a-f]{4}$")
    text: str = Field(max_length=MAX_TEXT + 100)


@router.post("/api/admin-messages", dependencies=[Depends(require_disk)])
async def send(body: SendIn, agent: Agent = Depends(current_agent), db: AsyncSession = Depends(get_db),
               state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await AdminMessageService(db, state).send(agent, body.text, body.short_id)


@router.get("/api/admin-messages")
async def inbox(agent: Agent = Depends(current_agent), db: AsyncSession = Depends(get_db),
                state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await AdminMessageService(db, state).inbox(agent)


def internal_guard(request: Request, state: AppState = Depends(get_state)) -> None:
    # dépendance : résolue AVANT la validation du body, donc rien n'est révélé sans token valide
    verify_internal(request, state.settings)


@router.post("/internal/admin-replies", include_in_schema=False, dependencies=[Depends(internal_guard)])
async def admin_reply(body: ReplyIn, db: AsyncSession = Depends(get_db),
                      state: AppState = Depends(get_state)) -> dict[str, bool]:
    await AdminMessageService(db, state).reply(body.short_id, body.text)
    return {"ok": True}
