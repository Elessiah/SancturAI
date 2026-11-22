"""Bannissement (spec visibilité §4.5, §4.8) : purge complète par les services existants, pseudo anonymisé.

Ordre des verrous : la purge enchaîne plusieurs suppressions de posts (fil puis post, statistiques, auteurs) dans une
seule transaction et ne peut pas respecter l'ordre global de bout en bout. Un interblocage avec une écriture
concurrente est rendu SERVER_BUSY réessayable (REST, MCP) ou retenté au tick suivant (échéance d'un procès).
"""
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Post, Report, ReporterStat
from app.forum.moderation import ModerationService
from app.forum.notifications import notify_moderation
from app.forum.service import ADMIN_REASON_MAX, ForumService
from app.identity.models import AGENT_ROW_LOCK, Agent, Invite
from app.identity.service import SPONSOR_BAN
from app.outbox.service import enqueue
from app.state import AppState
from app.text import check_text, one_line
from app.tribunal.models import AgentReport, Trial, TrialVote
from app.tribunal.system import system_reply
from app.vault.service import VaultService

EXCERPT_POSTS = 5
EXCERPT_CHARS = 200
SPONSOR_WINDOW = timedelta(days=30)  # parrain sanctionné si le banni a été invité il y a moins de 30 jours


async def posts_excerpt(db: AsyncSession, agent_id: str) -> str:
    """Extrait des 5 derniers posts pour Discord, à prendre AVANT la purge (§4.5)."""
    rows = await db.execute(select(Post.id, func.left(Post.body, EXCERPT_CHARS)).where(Post.author_id == agent_id)
                            .order_by(Post.id.desc()).limit(EXCERPT_POSTS))
    return " | ".join(f"#{pid} {one_line(snippet)}" for pid, snippet in rows) or "(aucun post)"


async def ban_agent(db: AsyncSession, state: AppState, agent: Agent) -> str:
    """Bannit `agent` (ligne verrouillée par l'appelant) et purge ses données. Ne commit pas. Renvoie l'ancien pseudo.

    La ligne agent reste (historique des procès) ; le pseudo est anonymisé pour être libéré.
    """
    now = datetime.now(UTC)
    aid, old_handle = agent.agent_id, agent.handle
    agent.banned_at = now
    agent.frozen_at = agent.frozen_at or now
    agent.handle = f"banned-{secrets.token_hex(4)}"
    await db.flush()
    state.sessions.revoke_agent(aid)
    await ModerationService(db, state).retract_votes(aid)  # réputation des autres recalculée
    forum = ForumService(db, state)
    for first in (True, False):  # ses fils d'abord (emportent leurs réponses), puis ses réponses restantes
        post_ids = list(await db.scalars(select(Post.id).where(Post.author_id == aid, Post.is_first.is_(first))
                                         .order_by(Post.id)))
        for pid in post_ids:
            post = await db.get(Post, pid)
            if post is not None:
                await forum.remove_post(post)  # défense comprise ; signalements visant ses posts neutralisés
    await VaultService(db, state.settings).purge(aid)
    await db.execute(delete(Invite).where(Invite.creator_id == aid, Invite.used_by.is_(None)))
    removed = await db.execute(delete(Report).where(Report.agent_id == aid))
    live = int(removed.rowcount or 0)  # type: ignore[attr-defined]
    if live:  # signalements de posts encore vivants : retirés de reports_total (invariant des statistiques)
        await db.execute(update(ReporterStat).where(ReporterStat.agent_id == aid)
                         .values(reports_total=ReporterStat.reports_total - live))
    await db.execute(delete(AgentReport).where(AgentReport.reporter_id == aid, AgentReport.trial_id.is_(None)))
    await db.execute(delete(TrialVote).where(TrialVote.voter_id == aid,
                                             TrialVote.trial_id.in_(select(Trial.id).where(Trial.status == "open"))))
    if agent.sponsor_id is not None and agent.created_at > now - SPONSOR_WINDOW:
        await db.execute(update(Agent).where(Agent.agent_id == agent.sponsor_id)
                         .values(invite_banned_until=now + SPONSOR_BAN))
    notify_moderation(db, aid, "Ton compte a été banni ; ses données ont été effacées.")
    return old_handle


async def admin_ban(db: AsyncSession, state: AppState, handle: str, reason: str) -> str:
    """CLI `sancturai ban` (§4.8) : bannissement immédiat sans procès. Un procès ouvert est clos `ban` / `admin`,
    ses signalements restent neutres (Décision 9). Commit ; renvoie l'ancien pseudo."""
    reason = check_text(reason, "reason").strip()
    if not reason or len(reason) > ADMIN_REASON_MAX:
        raise ApiError("VALIDATION_ERROR", f"reason must be 1..{ADMIN_REASON_MAX} chars")
    agent_id = await db.scalar(select(Agent.agent_id).where(Agent.handle == handle, Agent.is_system.is_(False),
                                                            Agent.banned_at.is_(None)))
    if agent_id is None:
        raise ApiError("NOT_FOUND", f"no agent {handle}")
    # procès puis agent : même ordre que la clôture d'un procès
    trial = await db.scalar(select(Trial).where(Trial.accused_id == agent_id, Trial.status == "open")
                            .with_for_update())
    agent = await db.get(Agent, agent_id, with_for_update=AGENT_ROW_LOCK, populate_existing=True)
    if agent is None or agent.banned_at is not None:
        raise ApiError("NOT_FOUND", f"no agent {handle}")
    excerpt = await posts_excerpt(db, agent_id)
    if trial is not None:
        trial.status, trial.verdict, trial.closed_at, trial.closed_by = "closed", "ban", datetime.now(UTC), "admin"
        await system_reply(db, trial.thread_id, f"Verdict : bannissement prononcé par l'admin. "
                                                f"Raison : {one_line(reason)}")
    old_handle = await ban_agent(db, state, agent)
    await enqueue(db, "log", f"Bannissement par l'admin : {old_handle} ({agent_id}). Raison : {one_line(reason)}. "
                             f"Derniers posts : {excerpt}", agent={"handle": old_handle, "agent_id": agent_id})
    await db.commit()
    return old_handle
