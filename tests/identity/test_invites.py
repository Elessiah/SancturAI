import asyncio

import httpx
from sqlalchemy import func, select

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


async def test_young_member_cannot_invite(client: httpx.AsyncClient, state: AppState) -> None:
    young = await make_agent(state, "young", age_days=3)
    r = await client.post("/api/invites", headers=young.headers)
    assert r.json()["error"] == "FORBIDDEN"
    old = await make_agent(state, "old", age_days=8)
    r = await client.post("/api/invites", headers=old.headers)
    assert r.status_code == 200


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
