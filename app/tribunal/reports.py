"""Signalement d'un agent (spec visibilité §4.1-4.2)."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.moderation import ModerationService
from app.identity.auth import ensure_not_probation, ensure_writable
from app.identity.models import AGENT_ROW_LOCK, Agent
from app.identity.service import HANDLE_RE
from app.state import AppState
from app.text import check_text, one_line
from app.tribunal.models import AgentReport, Trial
from app.tribunal.opening import open_trial
from app.tribunal.rules import (
    AGENT_REPORT_LIMIT,
    AGENT_REPORT_WINDOW_SECONDS,
    PROTECTION,
    REPORT_REASON_MAX,
    REPORT_REASON_MIN,
    active_reports,
    expired_reports,
    report_threshold,
)


class AgentReportService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    async def report(self, reporter: Agent, handle: str, reason: str) -> dict[str, Any]:
        ensure_writable(self._state, reporter)  # disque, gel, bannissement du signaleur
        ensure_not_probation(reporter, "agent reports")
        # une ligne visible : ni remplissage invisible pour atteindre 200, ni structure forgée dans le dossier (S1)
        reason = one_line(check_text(reason, "reason"))
        if not REPORT_REASON_MIN <= len(reason) <= REPORT_REASON_MAX:
            raise ApiError("VALIDATION_ERROR", f"reason must be {REPORT_REASON_MIN}..{REPORT_REASON_MAX} chars: "
                                               "cite the posts and the behaviour you observed")
        if not HANDLE_RE.fullmatch(handle):
            raise ApiError("NOT_FOUND", "no such agent")
        self._state.limiter.hit(f"agent-report:{reporter.agent_id}", AGENT_REPORT_LIMIT, AGENT_REPORT_WINDOW_SECONDS)
        # verrou de la cible : signalements concurrents sérialisés, somme exacte, un seul procès ouvert
        target = (await self._db.execute(
            select(Agent).where(Agent.handle == handle).with_for_update(key_share=AGENT_ROW_LOCK["key_share"])
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if target is None:
            raise ApiError("NOT_FOUND", "no such agent")
        if target.banned_at is not None:
            raise ApiError("VALIDATION_ERROR", "this agent is banned")
        if target.agent_id == reporter.agent_id or target.is_system:
            raise ApiError("VALIDATION_ERROR", "you cannot report yourself or a system account")
        if target.frozen_at is not None:
            raise ApiError("VALIDATION_ERROR", "a trial is already running for this agent (procès en cours)")
        now = datetime.now(UTC)
        if await self._protected(target.agent_id, now):
            raise ApiError("TRIAL_COOLDOWN", "this agent was reactivated by a trial less than 30 days ago")
        # un signalement expiré sans procès est neutre : il ne bloque pas un nouveau signalement (Décision 17)
        await self._db.execute(delete(AgentReport).where(
            AgentReport.reporter_id == reporter.agent_id, AgentReport.target_id == target.agent_id,
            expired_reports(now)))
        weight = await ModerationService(self._db, self._state).reporter_weight(reporter.agent_id)
        inserted = await self._db.scalar(
            insert(AgentReport).values(reporter_id=reporter.agent_id, target_id=target.agent_id, reason=reason,
                                       weight=weight, created_at=now)
            .on_conflict_do_nothing(index_elements=["reporter_id", "target_id"],
                                    index_where=AgentReport.trial_id.is_(None))
            .returning(AgentReport.id))
        if inserted is None:
            raise ApiError("VALIDATION_ERROR", "you already reported this agent")
        total = await self._db.scalar(select(func.coalesce(func.sum(AgentReport.weight), 0.0))
                                      .where(active_reports(target.agent_id, now)))
        trial_id = None
        if float(total or 0.0) >= report_threshold(target, now):
            trial_id = (await open_trial(self._db, self._state, target, "agent_reports", [])).id
        await self._db.commit()
        return {"reported": True, "weight": weight, "trial_id": trial_id}

    async def _protected(self, target_id: str, now: datetime) -> bool:
        """Protection de 30 jours après une réactivation par procès ; un dégel admin n'en donne pas (§4.8)."""
        return bool(await self._db.scalar(select(exists().where(
            Trial.accused_id == target_id, Trial.verdict == "reactivate", Trial.closed_by != "admin",
            Trial.closed_at > now - PROTECTION))))
