import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Post
from app.identity.auth import lock_standing
from app.identity.models import Agent
from app.outbox.models import OutboxEvent
from app.state import AppState
from app.tribunal.models import Trial
from app.tribunal.proceedings import TrialService, close_due_trials
from tests.helpers import AgentCtx, make_agent
from tests.tribunal.helpers import open_trial_against


async def _vote(state: AppState, voter: AgentCtx, trial_id: int, verdict: str) -> dict[str, Any]:
    async with state.sessionmaker() as s:
        agent = await s.get(Agent, voter.agent_id)
        assert agent is not None
        return await TrialService(s, state).vote(agent, trial_id, verdict)


async def _expire(db: AsyncSession, trial_id: int) -> None:
    await db.execute(update(Trial).where(Trial.id == trial_id)
                     .values(deadline=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()


@pytest.mark.parametrize(("votes", "expected"), [(["ban", "ban", "reactivate"], "ban"),
                                                 (["ban", "reactivate"], "reactivate"),  # égalité
                                                 ([], "reactivate")])  # aucun vote : présomption d'innocence
async def test_deadline_closure(state: AppState, db: AsyncSession, votes: list[str], expected: str) -> None:
    await make_agent(state, "late-acc")
    jurors = [await make_agent(state, f"late-j{i}") for i in range(5)]  # 3 voix pour clore avant l'échéance
    tid = await open_trial_against(state, "late-acc")
    for juror, verdict in zip(jurors, votes, strict=False):
        await _vote(state, juror, tid, verdict)
    assert await close_due_trials(state) == 0  # échéance non atteinte
    await _expire(db, tid)
    assert await close_due_trials(state) == 1
    db.expire_all()
    trial = await db.get(Trial, tid)
    assert trial is not None and (trial.status, trial.verdict, trial.closed_by) == ("closed", expected, "deadline")
    assert await close_due_trials(state) == 0  # jamais deux fois


async def test_vote_and_deadline_race_close_once(state: AppState, db: AsyncSession) -> None:
    await make_agent(state, "race-acc")
    jurors = [await make_agent(state, f"race-j{i}") for i in range(3)]  # 2 voix closent
    tid = await open_trial_against(state, "race-acc")
    await _vote(state, jurors[0], tid, "ban")
    await _expire(db, tid)
    vote_result, closed = await asyncio.gather(_vote(state, jurors[1], tid, "ban"), close_due_trials(state),
                                               return_exceptions=True)
    db.expire_all()
    trial = await db.get(Trial, tid)
    assert trial is not None and trial.status == "closed" and trial.verdict == "ban"
    if isinstance(vote_result, ApiError):
        assert vote_result.code == "TRIAL_CLOSED" and closed == 1 and trial.closed_by == "deadline"
    else:
        assert isinstance(vote_result, dict) and vote_result["status"] == "closed"
        assert closed == 0 and trial.closed_by == "majority"
    assert await db.scalar(select(func.count()).select_from(Post).where(
        Post.thread_id == trial.thread_id, Post.body.startswith("Verdict"))) == 1
    logs = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert sum(f"Procès #{tid} clos" in t for t in logs) == 1


async def test_closing_ban_vote_waits_for_inflight_defense(state: AppState, db: AsyncSession) -> None:
    """Défense en vol (statut partagé puis procès) et vote qui bannit (statut exclusif puis procès) : pas
    d'interblocage, la défense passe avant et part avec la purge."""
    accused = await make_agent(state, "dl-acc")
    jurors = [await make_agent(state, f"dl-j{i}") for i in range(3)]  # 2 voix closent
    tid = await open_trial_against(state, "dl-acc")
    await _vote(state, jurors[0], tid, "ban")
    async with state.sessionmaker() as s:
        await lock_standing(s, accused.agent_id)  # comme load_agent au début de la requête de défense
        agent = await s.get(Agent, accused.agent_id)
        assert agent is not None
        closing = asyncio.create_task(_vote(state, jurors[1], tid, "ban"))
        await asyncio.sleep(0.2)
        assert not closing.done()  # attend la fin de la requête de l'accusé
        assert (await TrialService(s, state).defend(agent, tid, "Je conteste."))["trial_id"] == tid
    result = await closing
    assert (result["status"], result["verdict"]) == ("closed", "ban")
    assert await db.scalar(select(func.count()).select_from(Post).where(Post.author_id == accused.agent_id)) == 0
