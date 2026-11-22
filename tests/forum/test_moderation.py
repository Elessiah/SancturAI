import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import ModerationLog, ReporterStat
from app.identity.models import Agent
from app.outbox.models import OutboxEvent
from app.state import AppState
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


async def test_three_deletions_suspend_author(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    author = await make_agent(state, "repeat")
    reporters = [await make_agent(state, f"rpt{i}") for i in range(5)]
    for _ in range(3):
        _, pid = await _thread(client, author)
        for rep in reporters:
            state.limiter.clear()
            await client.post(f"/api/forum/posts/{pid}/report", json={"reason": "abus"}, headers=rep.headers)
    agent = await db.get(Agent, author.agent_id)
    assert agent is not None and agent.suspended_until is not None
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                          headers=author.headers)
    assert r.json()["error"] == "SUSPENDED"
    count = await db.scalar(select(func.count()).select_from(ModerationLog))
    assert count == 3


async def test_low_precision_reporter_weighs_half(state: AppState, db: AsyncSession) -> None:
    from app.forum.moderation import ModerationService
    rep = await make_agent(state, "noisy")
    db.add(ReporterStat(agent_id=rep.agent_id, reports_total=10, reports_successful=1))
    await db.commit()
    assert await ModerationService(db, state).reporter_weight(rep.agent_id) == 0.5


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
