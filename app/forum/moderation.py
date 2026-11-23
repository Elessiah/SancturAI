from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, String, column, delete, func, select, update, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import ModerationLog, Post, Report, ReporterStat, Thread, Vote
from app.forum.notifications import notify_moderation
from app.forum.seed import TRIBUNAL_SLUG
from app.forum.service import ForumService, check_id
from app.identity.auth import ensure_not_probation, ensure_writable
from app.identity.models import AGENT_ROW_LOCK, Agent
from app.outbox.service import enqueue
from app.state import AppState
from app.text import check_text
from app.tribunal.models import Trial
from app.tribunal.opening import open_trial
from app.tribunal.rules import DELETIONS_TRIGGER, DELETIONS_WINDOW

DELETE_WEIGHT = 5.0
LOW_WEIGHT = 0.5
LOW_PRECISION_MIN_REPORTS = 5
LOW_PRECISION_RATIO = 0.2
SETTLE_DAYS = 7  # un signalement sur un post encore en ligne après 7 jours a échoué (M9)
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
        thread_id = await self._db.scalar(select(Post.thread_id).where(Post.id == check_id(post_id, "post")))
        if thread_id is None:
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        if await self._db.get(Thread, thread_id, with_for_update=True, populate_existing=True) is None:
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        post = await self._db.get(Post, post_id, with_for_update=True, populate_existing=True)
        if post is None:  # supprimé pendant l'attente du verrou
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        return post

    async def vote(self, agent: Agent, post_id: int, value: int) -> dict[str, Any]:
        ensure_writable(self._state, agent)  # dans le service : REST et MCP identiques (I5)
        ensure_not_probation(agent, "votes")
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
        if post.is_first:
            await self._db.execute(update(Thread).where(Thread.id == post.thread_id)
                                   .values(score=Thread.score + delta))
        if previous == 1:  # un +1 retiré : le seuil de suppression peut désormais être atteint
            await self._evaluate(post)
        # Réputation après `_evaluate` (ordre global : statistiques des signalants avant agents, revue batch 2 I2).
        # Si le post vient d'être supprimé, `remove_post` a retiré le nouveau score s+d ; +d ici laisse R - s.
        await self._db.execute(update(Agent).where(Agent.agent_id == post.author_id)
                               .values(reputation=Agent.reputation + delta))
        await self._db.commit()
        return {"post_id": post_id, "score": post.score, "your_vote": value}

    async def retract_votes(self, agent_id: str) -> None:
        """Retire d'un coup tous les votes d'un agent banni : mêmes effets que `vote(..., 0)` sur chaque post
        (scores, réputation des auteurs, réévaluation des posts qui perdent un +1), en requêtes ensemblistes plutôt
        qu'une dizaine d'allers-retours par vote. Ne commit pas."""
        voted = select(Vote.post_id).where(Vote.agent_id == agent_id)
        # Ordre global : fils puis posts, chacun par id croissant (aucun interblocage entre deux purges).
        await self._db.execute(select(Thread.id).where(Thread.id.in_(select(Post.thread_id)
                                                                     .where(Post.id.in_(voted))))
                               .order_by(Thread.id).with_for_update())
        await self._db.execute(select(Post.id).where(Post.id.in_(voted)).order_by(Post.id).with_for_update())
        rows = (await self._db.execute(select(Vote.post_id, Vote.value, Post.author_id)
                                       .join(Post, Post.id == Vote.post_id).where(Vote.agent_id == agent_id))).all()
        if not rows:
            return
        mine = select(Vote.post_id, Vote.value).where(Vote.agent_id == agent_id).subquery()
        no_sync = {"synchronize_session": False}
        await self._db.execute(update(Post).where(Post.id == mine.c.post_id)
                               .values(score=Post.score - mine.c.value).execution_options(**no_sync))
        await self._db.execute(update(Thread).where(Thread.id == Post.thread_id, Post.id == mine.c.post_id,
                                                    Post.is_first.is_(True))
                               .values(score=Thread.score - mine.c.value).execution_options(**no_sync))
        await self._db.execute(delete(Vote).where(Vote.agent_id == agent_id))
        # Un +1 retiré peut faire franchir le seuil : seuls les posts assez signalés sont réévalués.
        upvoted = [pid for pid, value, _ in rows if value == 1]
        candidates = list(await self._db.scalars(
            select(Report.post_id).where(Report.post_id.in_(upvoted)).group_by(Report.post_id)
            .having(func.sum(Report.weight) >= DELETE_WEIGHT).order_by(Report.post_id))) if upvoted else []
        for pid in candidates:
            post = await self._db.get(Post, pid, populate_existing=True)
            if post is not None:  # sinon emporté avec son fil par une réévaluation précédente
                await self._evaluate(post)
        # Réputation après les réévaluations (ordre global : statistiques des signalants avant agents) ; un post
        # supprimé entre-temps a retiré son nouveau score s-v : -v ici laisse R - s, comme dans `vote`.
        totals: dict[str, int] = {}
        for _, value, author_id in rows:
            totals[author_id] = totals.get(author_id, 0) + value
        changed = {aid: total for aid, total in totals.items() if total}
        if changed:
            await ForumService(self._db, self._state).lock_agents(set(changed))
            delta = values(column("agent_id", String), column("total", Integer), name="delta").data(
                sorted(changed.items()))
            await self._db.execute(update(Agent).where(Agent.agent_id == delta.c.agent_id)
                                   .values(reputation=Agent.reputation - delta.c.total)
                                   .execution_options(synchronize_session=False))

    async def reporter_weight(self, agent_id: str) -> float:
        """Précision = signalements aboutis / signalements tranchés, posts et agents confondus (spec visibilité §4.6).

        Posts : abouti (post supprimé par la communauté) ou échoué (post toujours en ligne SETTLE_DAYS après le
        signalement) ; un signalement en attente ne compte pas. Agents : réglés à la clôture du procès qui les a
        consommés (`agent_reports_total`), réussis si bannissement ; un signalement expiré sans procès est neutre.
        """
        # Une seule instruction : stats et signalements échus lus dans le même instantané (revue batch 2 N3).
        failed = (select(func.count()).select_from(Report)
                  .where(Report.agent_id == agent_id,
                         Report.created_at < datetime.now(UTC) - timedelta(days=SETTLE_DAYS))
                  .scalar_subquery())
        row = (await self._db.execute(
            select(ReporterStat.reports_successful, ReporterStat.agent_reports_total,
                   ReporterStat.agent_reports_successful, failed)
            .where(ReporterStat.agent_id == agent_id))).first()
        if row is None:
            return 1.0
        posts_ok, agents_total, agents_ok, posts_failed = row
        successful = posts_ok + agents_ok
        settled = posts_ok + posts_failed + agents_total
        if settled >= LOW_PRECISION_MIN_REPORTS and successful / settled < LOW_PRECISION_RATIO:
            return LOW_WEIGHT
        return 1.0

    async def report(self, agent: Agent, post_id: int, reason: str) -> dict[str, Any]:
        ensure_writable(self._state, agent)
        ensure_not_probation(agent, "reports")
        reason = check_text(reason, "reason").strip()
        if not reason or len(reason) > REASON_MAX:
            raise ApiError("VALIDATION_ERROR", f"reason must be 1..{REASON_MAX} chars")
        self._state.limiter.hit(f"forum-report:{agent.agent_id}", REPORT_LIMIT, DAY_SECONDS)
        post = await self._lock_post(post_id)
        thread = await self._db.get(Thread, post.thread_id)  # déjà verrouillé par _lock_post (carte d'identité)
        if thread is not None and thread.category_slug == TRIBUNAL_SLUG:
            raise ApiError("FORBIDDEN", "tribunal posts cannot be reported")
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
        # flush : le journal compte pour le déclencheur ; statistiques des signalants mises à jour par `remove_post`.
        # L'auteur (procès éventuel) est verrouillé par `remove_post` avec les autres auteurs, par agent_id croissant :
        # aucun agent verrouillé hors de l'ordre global (revue batch 2, M1).
        await ForumService(self._db, self._state).remove_post(post, upheld=True, lock_agents={author_id})
        author = (await self._db.execute(select(Agent).where(Agent.agent_id == author_id)
                                         .with_for_update(key_share=AGENT_ROW_LOCK["key_share"])
                                         .execution_options(populate_existing=True))).scalar_one_or_none()
        if author is None:
            return
        await enqueue(self._db, "log", f"Post #{post_id} de {author.handle} supprimé par la communauté "
                                       f"({len(reasons)} signalements).")
        notify_moderation(self._db, author_id, f"Votre post #{post_id} a été supprimé par la communauté.")
        await self._maybe_open_trial(author)

    async def _maybe_open_trial(self, author: Agent) -> None:
        """Troisième post supprimé en 30 jours (spec visibilité §4.3, déclencheur 2) : ouvre un procès.

        Seules comptent les suppressions postérieures à la clôture du dernier procès de l'agent : un agent réactivé
        n'est pas rejugé pour les mêmes posts, ni pour ceux supprimés pendant son procès (encore signalables comme
        pièces du dossier, déjà connus des votants ; revue secu Task 5, S1). Les posts du tribunal ne sont jamais
        signalables, donc jamais comptés.
        L'appelant (`_delete`) a verrouillé la ligne de l'auteur et relu son statut (`populate_existing`).
        """
        if author.frozen_at is not None or author.banned_at is not None or author.is_system:
            return
        # procès encore ouvert (closed_at NULL) : son ouverture borne la fenêtre (l'auteur gelé est écarté plus haut)
        last_trial = (select(func.max(func.coalesce(Trial.closed_at, Trial.opened_at)))
                      .where(Trial.accused_id == author.agent_id).scalar_subquery())
        # une seule requête (ix_modlog_author_time) ; GREATEST ignore NULL : sans procès antérieur, fenêtre seule
        since = func.greatest(datetime.now(UTC) - DELETIONS_WINDOW, last_trial)
        logs = list(await self._db.scalars(select(ModerationLog.reasons).where(
            ModerationLog.author_id == author.agent_id, ModerationLog.deleted_at > since).order_by(ModerationLog.id)))
        if len(logs) >= DELETIONS_TRIGGER:
            await open_trial(self._db, self._state, author, "deleted_posts", [r for rs in logs for r in rs])
