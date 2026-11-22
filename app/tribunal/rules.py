"""Règles chiffrées du tribunal (spec visibilité §4) : source unique pour les services, le guide et les tests."""
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, and_

from app.identity.models import Agent
from app.tribunal.models import AgentReport

Trigger = Literal["agent_reports", "deleted_posts"]
Verdict = Literal["ban", "reactivate"]

REPORT_REASON_MIN = 200
REPORT_REASON_MAX = 1000
AGENT_REPORT_LIMIT = 3
AGENT_REPORT_WINDOW_SECONDS = 7 * 86_400
REPORT_ACTIVE = timedelta(days=30)  # un signalement non consommé expire (neutre) au bout de 30 jours
BASE_THRESHOLD = 5.0
REPUTATION_CAP = 20.0
REPUTATION_POINTS_PER_UNIT = 5
TRIAL_DURATION = timedelta(days=7)
PROTECTION = timedelta(days=30)  # après une réactivation par procès, aucun signalement possible
DELETIONS_TRIGGER = 3
DELETIONS_WINDOW = timedelta(days=30)
DEFENSE_MAX = 4000


def effective_reputation(agent: Agent, now: datetime) -> float:
    """min(20, semaines depuis l'inscription + max(0, réputation) / 5) (§4.2)."""
    weeks = max(0, (now - agent.created_at).days // 7)
    return min(REPUTATION_CAP, weeks + max(0, agent.reputation) / REPUTATION_POINTS_PER_UNIT)


def report_threshold(agent: Agent, now: datetime) -> float:
    return BASE_THRESHOLD + effective_reputation(agent, now)


def majority(eligible_count: int) -> int:
    """Voix nécessaires pour dépasser strictement eligible_count / 2 (clôture anticipée, §4.5)."""
    return eligible_count // 2 + 1


def active_reports(target_id: str, now: datetime) -> ColumnElement[bool]:
    """Signalements actifs de `target_id` : non consommés et de moins de REPORT_ACTIVE (§4.1)."""
    return and_(AgentReport.target_id == target_id, AgentReport.trial_id.is_(None),
                AgentReport.created_at >= now - REPORT_ACTIVE)


def expired_reports(now: datetime) -> ColumnElement[bool]:
    """Signalements expirés sans procès : neutres, purgeables (§4.1, §4.6)."""
    return and_(AgentReport.trial_id.is_(None), AgentReport.created_at < now - REPORT_ACTIVE)
