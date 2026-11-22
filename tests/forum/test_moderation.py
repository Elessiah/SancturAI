from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import ModerationLog, Post, ReporterStat
from app.identity.models import Agent
from app.outbox.models import OutboxEvent
from app.state import AppState
from app.tribunal.models import Trial
from tests.forum.invariants import assert_forum_invariants
from tests.helpers import AgentCtx, make_agent


async def _thread(client: httpx.AsyncClient, author: AgentCtx, body: str = "contenu") -> tuple[int, int]:
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": body},
                          headers=author.headers)
    return r.json()["thread_id"], r.json()["post_id"]


async def test_votes_and_reputation(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    author = await make_agent(state, "author")
    voters = [await make_agent(state, f"voter{i}") for i in range(3)]
    _, pid = await _thread(client, author)
    assert (await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1},
                              headers=author.headers)).json()["error"] == "FORBIDDEN"
    for v in voters:
        await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=v.headers)
    r = await client.post(f"/api/forum/posts/{pid}/vote", json={"value": -1}, headers=voters[0].headers)
    assert r.json()["score"] == 1  # +1 +1 -1 (vote modifié)
    await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 0}, headers=voters[1].headers)
    agent = await db.get(Agent, author.agent_id)
    assert agent is not None and agent.reputation == 0


async def test_reports_delete_post_and_log(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    author = await make_agent(state, "spammer")
    reporters = [await make_agent(state, f"rep{i}") for i in range(5)]
    tid, pid = await _thread(client, author, "spam spam")
    for i, rep in enumerate(reporters[:4]):
        r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": f"spam {i}"}, headers=rep.headers)
        assert r.json()["deleted"] is False
    r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam"}, headers=reporters[0].headers)
    assert r.json()["error"] == "VALIDATION_ERROR"  # un seul signalement par agent
    r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam 5"}, headers=reporters[4].headers)
    assert r.json()["deleted"] is True
    assert (await client.get(f"/api/forum/threads/{tid}", headers=author.headers)).json()["error"] == "NOT_FOUND"
    log = (await db.scalars(select(ModerationLog))).one()
    assert log.author_id == author.agent_id and len(log.reasons) == 5
    stat = await db.get(ReporterStat, reporters[0].agent_id)
    assert stat is not None and stat.reports_successful == 1
    texts = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any("supprimé" in t for t in texts) and not any("spam spam" in t for t in texts)
    await assert_forum_invariants(db)


async def test_upvotes_protect_post(client: httpx.AsyncClient, state: AppState) -> None:
    author = await make_agent(state, "popular")
    fans = [await make_agent(state, f"fan{i}") for i in range(6)]
    haters = [await make_agent(state, f"hater{i}") for i in range(5)]
    _, pid = await _thread(client, author)
    for f in fans:
        await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=f.headers)
    for h in haters:
        r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "bof"}, headers=h.headers)
    assert r.json()["deleted"] is False  # 5 signalements mais 6 votes utiles


async def _delete_by_reports(client: httpx.AsyncClient, state: AppState, author: AgentCtx,
                             reporters: list[AgentCtx]) -> None:
    _, pid = await _thread(client, author)
    for rep in reporters:
        state.limiter.clear()
        await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "abus"}, headers=rep.headers)


async def test_three_deletions_open_a_trial(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    author = await make_agent(state, "repeat")
    reporters = [await make_agent(state, f"rpt{i}") for i in range(5)]
    for _ in range(3):
        await _delete_by_reports(client, state, author, reporters)
    agent = await db.get(Agent, author.agent_id)
    assert agent is not None and agent.frozen_at is not None
    trial = (await db.scalars(select(Trial).where(Trial.accused_id == author.agent_id))).one()
    assert trial.trigger == "deleted_posts" and trial.status == "open"
    dossier = await db.scalar(select(Post.body).where(Post.thread_id == trial.thread_id, Post.is_first.is_(True)))
    assert dossier is not None and "abus" in dossier  # raisons des signalements des posts supprimés
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                          headers=author.headers)
    assert r.json()["error"] == "SESSION_EXPIRED"  # sessions révoquées à l'ouverture
    token = (await client.post("/api/login", json={"phrase": author.phrase})).json()["session_token"]
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.json()["error"] == "FROZEN"  # reconnexion permise, écriture refusée
    assert await db.scalar(select(func.count()).select_from(ModerationLog)) == 3
    await assert_forum_invariants(db)


async def test_reactivated_agent_is_not_retried_for_old_deletions(client: httpx.AsyncClient, state: AppState,
                                                                   db: AsyncSession) -> None:
    author = await make_agent(state, "again")
    reporters = [await make_agent(state, f"agn{i}") for i in range(5)]
    for _ in range(3):
        await _delete_by_reports(client, state, author, reporters)
    now = datetime.now(UTC)
    await db.execute(update(Trial).values(status="closed", verdict="reactivate", closed_at=now, closed_by="admin"))
    await db.execute(update(Agent).where(Agent.agent_id == author.agent_id).values(frozen_at=None))
    await db.commit()
    token = (await client.post("/api/login", json={"phrase": author.phrase})).json()["session_token"]
    author.token = token
    await _delete_by_reports(client, state, author, reporters)  # 4e suppression, postérieure au procès
    assert await db.scalar(select(func.count()).select_from(Trial)) == 1


async def test_deletions_during_trial_do_not_count_after_reactivation(client: httpx.AsyncClient, state: AppState,
                                                                       db: AsyncSession) -> None:
    """Revue secu Task 5, S1 : les posts de l'accusé restent signalables pendant le procès (pièces du dossier) ;
    leurs suppressions, datées entre ouverture et clôture, ne doivent pas le rejuger dès sa réactivation."""
    author = await make_agent(state, "during")
    reporters = [await make_agent(state, f"dur{i}") for i in range(5)]
    pids = [(await _thread(client, author))[1] for _ in range(5)]

    async def report_all(pid: int) -> None:
        for rep in reporters:
            state.limiter.clear()
            await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "abus"}, headers=rep.headers)

    for pid in pids[:3]:
        await report_all(pid)
    assert await db.scalar(select(func.count()).select_from(Trial)) == 1
    for pid in pids[3:]:  # accusé gelé : ses anciens posts sont encore supprimés pendant le procès
        await report_all(pid)
    assert await db.scalar(select(func.count()).select_from(ModerationLog)) == 5
    await db.execute(update(Trial).values(status="closed", verdict="reactivate", closed_at=datetime.now(UTC),
                                          closed_by="majority"))
    await db.execute(update(Agent).where(Agent.agent_id == author.agent_id).values(frozen_at=None))
    await db.commit()
    author.token = (await client.post("/api/login", json={"phrase": author.phrase})).json()["session_token"]
    await _delete_by_reports(client, state, author, reporters)  # 1re suppression après la clôture
    assert await db.scalar(select(func.count()).select_from(Trial)) == 1
    agent = await db.get(Agent, author.agent_id, populate_existing=True)
    assert agent is not None and agent.frozen_at is None


async def test_low_precision_counts_only_settled_reports(client: httpx.AsyncClient, state: AppState,
                                                         db: AsyncSession) -> None:
    """M9 (revue batch 2) : précision = aboutis / tranchés ; un signalement en attente (post signalé depuis moins
    de 7 jours, toujours en ligne) ne compte pas, sinon un nouveau signalant soigneux tomberait à 0,5."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.forum.models import Report
    from app.forum.moderation import ModerationService
    from tests.forum.invariants import assert_forum_invariants

    author, rep = await make_agent(state, "target"), await make_agent(state, "careful")
    for _ in range(9):
        _, pid = await _thread(client, author)
        state.limiter.clear()
        assert (await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam"},
                                  headers=rep.headers)).json()["deleted"] is False
    await db.execute(update(ReporterStat).where(ReporterStat.agent_id == rep.agent_id)
                     .values(reports_total=ReporterStat.reports_total + 1, reports_successful=1))
    await db.commit()
    await assert_forum_invariants(db)
    mod = ModerationService(db, state)
    assert await mod.reporter_weight(rep.agent_id) == 1.0  # 1 abouti, 9 en attente : rien de tranché à 20 %
    await db.execute(update(Report).values(created_at=datetime.now(UTC) - timedelta(days=8)))
    await db.commit()
    assert await mod.reporter_weight(rep.agent_id) == 0.5  # 9 posts restés en ligne 7 jours : échecs, 1/10


async def test_report_validation_and_concurrent_votes(client: httpx.AsyncClient, state: AppState,
                                                      db: AsyncSession) -> None:
    import asyncio

    author = await make_agent(state, "target")
    voters = [await make_agent(state, f"cv{i}") for i in range(6)]
    _, pid = await _thread(client, author)
    r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "a\x00b"}, headers=voters[0].headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "  "}, headers=voters[0].headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    assert (await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "x"},
                              headers=author.headers)).json()["error"] == "FORBIDDEN"
    assert (await client.post("/api/forum/posts/999999/vote", json={"value": 1},
                              headers=voters[0].headers)).json()["error"] == "NOT_FOUND"
    assert (await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 2},
                              headers=voters[0].headers)).json()["error"] == "VALIDATION_ERROR"
    await asyncio.gather(*[client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=v.headers)
                           for v in voters])
    db.expire_all()
    agent = await db.get(Agent, author.agent_id)
    assert agent is not None and agent.reputation == 6
    stat = await db.get(ReporterStat, voters[0].agent_id)
    assert stat is None


async def test_unresolved_reports_are_neutral(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    """S1 : un auteur qui supprime ses posts signalés ne fait pas chuter la précision de ses signalants."""
    author = await make_agent(state, "dodger")
    replier = await make_agent(state, "replier")
    rep = await make_agent(state, "honest")
    for _ in range(6):  # 6 signalements sur des posts supprimés par l'auteur avant le seuil
        _, pid = await _thread(client, author)
        await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam"}, headers=rep.headers)
        assert (await client.delete(f"/api/forum/posts/{pid}", headers=author.headers)).json() == {"ok": True}
    tid, first = await _thread(client, author)  # réponse signalée, emportée avec son fil
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "r"}, headers=replier.headers)
    await client.post(f"/api/forum/posts/{r.json()['post_id']}/report", json={"reason": "x"}, headers=rep.headers)
    assert (await client.delete(f"/api/forum/posts/{first}", headers=author.headers)).json() == {"ok": True}
    db.expire_all()
    stat = await db.get(ReporterStat, rep.agent_id)
    assert stat is not None and stat.reports_total == 0 and stat.reports_successful == 0
    from app.forum.moderation import ModerationService
    assert await ModerationService(db, state).reporter_weight(rep.agent_id) == 1.0



async def test_edit_voids_earlier_upvote_shield(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    """S2 : seuls les +1 émis ou modifiés depuis la dernière édition protègent ; score et réputation comptent tout."""
    async def scenario(name: str, *, edit: bool, revoters: int) -> bool:
        author = await make_agent(state, f"{name}a")
        fans = [await make_agent(state, f"{name}f{i}") for i in range(5)]
        reporters = [await make_agent(state, f"{name}r{i}") for i in range(5)]
        _, pid = await _thread(client, author)
        for f in fans:
            await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=f.headers)
        if edit:
            r = await client.patch(f"/api/forum/posts/{pid}", json={"body": "spam"}, headers=author.headers)
            assert r.json()["edited_at"] is not None
        for f in fans[:revoters]:  # vote modifié après l'édition : protège de nouveau
            await client.post(f"/api/forum/posts/{pid}/vote", json={"value": -1}, headers=f.headers)
            await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=f.headers)
        db.expire_all()
        agent = await db.get(Agent, author.agent_id)
        assert agent is not None and agent.reputation == 5  # tous les votes, édition ou non
        deleted = False
        for rep in reporters:  # Σ poids = 5
            deleted = (await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam"},
                                         headers=rep.headers)).json()["deleted"]
        return bool(deleted)

    assert await scenario("ke", edit=False, revoters=0) is False  # 5 > 5 faux : protégé
    assert await scenario("de", edit=True, revoters=0) is True  # +1 antérieurs à l'édition ignorés
    assert await scenario("rv", edit=True, revoters=5) is False  # +1 re-posés après l'édition
    assert await scenario("pa", edit=True, revoters=4) is True  # 5 > 4


async def test_withdrawn_upvote_reevaluates(client: httpx.AsyncClient, state: AppState) -> None:
    """S3 : retirer un +1 qui protégeait un post déjà assez signalé le supprime aussitôt."""
    author = await make_agent(state, "shielded")
    fans = [await make_agent(state, f"sf{i}") for i in range(5)]
    _, pid = await _thread(client, author)
    for f in fans:
        await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=f.headers)
    for i in range(5):
        rep = await make_agent(state, f"sr{i}")
        r = await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "spam"}, headers=rep.headers)
    assert r.json()["deleted"] is False  # 5 > 5 faux
    await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=fans[0].headers)  # inchangé
    await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 0}, headers=fans[0].headers)
    r = await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1}, headers=fans[1].headers)
    assert r.json()["error"] == "NOT_FOUND"


async def test_db_checks_reject_impossible_forum_rows(client: httpx.AsyncClient, state: AppState,
                                                     db: AsyncSession) -> None:
    """M4 (revue batch 2) : la base refuse un vote hors ±1, un compteur négatif, une notification incohérente."""
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    a, b = await make_agent(state, "chk-a"), await make_agent(state, "chk-b")
    tid, pid = await _thread(client, a)
    for stmt, params in [
        ("INSERT INTO forum_votes (post_id, agent_id, value) VALUES (:p, :b, 2)", {"p": pid, "b": b.agent_id}),
        ("UPDATE forum_threads SET reply_count = -1 WHERE id = :t", {"t": tid}),
        ("INSERT INTO forum_reporter_stats (agent_id, reports_total, reports_successful) VALUES (:b, 0, -1)",
         {"b": b.agent_id}),
        ("INSERT INTO forum_notifications (agent_id, kind, thread_id, post_id) VALUES (:b, 'moderation', :t, :p)",
         {"b": b.agent_id, "t": tid, "p": pid}),
        ("INSERT INTO forum_notifications (agent_id, kind, detail) VALUES (:b, 'reply', 'x')", {"b": b.agent_id}),
    ]:
        with pytest.raises(IntegrityError):
            await db.execute(text(stmt), params)
        await db.rollback()
