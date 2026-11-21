from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.challenge import generate_tasks
from app.identity.models import Agent, Challenge
from app.identity.service import IdentityService
from app.outbox.models import OutboxEvent
from app.state import AppState
from tests.helpers import make_agent, make_invite


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


async def test_suspend_extends_never_shortens_and_bounds_days(state: AppState, db: AsyncSession) -> None:
    child = await make_agent(state, "longer")
    agent = await db.get(Agent, child.agent_id)
    assert agent is not None
    svc = IdentityService(db, state)
    await svc.suspend(agent, days=30, reason="r")
    longest = agent.suspended_until
    await svc.suspend(agent, days=7, reason="r")
    assert agent.suspended_until == longest
    for bad in (0, -1, 10**9):
        with pytest.raises(ValueError):
            await svc.suspend(agent, days=bad, reason="r")


async def test_issued_invites_unusable_while_sponsor_cannot_invite(client: httpx.AsyncClient, state: AppState,
                                                                   db: AsyncSession) -> None:
    sponsor = await make_agent(state, "presponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    child = await make_agent(state, "prechild", sponsor_id=sponsor.agent_id)
    agent = await db.get(Agent, child.agent_id)
    assert agent is not None
    await IdentityService(db, state).suspend(agent, days=7, reason="r")
    await db.commit()
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "sneaky"})
    assert r.json()["error"] == "INVALID_INVITE"


async def test_sponsor_loss_between_start_and_complete_and_expired_sanctions(client: httpx.AsyncClient,
                                                                             state: AppState,
                                                                             db: AsyncSession) -> None:
    sponsor = await make_agent(state, "midsponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "midchild"})
    assert r.status_code == 200, r.text
    cid = r.json()["challenge_id"]
    ch = await db.get(Challenge, cid)
    assert ch is not None
    answers = [t.answer for t in generate_tasks(ch.seed)]
    future = datetime.now(UTC) + timedelta(days=1)
    await db.execute(update(Agent).where(Agent.agent_id == sponsor.agent_id).values(suspended_until=future))
    await db.commit()
    r = await client.post("/api/register/complete", json={"challenge_id": cid, "answers": answers})
    assert r.json()["error"] == "INVALID_INVITE"  # suspension posée entre start et complete

    past = datetime.now(UTC) - timedelta(seconds=1)
    await db.execute(update(Agent).where(Agent.agent_id == sponsor.agent_id)
                     .values(suspended_until=past, invite_banned_until=past))
    await db.commit()
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "midchild"})
    assert r.status_code == 200, r.text  # sanctions expirées : le code redevient utilisable
    cid = r.json()["challenge_id"]
    ch = await db.get(Challenge, cid)
    assert ch is not None
    answers = [t.answer for t in generate_tasks(ch.seed)]
    r = await client.post("/api/register/complete", json={"challenge_id": cid, "answers": answers})
    assert r.status_code == 200, r.text
