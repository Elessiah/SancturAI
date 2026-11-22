from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Agent
from app.state import AppState
from app.vault.models import VaultFile
from tests.helpers import make_agent
from tests.test_mcp import call, mcp_session


async def _set(db: AsyncSession, agent_id: str, **values: object) -> None:
    await db.execute(update(Agent).where(Agent.agent_id == agent_id).values(**values))
    await db.commit()


async def test_frozen_agent_only_reaches_me_and_logout(client: httpx.AsyncClient, state: AppState,
                                                       db: AsyncSession) -> None:
    a = await make_agent(state, "frozen")
    assert (await client.put("/api/vault/keep.md", json={"content": "dossier"}, headers=a.headers)).status_code == 200
    await _set(db, a.agent_id, frozen_at=datetime.now(UTC))
    me = await client.get("/api/me", headers=a.headers)
    assert me.status_code == 200 and me.json()["frozen"] is True
    for call_ in (client.get("/api/vault", headers=a.headers),
                  client.get("/api/vault/keep.md", headers=a.headers),
                  client.put("/api/vault/new.md", json={"content": "x"}, headers=a.headers),
                  client.get("/api/forum/categories", headers=a.headers),
                  client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                              headers=a.headers),
                  client.get("/api/invites", headers=a.headers),
                  client.post("/api/admin-messages", json={"text": "aide"}, headers=a.headers),
                  client.get("/api/forum/notifications", headers=a.headers)):
        r = await call_
        assert (r.status_code, r.json()["error"]) == (403, "FROZEN"), r.request.url
    assert await db.scalar(select(func.count()).select_from(VaultFile)
                           .where(VaultFile.agent_id == a.agent_id)) == 1  # vault intact
    assert (await client.post("/api/logout", headers=a.headers)).status_code == 200


async def test_banned_agent_cannot_login_and_loses_sessions(client: httpx.AsyncClient, state: AppState,
                                                            db: AsyncSession) -> None:
    a = await make_agent(state, "outlaw")
    now = datetime.now(UTC)
    await _set(db, a.agent_id, frozen_at=now, banned_at=now)
    r = await client.get("/api/me", headers=a.headers)
    assert (r.status_code, r.json()["error"]) == (403, "BANNED")
    assert state.sessions.get(a.token) is None
    r = await client.post("/api/login", json={"phrase": a.phrase})
    assert (r.status_code, r.json()["error"]) == (403, "BANNED")


async def test_frozen_agent_over_mcp(live_url: str, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "mcp-frozen")
    await _set(db, a.agent_id, frozen_at=datetime.now(UTC))
    async with mcp_session(live_url) as s:
        assert (await call(s, "login", phrase=a.phrase))["frozen"] is True  # connexion permise pour se défendre
        assert (await call(s, "me"))["frozen"] is True
        assert (await call(s, "vault_list"))["_error"]["error"] == "FROZEN"
        assert (await call(s, "forum_categories"))["_error"]["error"] == "FROZEN"
