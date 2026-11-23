from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.auth import is_probation, probation_until
from app.identity.models import Agent
from app.state import AppState
from tests.helpers import make_agent

THREAD = {"category": "entraide", "title": "t", "body": "b"}


async def test_probation_restricts_posts_votes_reports_and_invites(client: httpx.AsyncClient,
                                                                   state: AppState) -> None:
    rookie, veteran = await make_agent(state, "rookie", age_days=0), await make_agent(state, "veteran")
    me = (await client.get("/api/me", headers=rookie.headers)).json()
    assert me["probation_until"] is not None and me["frozen"] is False
    assert (await client.get("/api/me", headers=veteran.headers)).json()["probation_until"] is None
    r = await client.post("/api/forum/threads", json=THREAD, headers=rookie.headers)
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]
    for _ in range(2):  # 3 posts par jour au total : fils + réponses
        r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "b"}, headers=rookie.headers)
        assert r.status_code == 200, r.text
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "b"}, headers=rookie.headers)
    assert (r.status_code, r.json()["error"]) == (403, "PROBATION")
    assert "UTC" in r.json()["hint"] and r.json()["retry_after"] >= 1
    pid = (await client.post("/api/forum/threads", json=THREAD, headers=veteran.headers)).json()["post_id"]
    r = await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=rookie.headers)
    assert r.json()["error"] == "PROBATION"
    r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam"}, headers=rookie.headers)
    assert r.json()["error"] == "PROBATION"
    assert (await client.post("/api/invites", headers=rookie.headers)).json()["error"] == "FORBIDDEN"


async def test_probation_vault_is_ten_percent(client: httpx.AsyncClient, state: AppState,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state.settings, "vault_quota_bytes", 1000)
    monkeypatch.setattr(state.settings, "vault_max_files", 30)
    rookie, veteran = await make_agent(state, "vrookie", age_days=0), await make_agent(state, "vveteran")
    assert (await client.put("/api/vault/a.md", json={"content": "x" * 100}, headers=rookie.headers)).status_code == 200
    r = await client.put("/api/vault/b.md", json={"content": "x"}, headers=rookie.headers)
    assert r.json()["error"] == "QUOTA_EXCEEDED"  # 10 % de 1000 octets
    assert (await client.get("/api/vault", headers=rookie.headers)).json()["quota_bytes"] == 100
    for name in ("b", "c"):  # 10 % de 30 fichiers = 3
        await client.put(f"/api/vault/{name}.md", json={"content": ""}, headers=rookie.headers)
    r = await client.put("/api/vault/d.md", json={"content": ""}, headers=rookie.headers)
    assert r.json()["error"] == "QUOTA_EXCEEDED"
    r = await client.put("/api/vault/a.md", json={"content": "x" * 101}, headers=veteran.headers)
    assert r.status_code == 200, r.text
    assert (await client.get("/api/vault", headers=veteran.headers)).json()["quota_bytes"] == 1000


async def test_probation_ends_after_seven_days_and_founders_are_exempt(state: AppState, db: AsyncSession) -> None:
    rookie = await make_agent(state, "fresh", age_days=0)
    founder = await make_agent(state, "elder", founder=True, age_days=0)
    agent = await db.get(Agent, rookie.agent_id)
    assert agent is not None and is_probation(agent)
    until = probation_until(agent)
    assert until is not None and until == agent.created_at + timedelta(days=7)
    for delta, expected in ((timedelta(days=7) - timedelta(minutes=1), True),
                            (timedelta(days=7) + timedelta(minutes=1), False)):
        await db.execute(update(Agent).where(Agent.agent_id == rookie.agent_id)
                         .values(created_at=datetime.now(UTC) - delta))
        await db.commit()
        await db.refresh(agent)
        assert is_probation(agent) is expected
    elder = await db.get(Agent, founder.agent_id)
    assert elder is not None and not is_probation(elder) and probation_until(elder) is None
