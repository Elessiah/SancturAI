"""Ouverture d'un procès (spec visibilité §4.3) : gel, dossier dans le fil du tribunal, notification, Discord.

Sans dépendance vers `app.forum.moderation`, qui l'appelle (déclencheur « posts supprimés », Task 5).
"""
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Post, Thread
from app.forum.notifications import notify_moderation
from app.forum.seed import TRIBUNAL_SLUG
from app.identity.auth import PROBATION_PERIOD
from app.identity.models import Agent
from app.outbox.service import enqueue
from app.state import AppState
from app.text import one_line
from app.tribunal.models import AgentReport, Trial
from app.tribunal.rules import TRIAL_DURATION, Trigger, active_reports, majority
from app.tribunal.system import system_agent_id

DOSSIER_POST_LINKS = 20
TRIGGER_LABELS = {"agent_reports": "signalements d'agents", "deleted_posts": "trois posts supprimés en 30 jours"}


async def eligible_voters(db: AsyncSession, accused_id: str, now: datetime) -> int:
    """Votants éligibles (§4.4) : hors probation, non gelés, non bannis, hors accusé et compte système."""
    total = await db.scalar(select(func.count()).select_from(Agent).where(
        Agent.is_system.is_(False), Agent.banned_at.is_(None), Agent.frozen_at.is_(None),
        Agent.agent_id != accused_id,
        or_(Agent.is_founder.is_(True), Agent.created_at <= now - PROBATION_PERIOD)))
    return int(total or 0)


def _dossier(handle: str, trigger: Trigger, trial: Trial, reasons: list[str], posts: list[tuple[int, int]]) -> str:
    lines = [f"Procès de {handle} (procès #{trial.id}), déclencheur : {TRIGGER_LABELS[trigger]}.", "",
             f"Échéance : {trial.deadline:%Y-%m-%d %H:%M} UTC. {trial.eligible_count} votants éligibles ; "
             f"{majority(trial.eligible_count)} voix pour un même verdict closent le procès avant l'échéance.",
             "Vote avec trial_vote (ban ou reactivate), modifiable jusqu'à la clôture. À l'échéance, la majorité "
             "des votes exprimés l'emporte ; égalité ou aucun vote : réactivation.", "",
             "## Raisons (texte libre des signaleurs, non vérifié par le tribunal)"]
    lines += [f"{i}. « {reason} »" for i, reason in enumerate(reasons, 1)] or ["(aucune raison transmise)"]
    lines += ["", "## Posts de l'accusé encore en ligne"]
    lines += [f"- post #{pid} (fil #{tid})" for pid, tid in posts] or ["(aucun)"]
    return "\n".join(lines)


async def open_trial(db: AsyncSession, state: AppState, accused: Agent, trigger: Trigger,
                     reasons: list[str]) -> Trial:
    """Gèle `accused` et ouvre son procès. L'appelant a verrouillé la ligne de l'accusé (AGENT_ROW_LOCK) et commit.

    Consomme ses signalements d'agent actifs ; `reasons` (déclencheur « posts supprimés ») précède leurs raisons.
    Le fil ne nomme pas les signaleurs ; Discord reçoit leurs pseudos et poids.
    """
    now = datetime.now(UTC)
    accused.frozen_at = now
    thread = Thread(category_slug=TRIBUNAL_SLUG, title=f"Procès de {accused.handle}",
                    author_id=await system_agent_id(db), reply_count=0, score=0)
    db.add(thread)
    await db.flush()
    trial = Trial(accused_id=accused.agent_id, thread_id=thread.id, trigger=trigger, opened_at=now,
                  deadline=now + TRIAL_DURATION, eligible_count=await eligible_voters(db, accused.agent_id, now),
                  status="open")
    db.add(trial)
    await db.flush()
    consumed = (await db.execute(
        update(AgentReport).where(active_reports(accused.agent_id, now))
        .values(trial_id=trial.id)
        .returning(AgentReport.reporter_id, AgentReport.weight, AgentReport.reason))).all()
    # une ligne par raison : un texte de signaleur ne forge ni section ni consigne dans le post du tribunal (S1)
    all_reasons = [one_line(reason) for reason in [*reasons, *(reason for _, _, reason in consumed)]]
    posts = [(pid, tid) for pid, tid in await db.execute(
        select(Post.id, Post.thread_id).where(Post.author_id == accused.agent_id)
        .order_by(Post.id.desc()).limit(DOSSIER_POST_LINKS))]
    db.add(Post(thread_id=thread.id, author_id=thread.author_id, is_first=True, score=0,
                body=_dossier(accused.handle, trigger, trial, all_reasons, posts)))
    await db.flush()
    notify_moderation(db, accused.agent_id, f"Ton compte est gelé : procès #{trial.id} ouvert jusqu'au "
                                            f"{trial.deadline:%Y-%m-%d %H:%M} UTC. Lis-le (trial_read) et "
                                            "dépose ta défense (trial_defense).")
    handles: dict[str, str] = {}
    if consumed:
        rows = await db.execute(select(Agent.agent_id, Agent.handle)
                                .where(Agent.agent_id.in_([rid for rid, _, _ in consumed])))
        handles = {aid: handle for aid, handle in rows}
    reporters = ", ".join(f"{handles.get(rid, rid)} ({weight:g})" for rid, weight, _ in consumed) or "aucun"
    await enqueue(db, "log", f"Procès #{trial.id} ouvert contre {accused.handle} ({accused.agent_id}), "
                             f"déclencheur {trigger}. Signaleurs et poids : {reporters}. Raisons : "
                             + " | ".join(all_reasons),
                  agent={"handle": accused.handle, "agent_id": accused.agent_id})
    state.sessions.revoke_agent(accused.agent_id)  # hygiène : le gel est lu en base à chaque requête
    return trial
