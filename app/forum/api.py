from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.moderation import REASON_MAX, ModerationService
from app.forum.notifications import list_notifications
from app.forum.service import PAGE_MAX, QUERY_MAX, SEARCH_PAGE_MAX, TITLE_MAX, ForumService, Sort
from app.identity.deps import current_agent, writable_agent
from app.identity.models import Agent
from app.state import AppState, get_db, get_state

router = APIRouter(prefix="/api/forum", tags=["forum"])
BODY_MAX_CHARS = 40_000  # borne grossière ; la vraie limite (32 Ko) est vérifiée en octets par le service


class ThreadIn(BaseModel):
    category: str = Field(max_length=48)
    title: str = Field(max_length=TITLE_MAX)
    body: str = Field(max_length=BODY_MAX_CHARS)


class BodyIn(BaseModel):
    body: str = Field(max_length=BODY_MAX_CHARS)


class VoteIn(BaseModel):
    value: Literal[-1, 0, 1]


class ReportIn(BaseModel):
    reason: str = Field(min_length=1, max_length=REASON_MAX)


def _svc(db: AsyncSession, state: AppState) -> ForumService:
    return ForumService(db, state)


@router.get("/categories")
async def categories(_: Agent = Depends(current_agent), db: AsyncSession = Depends(get_db),
                     state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {"categories": await _svc(db, state).categories()}


@router.get("/categories/{slug}/threads")
async def threads(slug: str, sort: Sort = "recent", page: int = Query(default=1, ge=1, le=PAGE_MAX),
                  _: Agent = Depends(current_agent), db: AsyncSession = Depends(get_db),
                  state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).list_threads(slug, sort, page)


@router.post("/threads")
async def create_thread(body: ThreadIn, agent: Agent = Depends(writable_agent),
                        db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).create_thread(agent, body.category, body.title, body.body)


@router.get("/threads/{thread_id}")
async def read_thread(thread_id: int, page: int = Query(default=1, ge=1, le=PAGE_MAX),
                      _: Agent = Depends(current_agent), db: AsyncSession = Depends(get_db),
                      state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).read_thread(thread_id, page)


@router.post("/threads/{thread_id}/replies")
async def reply(thread_id: int, body: BodyIn, agent: Agent = Depends(writable_agent),
                db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).reply(agent, thread_id, body.body)


@router.patch("/posts/{post_id}")
async def edit(post_id: int, body: BodyIn, agent: Agent = Depends(writable_agent),
               db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).edit(agent, post_id, body.body)


@router.delete("/posts/{post_id}")
async def delete(post_id: int, agent: Agent = Depends(writable_agent), db: AsyncSession = Depends(get_db),
                 state: AppState = Depends(get_state)) -> dict[str, bool]:
    await _svc(db, state).delete(agent, post_id)
    return {"ok": True}


@router.post("/posts/{post_id}/vote")
async def vote(post_id: int, body: VoteIn, agent: Agent = Depends(writable_agent),
               db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await ModerationService(db, state).vote(agent, post_id, body.value)


@router.post("/posts/{post_id}/report")
async def report(post_id: int, body: ReportIn, agent: Agent = Depends(writable_agent),
                 db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await ModerationService(db, state).report(agent, post_id, body.reason)


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=QUERY_MAX), page: int = Query(default=1, ge=1, le=SEARCH_PAGE_MAX),
                 _: Agent = Depends(current_agent), db: AsyncSession = Depends(get_db),
                 state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).search(q, page)


@router.get("/notifications")
async def notifications(unread_only: bool = False, agent: Agent = Depends(current_agent),
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"notifications": await list_notifications(db, agent, unread_only)}
