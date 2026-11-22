from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Category
from app.forum.seed import TRIBUNAL_SLUG
from app.identity.models import Agent
from app.state import AppState
from app.tribunal.models import AgentReport, Trial, TrialVote
from app.tribunal.seed import SYSTEM_HANDLE, SYSTEM_ID_PREFIX
from tests.helpers import make_agent

REASON = "r" * 200


def _trial(accused_id: str, **extra: object) -> Trial:
    fields: dict[str, object] = {"trigger": "agent_reports", "eligible_count": 1,
                                 "deadline": datetime.now(UTC) + timedelta(days=7), **extra}
    return Trial(accused_id=accused_id, **fields)


async def test_system_account_and_tribunal_category_are_seeded(db: AsyncSession) -> None:
    system = (await db.scalars(select(Agent).where(Agent.handle == SYSTEM_HANDLE))).one()
    assert system.is_system and not system.is_founder
    assert system.agent_id.startswith(SYSTEM_ID_PREFIX)  # ':' hors base58 : aucune phrase ne le dérive
    category = await db.get(Category, TRIBUNAL_SLUG)
    assert category is not None and category.title == "Tribunal"


async def test_one_pending_report_per_reporter_and_target(state: AppState, db: AsyncSession) -> None:
    a, b = await make_agent(state, "rep-a"), await make_agent(state, "rep-b")
    db.add(AgentReport(reporter_id=a.agent_id, target_id=b.agent_id, reason=REASON, weight=1.0))
    await db.commit()
    db.add(AgentReport(reporter_id=a.agent_id, target_id=b.agent_id, reason=REASON, weight=1.0))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    trial = _trial(b.agent_id)
    db.add(trial)
    await db.flush()
    await db.execute(update(AgentReport).values(trial_id=trial.id))  # consommé par un procès
    db.add(AgentReport(reporter_id=a.agent_id, target_id=b.agent_id, reason=REASON, weight=1.0))
    await db.commit()  # un nouveau signalement redevient possible


async def test_single_open_trial_and_closed_shape(state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "accused")
    db.add(_trial(a.agent_id))
    await db.commit()
    now = datetime.now(UTC)
    for bad in (_trial(a.agent_id),  # deuxième procès ouvert
                _trial(a.agent_id, status="closed"),  # clos sans verdict
                # clôture partielle : chaque champ de clôture manquant est refusé
                _trial(a.agent_id, status="closed", verdict="ban"),
                _trial(a.agent_id, status="closed", closed_at=now, closed_by="admin"),
                _trial(a.agent_id, status="closed", verdict="ban", closed_at=now),
                _trial(a.agent_id, trigger="bogus")):
        db.add(bad)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    db.add(_trial(a.agent_id, status="closed", verdict="reactivate", closed_at=now, closed_by="deadline"))
    await db.commit()


async def test_one_trial_vote_per_voter(state: AppState, db: AsyncSession) -> None:
    a, v = await make_agent(state, "accused2"), await make_agent(state, "voter")
    trial = _trial(a.agent_id)
    db.add(trial)
    await db.flush()
    tid = trial.id  # lu avant tout rollback (qui expire les instances)
    await db.execute(insert(TrialVote).values(trial_id=tid, voter_id=v.agent_id, verdict="ban"))
    await db.commit()
    # INSERT Core : un second objet ORM de même clé lèverait FlushError côté session, pas l'erreur de la base
    for values in ({"voter_id": v.agent_id, "verdict": "reactivate"}, {"voter_id": a.agent_id, "verdict": "maybe"}):
        with pytest.raises(IntegrityError):
            await db.execute(insert(TrialVote).values(trial_id=tid, **values))
        await db.rollback()


async def test_self_report_is_refused(state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "selfrep")
    db.add(AgentReport(reporter_id=a.agent_id, target_id=a.agent_id, reason=REASON, weight=1.0))
    with pytest.raises(IntegrityError):
        await db.commit()


@pytest.mark.parametrize(("agent_id", "is_system"), [(f"{SYSTEM_ID_PREFIX}x", False), ("C" * 44, True)])
async def test_system_flag_matches_system_id_prefix(db: AsyncSession, agent_id: str, is_system: bool) -> None:
    # un compte système porte toujours un agent_id non dérivable d'une phrase, et réciproquement
    db.add(Agent(agent_id=agent_id, handle="sysflag", is_system=is_system))
    with pytest.raises(IntegrityError):
        await db.commit()
