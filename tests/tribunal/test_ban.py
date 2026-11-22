from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Post, Report, Thread, Vote
from app.identity.models import Agent, Invite
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
