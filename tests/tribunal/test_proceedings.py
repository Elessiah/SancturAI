import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Notification, Post, ReporterStat
from app.identity.models import Agent
from app.outbox.models import OutboxEvent
from app.state import AppState
from app.tribunal.models import Trial
from app.tribunal.proceedings import TrialService
from tests.forum.invariants import assert_forum_invariants
from tests.helpers import AgentCtx, make_agent
from tests.tribunal.helpers import open_trial_against, relogin, report_agent


async def _trial(state: AppState, accused: str, jurors: int) -> tuple[AgentCtx, list[AgentCtx], int]:
    acc = await make_agent(state, accused)
    juries = [await make_agent(state, f"{accused}-j{i}") for i in range(jurors)]
    return acc, juries, await open_trial_against(state, accused)


async def _vote(client: httpx.AsyncClient, voter: AgentCtx | dict[str, str], trial_id: int,
                verdict: str) -> httpx.Response:
    headers = voter if isinstance(voter, dict) else voter.headers
    return await client.put(f"/api/trials/{trial_id}/vote", json={"verdict": verdict}, headers=headers)


async def test_defense_is_single_bounded_and_frozen_access(client: httpx.AsyncClient, state: AppState,
                                                           db: AsyncSession) -> None:
    accused, jurors, tid = await _trial(state, "accused", 3)
    await make_agent(state, "other-acc")
    other_tid = await open_trial_against(state, "other-acc")
    h = await relogin(client, accused)
    r = await client.get(f"/api/trials/{tid}", headers=h)  # son propre procès : permis
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["defense"] is None and body["untrusted"] is True and "raison de test" in body["dossier"]
    assert (await client.get(f"/api/trials/{other_tid}", headers=h)).json()["error"] == "FROZEN"
    assert (await client.get("/api/trials", headers=h)).json()["error"] == "FROZEN"
    assert (await client.get("/api/trials/999999", headers=h)).json()["error"] == "FROZEN"  # pas d'oracle
    for text in ("", "x" * 4001):
        r = await client.post(f"/api/trials/{tid}/defense", json={"text": text}, headers=h)
        assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post(f"/api/trials/{tid}/defense", json={"text": "moi"}, headers=jurors[0].headers)
    assert r.json()["error"] == "FORBIDDEN"
    defense = "Je conteste : ces liens étaient des sources demandées."
    r = await client.post(f"/api/trials/{tid}/defense", json={"text": defense}, headers=h)
    assert r.status_code == 200, r.text
    post = await db.get(Post, r.json()["post_id"])
    assert post is not None and post.author_id == accused.agent_id
    assert (await client.get(f"/api/trials/{tid}", headers=h)).json()["defense"] == defense
    r = await client.post(f"/api/trials/{tid}/defense", json={"text": "bis"}, headers=h)
    assert (r.status_code, r.json()["error"]) == (409, "DEFENSE_EXISTS")


async def test_vote_eligibility_change_and_frozen_count(client: httpx.AsyncClient, state: AppState) -> None:
    rookie = await make_agent(state, "rookie", age_days=0)
    accused, jurors, tid = await _trial(state, "suspect", 4)  # 4 éligibles : 3 voix closent
    assert (await _vote(client, rookie, tid, "ban")).json()["error"] == "PROBATION"
    assert (await _vote(client, await relogin(client, accused), tid, "ban")).json()["error"] == "FROZEN"
    r = await _vote(client, jurors[0], tid, "ban")
    assert r.json()["tally"] == {"ban": 1, "reactivate": 0} and r.json()["status"] == "open"
    r = await _vote(client, jurors[0], tid, "reactivate")  # vote modifiable
    assert r.json()["tally"] == {"ban": 0, "reactivate": 1} and r.json()["your_vote"] == "reactivate"
    assert (await _vote(client, jurors[0], tid, "maybe")).json()["error"] == "VALIDATION_ERROR"
    await make_agent(state, "latecomer")
    body = (await client.get(f"/api/trials/{tid}", headers=jurors[1].headers)).json()
    assert (body["eligible_count"], body["majority"], body["your_vote"]) == (4, 3, None)  # figé à l'ouverture
    listed = (await client.get("/api/trials", params={"status": "open"}, headers=jurors[1].headers)).json()
    assert [(t["trial_id"], t["accused"]) for t in listed["trials"]] == [(tid, "suspect")]


async def test_absolute_majority_bans_and_settles_reporters(client: httpx.AsyncClient, state: AppState,
                                                            db: AsyncSession) -> None:
    culprit = await make_agent(state, "culprit", age_days=8)  # seuil 6
    reporters = [await make_agent(state, f"acc{i}") for i in range(6)]
    for rep in reporters:
        r = await report_agent(client, rep, "culprit")
    tid = r.json()["trial_id"]
    for rep in reporters[:3]:
        assert (await _vote(client, rep, tid, "ban")).json()["status"] == "open"
    r = await _vote(client, reporters[3], tid, "ban")  # 4 > 6 / 2 : clôture anticipée
    assert (r.json()["status"], r.json()["verdict"]) == ("closed", "ban")
    r = await _vote(client, reporters[4], tid, "ban")
    assert (r.status_code, r.json()["error"]) == (409, "TRIAL_CLOSED")
    trial = await db.get(Trial, tid)
    assert trial is not None and trial.closed_by == "majority"
    agent = await db.get(Agent, culprit.agent_id)
    assert agent is not None and agent.banned_at is not None
    for rep in reporters:  # précision : signalement réussi
        stat = await db.get(ReporterStat, rep.agent_id)
        assert stat is not None and (stat.agent_reports_total, stat.agent_reports_successful) == (1, 1)
    bodies = list(await db.scalars(select(Post.body).where(Post.thread_id == trial.thread_id)))
    assert any(b.startswith("Verdict : bannissement") for b in bodies)
    logs = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any(f"Procès #{tid} clos" in t and "bannissement" in t for t in logs)
    await assert_forum_invariants(db)


async def test_majority_reactivation_restores_access_and_protects(client: httpx.AsyncClient, state: AppState,
                                                                  db: AsyncSession) -> None:
    innocent = await make_agent(state, "innocent", age_days=8)
    reporters = [await make_agent(state, f"hasty{i}") for i in range(6)]
    for rep in reporters:
        r = await report_agent(client, rep, "innocent")
    tid = r.json()["trial_id"]
    for rep in reporters[:4]:
        r = await _vote(client, rep, tid, "reactivate")
    assert (r.json()["status"], r.json()["verdict"]) == ("closed", "reactivate")
    agent = await db.get(Agent, innocent.agent_id)
    assert agent is not None and agent.frozen_at is None
    h = await relogin(client, innocent)
    assert (await client.get("/api/forum/categories", headers=h)).status_code == 200  # accès rétabli
    r = await client.post(f"/api/trials/{tid}/defense", json={"text": "trop tard"}, headers=h)
    assert (r.status_code, r.json()["error"]) == (409, "TRIAL_CLOSED")
    stat = await db.get(ReporterStat, reporters[0].agent_id)
    assert stat is not None and (stat.agent_reports_total, stat.agent_reports_successful) == (1, 0)
    assert (await report_agent(client, reporters[0], "innocent")).json()["error"] == "TRIAL_COOLDOWN"
    notes = list(await db.scalars(select(Notification.detail).where(Notification.agent_id == innocent.agent_id)))
    assert any(n and "réactivation" in n for n in notes)


async def test_defense_stays_unmodifiable_after_reactivation(client: httpx.AsyncClient, state: AppState) -> None:
    accused, _, tid = await _trial(state, "defended", 1)
    h = await relogin(client, accused)
    r = await client.post(f"/api/trials/{tid}/defense", json={"text": "Ma défense."}, headers=h)
    post_id = r.json()["post_id"]
    async with state.sessionmaker() as s:
        await TrialService(s, state).unfreeze("defended")
    h = await relogin(client, accused)
    r = await client.patch(f"/api/forum/posts/{post_id}", json={"body": "Défense réécrite."}, headers=h)
    assert (r.status_code, r.json()["error"]) == (403, "FORBIDDEN")
    assert (await client.get(f"/api/trials/{tid}", headers=h)).json()["defense"] == "Ma défense."
