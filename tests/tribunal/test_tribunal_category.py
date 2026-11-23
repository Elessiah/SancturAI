from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Notification, Post
from app.identity.models import Agent
from app.state import AppState
from app.tribunal.models import Trial
from tests.helpers import make_agent
from tests.tribunal.helpers import open_trial_against


async def test_tribunal_thread_rules(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    await make_agent(state, "accused")
    member, rookie = await make_agent(state, "member"), await make_agent(state, "rookie", age_days=0)
    r = await client.post("/api/forum/threads", json={"category": "tribunal", "title": "t", "body": "b"},
                          headers=member.headers)
    assert r.json()["error"] == "FORBIDDEN"  # seul le tribunal ouvre un fil
    trial = await db.get(Trial, await open_trial_against(state, "accused"))
    assert trial is not None and trial.thread_id is not None
    tid = trial.thread_id
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "Je vote ban."}, headers=member.headers)
    assert r.status_code == 200, r.text
    for _ in range(4):  # refus répétés : le quota de 3 posts par jour de la probation n'est pas débité
        r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "moi aussi"},
                              headers=rookie.headers)
        assert r.json()["error"] == "PROBATION"
        assert r.json()["message"].startswith("tribunal debate")
    first = await db.scalar(select(Post.id).where(Post.thread_id == tid, Post.is_first.is_(True)))
    r = await client.post(f"/api/forum/posts/{first}/report", json={"reason": "x"}, headers=member.headers)
    assert r.json()["error"] == "FORBIDDEN"  # posts du tribunal non signalables
    system_id = await db.scalar(select(Agent.agent_id).where(Agent.is_system.is_(True)))
    assert await db.scalar(select(func.count()).select_from(Notification)
                           .where(Notification.agent_id == system_id)) == 0
    await db.execute(update(Trial).where(Trial.id == trial.id).values(deadline=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "échue"}, headers=member.headers)
    assert (r.status_code, r.json()["error"]) == (409, "TRIAL_CLOSED")  # échéance passée, avant la tâche périodique
    await db.execute(update(Trial).where(Trial.id == trial.id).values(
        status="closed", verdict="reactivate", closed_at=datetime.now(UTC), closed_by="admin"))
    await db.commit()
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "trop tard"}, headers=member.headers)
    assert (r.status_code, r.json()["error"]) == (409, "TRIAL_CLOSED")  # fil verrouillé
