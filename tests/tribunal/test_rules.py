from datetime import UTC, datetime, timedelta

from app.identity.models import Agent
from app.tribunal.rules import (
    REPORT_REASON_MAX,
    REPORT_REASON_MIN,
    effective_reputation,
    majority,
    report_threshold,
)
from tests.tribunal.helpers import REASON

NOW = datetime(2026, 11, 22, tzinfo=UTC)


def _agent(age_days: int, reputation: int) -> Agent:
    return Agent(agent_id="x", handle="x", created_at=NOW - timedelta(days=age_days), reputation=reputation)


def test_effective_reputation_and_threshold() -> None:
    assert effective_reputation(_agent(0, 0), NOW) == 0
    assert effective_reputation(_agent(15, 10), NOW) == 2 + 10 / 5  # 2 semaines + réputation / 5
    assert effective_reputation(_agent(15, -50), NOW) == 2  # réputation négative ignorée
    assert effective_reputation(_agent(700, 500), NOW) == 20  # plafond
    assert report_threshold(_agent(0, 0), NOW) == 5  # une recrue : 5 signalements de poids 1
    assert report_threshold(_agent(700, 500), NOW) == 25  # un ancien apprécié : 25


def test_absolute_majority() -> None:
    assert [majority(n) for n in (0, 1, 4, 5)] == [1, 1, 3, 3]  # strictement plus de eligible_count / 2


def test_helper_reason_is_valid() -> None:
    assert REPORT_REASON_MIN <= len(REASON) <= REPORT_REASON_MAX
