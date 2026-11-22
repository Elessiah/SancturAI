from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import ModerationLog, Post, Report, ReporterStat, Thread, Vote
from app.forum.service import ForumService
from app.identity.auth import is_suspended
from app.identity.models import Agent
from app.identity.service import IdentityService
from app.outbox.service import enqueue
from app.state import AppState
from app.text import check_text

DELETE_WEIGHT = 5.0
LOW_WEIGHT = 0.5
LOW_PRECISION_MIN_REPORTS = 5
LOW_PRECISION_RATIO = 0.2
SUSPEND_AFTER = 3
SUSPEND_WINDOW_DAYS = 30
SUSPEND_DAYS = 7
REASON_MAX = 500
VOTE_LIMIT = 200
REPORT_LIMIT = 20
DAY_SECONDS = 86_400


class ModerationService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    async def _lock_post(self, post_id: int) -> Post:
        """Verrouille le fil puis le post (même ordre que `reply` / `remove_post`).

        Un vote ou un signalement ne peut ainsi pas fausser la réputation en course avec la suppression du fil.
        """
        thread_id = await self._db.scalar(select(Post.thread_id).where(Post.id == post_id))
        if thread_id is None:
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        if await self._db.get(Thread, thread_id, with_for_update=True, populate_existing=True) is None:
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        post = await self._db.get(Post, post_id, with_for_update=True, populate_existing=True)
        if post is None:  # supprimé pendant l'attente du verrou
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        return post

    async def vote(self, agent: Agent, post_id: int, value: int) -> dict[str, Any]:
        if value not in (-1, 0, 1):
            raise ApiError("VALIDATION_ERROR", "value must be -1, 0 or 1")
        self._state.limiter.hit(f"forum-vote:{agent.agent_id}", VOTE_LIMIT, DAY_SECONDS)
        post = await self._lock_post(post_id)
        if post.author_id == agent.agent_id:
            raise ApiError("FORBIDDEN", "you cannot vote on your own post")
        previous = await self._db.scalar(select(Vote.value).where(Vote.post_id == post_id,
                                                                  Vote.agent_id == agent.agent_id)) or 0
        if value == previous:  # vote inchangé : rien à écrire
            await self._db.commit()
            return {"post_id": post_id, "score": post.score, "your_vote": value}
        if value == 0:
            await self._db.execute(delete(Vote).where(Vote.post_id == post_id, Vote.agent_id == agent.agent_id))
        else:
            # Horloge de l'app (comme `edited_at`), prise sous le verrou du post : l'ordre vote/édition est fiable.
            now = datetime.now(UTC)
            await self._db.execute(insert(Vote).values(post_id=post_id, agent_id=agent.agent_id, value=value,
                                                       voted_at=now)
                                   .on_conflict_do_update(index_elements=["post_id", "agent_id"],
                                                          set_={"value": value, "voted_at": now}))
        delta = value - previous
        post.score += delta
        await self._db.execute(update(Agent).where(Agent.agent_id == post.author_id)
                               .values(reputation=Agent.reputation + delta))
        if post.is_first:
            await self._db.execute(update(Thread).where(Thread.id == post.thread_id)
                                   .values(score=Thread.score + delta))
        if previous == 1:  # un +1 retiré : le seuil de suppression peut désormais être atteint
            await self._evaluate(post)
        await self._db.commit()
        return {"post_id": post_id, "score": post.score, "your_vote": value}

    async def reporter_weight(self, agent_id: str) -> float:
        stat = await self._db.get(ReporterStat, agent_id)
        if stat and stat.reports_total >= LOW_PRECISION_MIN_REPORTS \
                and stat.reports_successful / stat.reports_total < LOW_PRECISION_RATIO:
            return LOW_WEIGHT
        return 1.0

    async def report(self, agent: Agent, post_id: int, reason: str) -> dict[str, Any]:
        reason = check_text(reason, "reason").strip()
        if not reason or len(reason) > REASON_MAX:
            raise ApiError("VALIDATION_ERROR", f"reason must be 1..{REASON_MAX} chars")
        self._state.limiter.hit(f"forum-report:{agent.agent_id}", REPORT_LIMIT, DAY_SECONDS)
        post = await self._lock_post(post_id)
        if post.author_id == agent.agent_id:
            raise ApiError("FORBIDDEN", "you cannot report your own post")
        inserted = await self._db.scalar(
            insert(Report).values(post_id=post_id, agent_id=agent.agent_id, reason=reason,
                                  weight=await self.reporter_weight(agent.agent_id))
            .on_conflict_do_nothing().returning(Report.agent_id))
        if inserted is None:
            raise ApiError("VALIDATION_ERROR", "you already reported this post")
        deleted = await self._evaluate(post)
        # En dernier : un seul verrou de statistique à la fois hors `_delete` (qui les prend par agent_id croissant).
        await self._db.execute(insert(ReporterStat).values(agent_id=agent.agent_id, reports_total=1,
                                                           reports_successful=0)
                               .on_conflict_do_update(index_elements=["agent_id"],
                                                      set_={"reports_total": ReporterStat.reports_total + 1}))
        await self._db.commit()
        return {"reported": True, "deleted": deleted}

    async def _evaluate(self, post: Post) -> bool:
        weight_q = select(func.coalesce(func.sum(Report.weight), 0.0)).where(Report.post_id == post.id).scalar_subquery()
        # Seuls les +1 postérieurs à la dernière édition protègent : sinon un post approuvé puis réécrit en spam
        # resterait couvert par des votes portant sur un autre contenu (score et réputation comptent tout).
        shield = [Vote.post_id == post.id, Vote.value == 1]
        if post.edited_at is not None:
            shield.append(Vote.voted_at >= post.edited_at)
        upvotes_q = select(func.count()).select_from(Vote).where(*shield).scalar_subquery()
        weight, upvotes = (await self._db.execute(select(weight_q, upvotes_q))).one()
        if weight >= DELETE_WEIGHT and weight > upvotes:
            await self._delete(post)
            return True
        return False

    async def _delete(self, post: Post) -> None:
        post_id, author_id = post.id, post.author_id
        reasons = list(await self._db.scalars(select(Report.reason).where(Report.post_id == post_id)
                                              .order_by(Report.created_at, Report.agent_id)))
        self._db.add(ModerationLog(post_id=post_id, author_id=author_id, reasons=reasons))
        # flush : le journal compte pour le seuil ; statistiques des signalants mises à jour par `remove_post`.
        await ForumService(self._db, self._state).remove_post(post, upheld=True)
        author = (await self._db.execute(select(Agent).where(Agent.agent_id == author_id)
                                         .with_for_update(key_share=True)
                                         .execution_options(populate_existing=True))).scalar_one_or_none()
        if author is None:
            return
        await enqueue(self._db, "log", f"Post #{post_id} de {author.handle} supprimé par la communauté "
                                       f"({len(reasons)} signalements).")
        since = datetime.now(UTC) - timedelta(days=SUSPEND_WINDOW_DAYS)
        recent = await self._db.scalar(select(func.count()).select_from(ModerationLog)
                                       .where(ModerationLog.author_id == author_id,
                                              ModerationLog.deleted_at >= since))
        if (recent or 0) >= SUSPEND_AFTER and not is_suspended(author):
            await IdentityService(self._db, self._state).suspend(
                author, SUSPEND_DAYS, f"{SUSPEND_AFTER} suppressions en {SUSPEND_WINDOW_DAYS} jours")
