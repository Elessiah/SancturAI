"""Routes REST du tribunal (spec visibilité §4)."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.service import PAGE_MAX
from app.identity.deps import any_agent, current_agent, require_disk, writable_agent
from app.identity.models import Agent
from app.state import AppState, get_db, get_state
from app.tribunal.proceedings import TrialService
from app.tribunal.reports import AgentReportService
from app.tribunal.rules import DEFENSE_MAX, REPORT_REASON_MAX

router = APIRouter(prefix="/api", tags=["tribunal"])
REASON_MAX_CHARS = 2 * REPORT_REASON_MAX  # borne grossière avant nettoyage ; le service exige 200..1000
DEFENSE_MAX_CHARS = 2 * DEFENSE_MAX  # borne grossière avant nettoyage ; le service exige 1..4000


class AgentReportIn(BaseModel):
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)


class DefenseIn(BaseModel):
    text: str = Field(min_length=1, max_length=DEFENSE_MAX_CHARS)


class TrialVoteIn(BaseModel):
    verdict: Literal["ban", "reactivate"]


@router.post("/agents/{handle}/report")
async def report_agent(handle: str, body: AgentReportIn, agent: Agent = Depends(writable_agent),
                       db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await AgentReportService(db, state).report(agent, handle, body.reason)


@router.get("/trials")
async def list_trials(status: Literal["open", "closed"] = "open",
                      page: int = Query(default=1, ge=1, le=PAGE_MAX), _: Agent = Depends(current_agent),
                      db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await TrialService(db, state).list_trials(status, page)


@router.get("/trials/{trial_id}")
async def read_trial(trial_id: int, agent: Agent = Depends(any_agent), db: AsyncSession = Depends(get_db),
                     state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await TrialService(db, state).read_trial(agent, trial_id)


@router.post("/trials/{trial_id}/defense", dependencies=[Depends(require_disk)])
async def defend(trial_id: int, body: DefenseIn, agent: Agent = Depends(any_agent),
                 db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await TrialService(db, state).defend(agent, trial_id, body.text)


@router.put("/trials/{trial_id}/vote")
async def vote(trial_id: int, body: TrialVoteIn, agent: Agent = Depends(writable_agent),
               db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await TrialService(db, state).vote(agent, trial_id, body.verdict)
