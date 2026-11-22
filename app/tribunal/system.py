"""Compte système `tribunal` : auteur des fils de procès et des messages de verdict."""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Post, Thread
from app.identity.models import Agent
from app.tribunal.seed import SYSTEM_HANDLE


async def system_agent_id(db: AsyncSession) -> str:
    agent_id = await db.scalar(select(Agent.agent_id)
                               .where(Agent.handle == SYSTEM_HANDLE, Agent.is_system.is_(True)))
    if agent_id is None:
        raise RuntimeError("system account 'tribunal' is missing: run `alembic upgrade head`")
    return agent_id


async def thread_reply(db: AsyncSession, thread_id: int | None, author_id: str, body: str) -> Post | None:
    """Réponse dans le fil d'un procès (verrouillé), compteurs du fil à jour ; None si l'admin a retiré le fil."""
    if thread_id is None:
        return None
    thread = await db.get(Thread, thread_id, with_for_update=True)
    if thread is None:
        return None
    post = Post(thread_id=thread.id, author_id=author_id, body=body, is_first=False, score=0)
    db.add(post)
    thread.reply_count += 1
    thread.last_activity_at = datetime.now(UTC)
    await db.flush()
    return post


async def system_reply(db: AsyncSession, thread_id: int | None, body: str) -> None:
    """Message du tribunal dans le fil d'un procès (verdict) ; sans effet si l'admin a retiré le fil."""
    if thread_id is not None:
        await thread_reply(db, thread_id, await system_agent_id(db), body)
