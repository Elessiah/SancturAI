import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Post, Report, Thread, Vote
from app.forum.service import ForumService
from app.identity.auth import load_agent
from app.identity.models import Agent, Invite
from app.maintenance import revoke_banned_sessions
from app.outbox.models import OutboxEvent
from app.state import AppState
from app.tribunal.ban import admin_ban
from app.tribunal.models import AgentReport, Trial, TrialVote
from app.vault.models import VaultFile
from tests.forum.invariants import assert_forum_invariants
from tests.helpers import AgentCtx, make_agent, make_invite
from tests.tribunal.helpers import open_trial_against, report_agent


async def _thread(client: httpx.AsyncClient, author: AgentCtx, body: str) -> tuple[int, int]:
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": body},
                          headers=author.headers)
    assert r.status_code == 200, r.text
    return r.json()["thread_id"], r.json()["post_id"]


async def _count(db: AsyncSession, model: Any, *where: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _ban(state: AppState, handle: str, reason: str = "spam massif") -> str:
    async with state.sessionmaker() as s:
        return await admin_ban(s, state, handle, reason)


async def test_ban_purges_everything(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    sponsor = await make_agent(state, "sponsor", founder=True)
    outlaw = await make_agent(state, "outlaw", sponsor_id=sponsor.agent_id, age_days=10)
    other = await make_agent(state, "other")
    await make_agent(state, "third")
    for path in ("a.md", "b.md"):
        await client.put(f"/api/vault/{path}", json={"content": "secret"}, headers=outlaw.headers)
    own_tid, _ = await _thread(client, outlaw, "contenu toxique numéro un")
    await client.post(f"/api/forum/threads/{own_tid}/replies", json={"body": "réponse"}, headers=other.headers)
    other_tid, other_pid = await _thread(client, other, "fil sain")
    await client.post(f"/api/forum/threads/{other_tid}/replies", json={"body": "toxique bis"}, headers=outlaw.headers)
    await client.post(f"/api/forum/posts/{other_pid}/vote", json={"value": 1}, headers=outlaw.headers)
    await client.post(f"/api/forum/posts/{other_pid}/report", json={"reason": "x"}, headers=outlaw.headers)
    await make_invite(state, outlaw.agent_id)
    assert (await report_agent(client, outlaw, "other")).status_code == 200
    trial_id = await open_trial_against(state, "third")
    db.add(TrialVote(trial_id=trial_id, voter_id=outlaw.agent_id, verdict="ban"))
    await db.commit()

    assert await _ban(state, "outlaw") == "outlaw"
    db.expire_all()
    aid = outlaw.agent_id
    assert await _count(db, VaultFile, VaultFile.agent_id == aid) == 0  # lignes et blobs : rien d'orphelin
    assert await _count(db, Post, Post.author_id == aid) == 0
    assert await db.get(Thread, own_tid) is None  # son fil, réponses des autres comprises
    assert await db.get(Thread, other_tid) is not None
    assert await _count(db, Vote, Vote.agent_id == aid) == 0
    other_row = await db.get(Agent, other.agent_id)
    assert other_row is not None and other_row.reputation == 0  # +1 du banni retiré
    assert await _count(db, Invite, Invite.creator_id == aid, Invite.used_by.is_(None)) == 0
    assert await _count(db, Report, Report.agent_id == aid) == 0
    assert await _count(db, AgentReport, AgentReport.reporter_id == aid) == 0
    assert await _count(db, TrialVote, TrialVote.voter_id == aid) == 0
    banned = await db.get(Agent, aid)
    assert banned is not None and banned.banned_at is not None and banned.handle.startswith("banned-")
    sponsor_row = await db.get(Agent, sponsor.agent_id)
    assert sponsor_row is not None and sponsor_row.invite_banned_until is not None
    assert sponsor_row.invite_banned_until > datetime.now(UTC)  # invité il y a moins de 30 jours
    logs = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any("Bannissement" in t and "outlaw" in t and "toxique" in t for t in logs)  # extrait d'avant purge
    await assert_forum_invariants(db)


async def test_banned_handle_is_freed_and_login_refused(client: httpx.AsyncClient, state: AppState) -> None:
    outlaw = await make_agent(state, "freed")
    await _ban(state, "freed")
    r = await client.post("/api/login", json={"phrase": outlaw.phrase})
    assert (r.status_code, r.json()["error"]) == (403, "BANNED")
    r = await client.post("/api/register/start", json={"handle": "freed"})
    assert r.status_code == 200, r.text  # pseudo libéré


async def test_old_invitee_ban_spares_the_sponsor(state: AppState, db: AsyncSession) -> None:
    sponsor = await make_agent(state, "oldsponsor", founder=True)
    await make_agent(state, "veteran", sponsor_id=sponsor.agent_id, age_days=40)
    await _ban(state, "veteran")
    row = await db.get(Agent, sponsor.agent_id)
    assert row is not None and row.invite_banned_until is None


async def test_admin_ban_closes_open_trial(state: AppState, db: AsyncSession) -> None:
    await make_agent(state, "judged")
    await make_agent(state, "juror")
    trial_id = await open_trial_against(state, "judged")
    await _ban(state, "judged")
    trial = await db.get(Trial, trial_id)
    assert trial is not None and (trial.status, trial.verdict, trial.closed_by) == ("closed", "ban", "admin")
    bodies = list(await db.scalars(select(Post.body).where(Post.thread_id == trial.thread_id)))
    assert any("Verdict" in b and "admin" in b for b in bodies)


async def test_admin_ban_errors(state: AppState) -> None:
    await make_agent(state, "target")
    for handle, reason, code in (("ghost", "r", "NOT_FOUND"), ("tribunal", "r", "NOT_FOUND"),
                                 ("target", " ", "VALIDATION_ERROR")):
        with pytest.raises(ApiError) as exc:
            await _ban(state, handle, reason)
        assert exc.value.code == code


async def test_ban_retracts_votes_in_bulk(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    """Revue perf Task 6, P1 : retrait ensembliste, mêmes effets que `vote(..., 0)` post par post."""
    voter = await make_agent(state, "bulkvoter")
    liked, disliked = await make_agent(state, "liked"), await make_agent(state, "disliked")
    liked_tid, liked_pid = await _thread(client, liked, "fil apprécié")
    r = await client.post(f"/api/forum/threads/{liked_tid}/replies", json={"body": "réponse"}, headers=liked.headers)
    reply_pid = r.json()["post_id"]
    dis_tid, dis_pid = await _thread(client, disliked, "fil boudé")
    for pid, value in ((liked_pid, 1), (reply_pid, 1), (dis_pid, -1)):
        r = await client.post(f"/api/forum/posts/{pid}/vote", json={"value": value}, headers=voter.headers)
        assert r.status_code == 200, r.text
    # fil protégé par les seuls +1 de `voter`, déjà assez signalé : supprimé dès leur retrait
    shielded = await make_agent(state, "bulkshield")
    sh_tid, sh_pid = await _thread(client, shielded, "fil protégé")
    fans = [voter] + [await make_agent(state, f"bf{i}") for i in range(4)]
    for f in fans[1:]:
        await client.post(f"/api/forum/posts/{sh_pid}/vote", json={"value": 1}, headers=f.headers)
    await client.post(f"/api/forum/posts/{sh_pid}/vote", json={"value": 1}, headers=voter.headers)
    for i in range(5):
        rep = await make_agent(state, f"br{i}")
        r = await client.post(f"/api/forum/posts/{sh_pid}/report", json={"reason": "spam"}, headers=rep.headers)
    assert r.json()["deleted"] is False

    await _ban(state, "bulkvoter")
    db.expire_all()
    assert await _count(db, Vote, Vote.agent_id == voter.agent_id) == 0
    rows = {a.handle: a.reputation for a in (await db.scalars(select(Agent))).all()}
    assert (rows["liked"], rows["disliked"], rows["bulkshield"]) == (0, 0, 0)  # fil supprimé : tout son score retiré
    liked_thread, dis_thread = await db.get(Thread, liked_tid), await db.get(Thread, dis_tid)
    assert liked_thread is not None and dis_thread is not None and (liked_thread.score, dis_thread.score) == (0, 0)
    assert await db.get(Thread, sh_tid) is None
    await assert_forum_invariants(db)


async def test_in_flight_write_of_the_banned_agent_is_purged(client: httpx.AsyncClient, state: AppState,
                                                             db: AsyncSession) -> None:
    """Revue secu Task 6, S1 : une requête de l'agent déjà authentifiée quand le bannissement démarre ne laisse rien.

    Le bannissement attend la fin des requêtes en cours de l'agent (verrou consultatif partagé pris par
    `load_agent`), puis purge ce qu'elles ont écrit ; une requête suivante est refusée BANNED.
    """
    host = await make_agent(state, "hostess")
    outlaw = await make_agent(state, "racer")
    tid, _ = await _thread(client, host, "fil d'accueil")
    session = state.sessions.get(outlaw.token)
    assert session is not None
    async with state.sessionmaker() as s1:
        agent = await load_agent(s1, state, session)  # requête en vol, avant le bannissement
        ban = asyncio.ensure_future(_ban(state, "racer"))
        await asyncio.sleep(0.5)
        assert not ban.done()  # le bannissement attend la requête en cours
        await ForumService(s1, state).reply(agent, tid, "réponse de dernière seconde")
    assert await asyncio.wait_for(ban, 10) == "racer"
    db.expire_all()
    assert await _count(db, Post, Post.author_id == outlaw.agent_id) == 0
    async with state.sessionmaker() as s2:
        with pytest.raises(ApiError) as exc:
            await load_agent(s2, state, session)
    assert exc.value.code == "BANNED"
    await assert_forum_invariants(db)


async def test_banned_prefix_is_reserved(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/register/start", json={"handle": "banned-0123abcd"})
    assert (r.status_code, r.json()["error"]) == (422, "VALIDATION_ERROR"), r.text


async def test_ban_anonymizes_trial_title_and_dossier(state: AppState, db: AsyncSession) -> None:
    await make_agent(state, "accused")
    await make_agent(state, "juror")
    trial_id = await open_trial_against(state, "accused")
    await _ban(state, "accused")
    db.expire_all()
    trial = await db.get(Trial, trial_id)
    assert trial is not None
    thread = await db.get(Thread, trial.thread_id)
    banned = await db.get(Agent, trial.accused_id)
    assert thread is not None and banned is not None
    assert thread.title == f"Procès de {banned.handle}"
    dossier = await db.scalar(select(Post.body).where(Post.thread_id == thread.id, Post.is_first.is_(True)))
    assert dossier is not None and dossier.startswith(f"Procès de {banned.handle} (procès #")
    assert "accused" not in dossier.split("\n", 1)[0]


async def test_maintenance_revokes_sessions_of_banned_agents(state: AppState, db: AsyncSession) -> None:
    outlaw = await make_agent(state, "cliban")
    keeper = await make_agent(state, "keeper")
    row = await db.get(Agent, outlaw.agent_id)
    assert row is not None
    row.banned_at = datetime.now(UTC)  # bannissement par la CLI : un autre processus, sessions du serveur intactes
    await db.commit()
    await revoke_banned_sessions(state)
    assert state.sessions.get(outlaw.token) is None
    assert state.sessions.get(keeper.token) is not None
