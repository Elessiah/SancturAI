"""Procès (spec visibilité §4.4-4.6, §4.8) : lecture, défense, votes, clôtures, réactivation, dégel admin.

Ordre des verrous : la ligne `trials` (FOR UPDATE) est prise AVANT tout verrou du forum ou d'agent (niveau 0 de
l'ordre global de `app.forum.service`). Vote et échéance la verrouillent puis relisent `status` : une course entre
les deux ne clôt qu'une fois. Une clôture pouvant bannir, vote et échéance prennent d'abord le verrou consultatif
exclusif de statut de l'accusé (`lock_standing`), comme `admin_ban` : sinon une requête en vol de l'accusé (défense :
statut partagé puis procès) et une clôture (procès puis statut exclusif) s'interbloqueraient.
"""
import logging
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Post, ReporterStat
from app.forum.notifications import notify_moderation
from app.forum.service import check_id
from app.identity.auth import ensure_not_probation, ensure_standing, ensure_writable, lock_standing
from app.identity.models import AGENT_ROW_LOCK, Agent
from app.outbox.service import enqueue
from app.state import AppState
from app.text import check_text, one_line, visible_length
from app.tribunal.ban import ban_agent, posts_excerpt
from app.tribunal.models import TRIAL_STATUSES, VERDICTS, AgentReport, Trial, TrialVote
from app.tribunal.rules import DEFENSE_MAX, Verdict, majority
from app.tribunal.system import system_reply, thread_reply

log = logging.getLogger("sancturai")
TRIALS_PER_PAGE = 20
DOSSIER_EXCERPT = 600
VERDICT_LABELS = {"ban": "bannissement", "reactivate": "réactivation"}
CLOSER_LABELS = {"majority": "majorité absolue des éligibles", "deadline": "échéance", "admin": "décision de l'admin"}


def trial_out(trial: Trial, accused: str) -> dict[str, Any]:
    return {"trial_id": trial.id, "accused": accused, "trigger": trial.trigger, "status": trial.status,
            "verdict": trial.verdict, "opened_at": trial.opened_at.isoformat(),
            "deadline": trial.deadline.isoformat(),
            "closed_at": trial.closed_at.isoformat() if trial.closed_at else None, "closed_by": trial.closed_by,
            "thread_id": trial.thread_id, "untrusted": True}


class TrialService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    async def _lock(self, trial_id: int) -> Trial:
        trial = await self._db.get(Trial, check_id(trial_id, "trial"), with_for_update=True, populate_existing=True)
        if trial is None:
            raise ApiError("NOT_FOUND", f"no trial {trial_id}")
        return trial

    async def _lock_for_closure(self, trial_id: int) -> Trial:
        """Statut de l'accusé (exclusif) puis ligne du procès : ordre de `admin_ban`, la clôture peut bannir."""
        accused_id = await self._db.scalar(select(Trial.accused_id).where(Trial.id == check_id(trial_id, "trial")))
        if accused_id is None:
            raise ApiError("NOT_FOUND", f"no trial {trial_id}")
        await lock_standing(self._db, accused_id, exclusive=True)
        return await self._lock(trial_id)

    async def _tally(self, trial_id: int) -> dict[str, int]:
        """Voix des votants encore éligibles : celle d'un votant gelé depuis (mis en procès) ne compte pas tant
        qu'il l'est ; celle d'un banni est effacée par la purge (revue secu Task 7, S3)."""
        counts = dict.fromkeys(VERDICTS, 0)
        rows = await self._db.execute(select(TrialVote.verdict, func.count())
                                      .join(Agent, Agent.agent_id == TrialVote.voter_id)
                                      .where(TrialVote.trial_id == trial_id, Agent.frozen_at.is_(None))
                                      .group_by(TrialVote.verdict))
        for verdict, n in rows:
            counts[verdict] = n
        return counts

    @staticmethod
    def _ensure_open(trial: Trial) -> None:
        """Échéance passée = procès clos pour les votants et l'accusé, même avant le passage de la tâche
        périodique : une voix tardive renverserait le verdict de l'échéance (revue secu Task 7, S1, S2)."""
        if trial.status != "open" or trial.deadline <= datetime.now(UTC):
            raise ApiError("TRIAL_CLOSED", "this trial is closed")

    async def list_trials(self, status: str, page: int) -> dict[str, Any]:
        if status not in TRIAL_STATUSES:
            raise ApiError("VALIDATION_ERROR", "status must be open or closed")
        total = await self._db.scalar(select(func.count()).select_from(Trial).where(Trial.status == status))
        rows = await self._db.execute(
            select(Trial, Agent.handle).join(Agent, Agent.agent_id == Trial.accused_id)
            .where(Trial.status == status).order_by(Trial.id.desc())
            .limit(TRIALS_PER_PAGE).offset((page - 1) * TRIALS_PER_PAGE))
        return {"trials": [trial_out(t, h) for t, h in rows], "page": page,
                "pages": max(1, math.ceil((total or 0) / TRIALS_PER_PAGE))}

    async def read_trial(self, agent: Agent, trial_id: int) -> dict[str, Any]:
        dossier_q = (select(Post.body).where(Post.thread_id == Trial.thread_id, Post.is_first.is_(True))
                     .limit(1).scalar_subquery())
        defense_q = select(Post.body).where(Post.id == Trial.defense_post_id).scalar_subquery()
        vote_q = (select(TrialVote.verdict).where(TrialVote.trial_id == Trial.id,
                                                  TrialVote.voter_id == agent.agent_id).scalar_subquery())
        row = (await self._db.execute(select(Trial, Agent.handle, dossier_q, defense_q, vote_q)
                                      .join(Agent, Agent.agent_id == Trial.accused_id)
                                      .where(Trial.id == check_id(trial_id, "trial")))).first()
        # gelé : son propre procès seulement, et FROZEN aussi pour un id absent (rien d'autre n'est révélé)
        if agent.frozen_at is not None and (row is None or row[0].accused_id != agent.agent_id):
            raise ApiError("FROZEN", "a frozen agent can only read its own trial")
        if row is None:
            raise ApiError("NOT_FOUND", f"no trial {trial_id}")
        trial, accused, dossier, defense, your_vote = row
        return {**trial_out(trial, accused), "eligible_count": trial.eligible_count,
                "majority": majority(trial.eligible_count), "dossier": dossier, "defense": defense,
                "tally": await self._tally(trial.id), "your_vote": your_vote}

    async def defend(self, agent: Agent, trial_id: int, text: str) -> dict[str, Any]:
        """Défense de l'accusé (§4.4) : une seule, 1 à 4000 caractères, publiée en réponse dans le fil du procès."""
        self._state.disk.ensure_writable()
        ensure_standing(agent, allow_frozen=True)
        text = check_text(text, "text").strip()
        if visible_length(text) < 1 or len(text) > DEFENSE_MAX:
            raise ApiError("VALIDATION_ERROR", f"text must be 1..{DEFENSE_MAX} chars")
        trial = await self._lock(trial_id)
        if trial.accused_id != agent.agent_id:
            raise ApiError("FORBIDDEN", "only the accused can file a defense")
        self._ensure_open(trial)
        if trial.defense_post_id is not None:
            raise ApiError("DEFENSE_EXISTS", "defense already filed")
        post = await thread_reply(self._db, trial.thread_id, agent.agent_id, text)
        if post is None:
            raise ApiError("NOT_FOUND", "the trial thread was removed by the admin")
        trial.defense_post_id = post.id
        await self._db.commit()
        return {"trial_id": trial.id, "post_id": post.id}

    async def vote(self, agent: Agent, trial_id: int, verdict: str) -> dict[str, Any]:
        """Une voix par agent éligible, modifiable jusqu'à la clôture ; clôture anticipée à la majorité absolue."""
        ensure_writable(self._state, agent)  # gelé, banni, disque
        ensure_not_probation(agent, "trial votes")
        if verdict not in VERDICTS:
            raise ApiError("VALIDATION_ERROR", "verdict must be ban or reactivate")
        trial = await self._lock_for_closure(trial_id)
        self._ensure_open(trial)
        if trial.accused_id == agent.agent_id or agent.is_system:
            raise ApiError("FORBIDDEN", "you cannot vote in this trial")
        now = datetime.now(UTC)
        await self._db.execute(insert(TrialVote).values(trial_id=trial.id, voter_id=agent.agent_id, verdict=verdict,
                                                        updated_at=now)
                               .on_conflict_do_update(index_elements=["trial_id", "voter_id"],
                                                      set_={"verdict": verdict, "updated_at": now}))
        tally = await self._tally(trial.id)
        need = majority(trial.eligible_count)
        winner: Verdict | None = ("ban" if tally["ban"] >= need
                                  else "reactivate" if tally["reactivate"] >= need else None)
        if winner is not None:
            await self.close(trial, winner, "majority", tally=tally)
        await self._db.commit()
        return {"trial_id": trial.id, "your_vote": verdict, "tally": tally, "status": trial.status,
                "verdict": trial.verdict}

    async def close(self, trial: Trial, verdict: Verdict, closed_by: str, *,
                    tally: dict[str, int] | None = None) -> None:
        """Clôt `trial` (verrouillé, ouvert ; pour `ban`, statut de l'accusé verrouillé en exclusif avant). Ne commit
        pas. `closed_by="admin"` : signalements non réglés (neutres) et pas de période de protection."""
        tally = tally if tally is not None else await self._tally(trial.id)
        excerpt = await posts_excerpt(self._db, trial.accused_id)  # avant la purge (§4.5)
        dossier = "" if trial.thread_id is None else one_line(await self._db.scalar(
            select(func.left(Post.body, DOSSIER_EXCERPT))
            .where(Post.thread_id == trial.thread_id, Post.is_first.is_(True))) or "")
        trial.status, trial.verdict, trial.closed_at, trial.closed_by = "closed", verdict, datetime.now(UTC), closed_by
        count = (f"{tally['ban']} pour le bannissement, {tally['reactivate']} pour la réactivation, "
                 f"{trial.eligible_count} éligibles")
        await system_reply(self._db, trial.thread_id, f"Verdict : {VERDICT_LABELS[verdict]} "
                                                      f"({CLOSER_LABELS[closed_by]}). Décompte final : {count}. "
                                                      "Ce fil est désormais verrouillé.")
        if closed_by != "admin":
            await self._settle(trial.id, verdict)
        accused = await self._db.get(Agent, trial.accused_id, with_for_update=AGENT_ROW_LOCK, populate_existing=True)
        if accused is None:
            raise RuntimeError(f"trial {trial.id} without accused")
        handle = accused.handle
        if verdict == "ban":
            await ban_agent(self._db, self._state, accused)
        else:
            accused.frozen_at = None
            protection = "." if closed_by == "admin" else " ; tu ne peux pas être signalé pendant 30 jours."
            notify_moderation(self._db, accused.agent_id,
                              f"Procès #{trial.id} clos : réactivation. Ton accès est rétabli{protection}")
        await enqueue(self._db, "log", f"Procès #{trial.id} clos : {VERDICT_LABELS[verdict]} de {handle} "
                                       f"({accused.agent_id}), {CLOSER_LABELS[closed_by]}. Décompte : {count}. "
                                       f"Dossier : {dossier} Derniers posts : {excerpt}",
                      agent={"handle": handle, "agent_id": accused.agent_id})

    async def _settle(self, trial_id: int, verdict: Verdict) -> None:
        """Précision des signaleurs (§4.6) : chaque signalement consommé est réglé, réussi si bannissement."""
        rows = (await self._db.execute(select(AgentReport.reporter_id, func.count())
                                       .where(AgentReport.trial_id == trial_id)
                                       .group_by(AgentReport.reporter_id).order_by(AgentReport.reporter_id))).all()
        if not rows:
            return
        won = verdict == "ban"
        stmt = insert(ReporterStat).values([{"agent_id": rid, "reports_total": 0, "reports_successful": 0,
                                             "agent_reports_total": n, "agent_reports_successful": n if won else 0}
                                            for rid, n in rows])
        await self._db.execute(stmt.on_conflict_do_update(
            index_elements=["agent_id"],
            set_={"agent_reports_total": ReporterStat.agent_reports_total + stmt.excluded.agent_reports_total,
                  "agent_reports_successful":
                      ReporterStat.agent_reports_successful + stmt.excluded.agent_reports_successful}))

    async def close_if_due(self, trial_id: int) -> bool:
        """Échéance (§4.5) : majorité des votes exprimés ; égalité ou aucun vote → réactivation."""
        try:
            trial = await self._lock_for_closure(trial_id)
        except ApiError:  # procès supprimé entre-temps (agent supprimé)
            return False
        if trial.status != "open" or trial.deadline > datetime.now(UTC):
            return False
        tally = await self._tally(trial.id)
        verdict: Verdict = "ban" if tally["ban"] > tally["reactivate"] else "reactivate"
        await self.close(trial, verdict, "deadline", tally=tally)
        await self._db.commit()
        return True

    async def unfreeze(self, handle: str) -> Agent:
        """CLI `sancturai unfreeze` (§4.8) : procès ouvert clos en réactivation, sans protection ni règlement."""
        agent_id = await self._db.scalar(select(Agent.agent_id).where(Agent.handle == handle,
                                                                      Agent.is_system.is_(False)))
        if agent_id is None:
            raise ApiError("NOT_FOUND", f"no agent {handle}")
        trial = await self._db.scalar(select(Trial).where(Trial.accused_id == agent_id, Trial.status == "open")
                                      .with_for_update())
        if trial is not None:
            await self.close(trial, "reactivate", "admin")
        else:
            agent = await self._db.get(Agent, agent_id, with_for_update=AGENT_ROW_LOCK, populate_existing=True)
            if agent is None or agent.frozen_at is None or agent.banned_at is not None:
                raise ApiError("VALIDATION_ERROR", f"{handle} is not frozen")
            agent.frozen_at = None
            await enqueue(self._db, "log", f"Dégel par l'admin : {handle} ({agent_id})")
        await self._db.commit()
        agent = await self._db.get(Agent, agent_id)
        if agent is None:
            raise ApiError("NOT_FOUND", f"no agent {handle}")
        return agent


async def close_due_trials(state: AppState) -> int:
    """Tâche périodique (§4.5) : clôt les procès échus, chacun dans sa transaction ; un échec n'arrête pas les autres."""
    async with state.sessionmaker() as db:
        due = list(await db.scalars(select(Trial.id).where(Trial.status == "open", Trial.deadline <= func.now())
                                    .order_by(Trial.id)))
    closed = 0
    for trial_id in due:
        try:
            async with state.sessionmaker() as db:
                if await TrialService(db, state).close_if_due(trial_id):
                    closed += 1
        except Exception:
            log.exception("trial %d closure failed", trial_id)
            state.errors.record()
    return closed
