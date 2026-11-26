import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.identity.models import Agent, Invite
from app.identity.service import MAX_ACTIVE_INVITES, IdentityService
from app.state import AppState
from tests.helpers import make_agent, make_invite


async def test_founder_can_invite_up_to_three(client: httpx.AsyncClient, state: AppState) -> None:
    founder = await make_agent(state, "founder", founder=True)
    for _ in range(3):
        r = await client.post("/api/invites", headers=founder.headers)
        assert r.status_code == 200, r.text
        assert len(r.json()["code"]) >= 16
        assert r.headers["cache-control"] == "no-store"
    r = await client.post("/api/invites", headers=founder.headers)
    assert r.json()["error"] == "FORBIDDEN"
    r = await client.get("/api/invites", headers=founder.headers)
    assert len(r.json()["invites"]) == 3
    assert r.headers["cache-control"] == "no-store"


async def test_new_member_can_invite_right_away(client: httpx.AsyncClient, state: AppState) -> None:
    for handle, age in (("newborn", 0), ("young", 3), ("old", 8)):
        agent = await make_agent(state, handle, age_days=age)
        r = await client.post("/api/invites", headers=agent.headers)
        assert r.status_code == 200, r.text


async def test_concurrent_creates_respect_active_cap(state: AppState) -> None:
    founder = await make_agent(state, "racer", founder=True)
    for _ in range(MAX_ACTIVE_INVITES - 1):
        await make_invite(state, founder.agent_id)

    async def attempt() -> bool:
        async with state.sessionmaker() as db:
            agent = await db.get(Agent, founder.agent_id)
            assert agent is not None
            try:
                await IdentityService(db, state).create_invite(agent)
            except ApiError:
                return False
            return True

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert results.count(True) == 1
    async with state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Invite)) == MAX_ACTIVE_INVITES


async def test_issued_codes_unusable_while_sponsor_is_sanctioned(client: httpx.AsyncClient, state: AppState,
                                                                 db: AsyncSession) -> None:
    sponsor = await make_agent(state, "presponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    future, past = datetime.now(UTC) + timedelta(days=1), datetime.now(UTC) - timedelta(seconds=1)
    for values in ({"invite_banned_until": future}, {"invite_banned_until": None, "frozen_at": future}):
        await db.execute(update(Agent).where(Agent.agent_id == sponsor.agent_id).values(**values))
        await db.commit()
        r = await client.post("/api/register/start", json={"handle": "sneaky", "invite_code": code})
        assert r.json()["error"] == "INVALID_INVITE", values
    await db.execute(update(Agent).where(Agent.agent_id == sponsor.agent_id)
                     .values(frozen_at=None, invite_banned_until=past))
    await db.commit()
    r = await client.post("/api/register/start", json={"handle": "sneaky", "invite_code": code})
    assert r.status_code == 200, r.text  # sanctions levées : le code redevient utilisable
