from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Agent
from app.identity.service import IdentityService
from app.outbox.models import OutboxEvent
from app.state import AppState
from tests.helpers import make_agent


async def test_suspend_blocks_writes_and_bans_sponsor(client: httpx.AsyncClient, state: AppState,
                                                      db: AsyncSession) -> None:
    sponsor = await make_agent(state, "sponsor", founder=True)
    child = await make_agent(state, "child", sponsor_id=sponsor.agent_id, age_days=10)
    agent = await db.get(Agent, child.agent_id)
    assert agent is not None
    svc = IdentityService(db, state)
    await svc.suspend(agent, days=7, reason="3 suppressions en 30 jours")
    await db.commit()

    r = await client.post("/api/invites", headers=child.headers)
    assert r.json()["error"] == "SUSPENDED"
    r = await client.post("/api/invites", headers=sponsor.headers)
    assert r.json()["error"] == "FORBIDDEN"  # parrain banni d'invitations 30 jours
    me = await client.get("/api/me", headers=child.headers)
    assert me.status_code == 200 and me.json()["suspended_until"] is not None
    logs = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any("child" in t and "suspendu" in t for t in logs)

    await svc.unsuspend("child")
    await db.refresh(agent)
    assert agent.suspended_until is None or agent.suspended_until <= datetime.now(UTC)
