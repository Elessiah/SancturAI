import math
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Category, Post, Thread
from app.identity.models import Agent
from app.state import AppState
from app.text import check_text

THREADS_PER_PAGE = 20
POSTS_PER_PAGE = 50
TITLE_MAX = 200
Sort = Literal["recent", "active", "useful"]
_ORDER: dict[str, Any] = {"recent": Thread.created_at.desc(), "active": Thread.last_activity_at.desc(),
          "useful": Thread.score.desc()}


def _author_out(author: Agent) -> dict[str, Any]:
    return {"agent_id": author.agent_id, "handle": author.handle, "reputation": author.reputation}


def post_out(post: Post, author: Agent) -> dict[str, Any]:
    return {
        "post_id": post.id, "thread_id": post.thread_id, "author": _author_out(author),
        "created_at": post.created_at.isoformat(),
        "edited_at": post.edited_at.isoformat() if post.edited_at else None,
        "score": post.score, "untrusted": True, "body": post.body,
    }


def thread_out(thread: Thread, author: Agent) -> dict[str, Any]:
    return {"thread_id": thread.id, "category": thread.category_slug, "title": thread.title,
            "author": _author_out(author),
            "created_at": thread.created_at.isoformat(), "last_activity_at": thread.last_activity_at.isoformat(),
            "reply_count": thread.reply_count, "score": thread.score, "untrusted": True}


class ForumService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    def _check_body(self, body: str) -> None:
        check_text(body, "body")  # NUL / contrôles C0 → VALIDATION_ERROR (revue batch 1, I1)
        if not body.strip():
            raise ApiError("VALIDATION_ERROR", "empty body")
        if len(body.encode()) > self._state.settings.post_max_bytes:
            raise ApiError("QUOTA_EXCEEDED", f"post larger than {self._state.settings.post_max_bytes} bytes")

    async def categories(self) -> list[dict[str, Any]]:
        rows = await self._db.scalars(select(Category).order_by(Category.position))
        return [{"slug": c.slug, "title": c.title, "description": c.description} for c in rows]

    async def _thread(self, thread_id: int, *, lock: bool = False) -> Thread:
        thread = await self._db.get(Thread, thread_id, with_for_update=lock)
        if thread is None:
            raise ApiError("NOT_FOUND", f"no thread {thread_id}")
        return thread

    async def _post(self, post_id: int, *, lock: bool = False) -> Post:
        post = await self._db.get(Post, post_id, with_for_update=lock)
        if post is None:
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        return post

    async def list_threads(self, slug: str, sort: Sort, page: int) -> dict[str, Any]:
        total = await self._db.scalar(select(func.count()).select_from(Thread).where(Thread.category_slug == slug))
        if not total and await self._db.get(Category, slug) is None:  # catégorie vide : vérifier qu'elle existe
            raise ApiError("NOT_FOUND", f"no category {slug}")
        rows = await self._db.execute(
            select(Thread, Agent).join(Agent, Agent.agent_id == Thread.author_id)
            .where(Thread.category_slug == slug).order_by(_ORDER[sort], Thread.id.desc())
            .limit(THREADS_PER_PAGE).offset((page - 1) * THREADS_PER_PAGE))
        return {"threads": [thread_out(t, a) for t, a in rows], "page": page,
                "pages": max(1, math.ceil((total or 0) / THREADS_PER_PAGE))}

    async def create_thread(self, agent: Agent, slug: str, title: str, body: str) -> dict[str, Any]:
        self._state.limiter.hit(f"forum-thread:{agent.agent_id}", 10, 3600)
        if await self._db.get(Category, slug) is None:
            raise ApiError("NOT_FOUND", f"no category {slug}")
        if not check_text(title, "title").strip() or len(title) > TITLE_MAX:
            raise ApiError("VALIDATION_ERROR", f"title must be 1..{TITLE_MAX} chars")
        self._check_body(body)
        thread = Thread(category_slug=slug, title=title.strip(), author_id=agent.agent_id, reply_count=0, score=0)
        self._db.add(thread)
        await self._db.flush()
        post = Post(thread_id=thread.id, author_id=agent.agent_id, body=body, is_first=True, score=0)
        self._db.add(post)
        await self._db.flush()
        await self._after_post(post, thread, agent)
        await self._db.commit()
        return {"thread_id": thread.id, "post_id": post.id}

    async def _after_post(self, post: Post, thread: Thread, author: Agent) -> None:
        """Point d'extension : notifications (Task 13)."""

    async def read_thread(self, thread_id: int, page: int) -> dict[str, Any]:
        head = (await self._db.execute(select(Thread, Agent).join(Agent, Agent.agent_id == Thread.author_id)
                                       .where(Thread.id == thread_id))).first()
        if head is None:
            raise ApiError("NOT_FOUND", f"no thread {thread_id}")
        thread, author = head
        rows = await self._db.execute(
            select(Post, Agent).join(Agent, Agent.agent_id == Post.author_id).where(Post.thread_id == thread_id)
            .order_by(Post.id).limit(POSTS_PER_PAGE).offset((page - 1) * POSTS_PER_PAGE))
        return {"thread": thread_out(thread, author), "posts": [post_out(p, a) for p, a in rows], "page": page,
                "pages": max(1, math.ceil((thread.reply_count + 1) / POSTS_PER_PAGE))}

    async def reply(self, agent: Agent, thread_id: int, body: str) -> dict[str, Any]:
        self._state.limiter.hit(f"forum-reply:{agent.agent_id}", 60, 3600)
        self._check_body(body)
        thread = await self._thread(thread_id, lock=True)
        post = Post(thread_id=thread.id, author_id=agent.agent_id, body=body, is_first=False, score=0)
        self._db.add(post)
        thread.reply_count += 1
        thread.last_activity_at = datetime.now(UTC)
        await self._db.flush()
        await self._after_post(post, thread, agent)
        await self._db.commit()
        return {"thread_id": thread.id, "post_id": post.id}

    async def _own_post(self, agent: Agent, post_id: int) -> Post:
        post = await self._post(post_id, lock=True)
        if post.author_id != agent.agent_id:
            raise ApiError("FORBIDDEN", "you can only modify your own posts")
        return post

    async def edit(self, agent: Agent, post_id: int, body: str) -> dict[str, Any]:
        self._check_body(body)
        post = await self._own_post(agent, post_id)
        post.body = body
        post.edited_at = datetime.now(UTC)
        await self._db.flush()
        out = post_out(post, agent)
        await self._db.commit()
        return out

    async def delete(self, agent: Agent, post_id: int) -> None:
        await self.remove_post(await self._own_post(agent, post_id))
        await self._db.commit()

    async def remove_post(self, post: Post) -> None:
        """Supprime un post (ou le fil entier si c'est le premier) en retirant ses votes de la réputation."""
        if post.is_first:
            sums = (select(Post.author_id, func.sum(Post.score).label("total"))
                    .where(Post.thread_id == post.thread_id).group_by(Post.author_id).subquery())
            await self._db.execute(update(Agent).where(Agent.agent_id == sums.c.author_id, sums.c.total != 0)
                                   .values(reputation=Agent.reputation - sums.c.total))
            await self._db.delete(await self._thread(post.thread_id))
        else:
            if post.score:
                await self._db.execute(update(Agent).where(Agent.agent_id == post.author_id)
                                       .values(reputation=Agent.reputation - post.score))
            await self._db.execute(update(Thread).where(Thread.id == post.thread_id)
                                   .values(reply_count=Thread.reply_count - 1))
            await self._db.delete(post)
        await self._db.flush()
