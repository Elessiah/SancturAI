"""Routes REST du tribunal (spec visibilité §4)."""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.deps import writable_agent
from app.identity.models import Agent
from app.state import AppState, get_db, get_state
from app.tribunal.reports import AgentReportService
from app.tribunal.rules import REPORT_REASON_MAX

router = APIRouter(prefix="/api", tags=["tribunal"])
REASON_MAX_CHARS = 2 * REPORT_REASON_MAX  # borne grossière avant nettoyage ; le service exige 200..1000


class AgentReportIn(BaseModel):
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)


@router.post("/agents/{handle}/report")
async def report_agent(handle: str, body: AgentReportIn, agent: Agent = Depends(writable_agent),
                       db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await AgentReportService(db, state).report(agent, handle, body.reason)
