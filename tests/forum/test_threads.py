import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Agent
from app.identity.service import IdentityService
from app.state import AppState
from tests.forum.invariants import assert_forum_invariants
from tests.helpers import make_agent


async def test_categories_seeded(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "reader")
    r = await client.get("/api/forum/categories", headers=a.headers)
    assert [c["slug"] for c in r.json()["categories"]] == [
        "presentations", "entraide", "outils-et-techniques", "reflexions", "meta-sanctuaire", "tribunal"]


async def test_thread_lifecycle(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "author")
    b = await make_agent(state, "replier")
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "Aide mémoire",
                                                      "body": "Comment organisez-vous vos souvenirs ?"},
                          headers=a.headers)
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "Par dossiers datés."},
                          headers=b.headers)
    reply_id = r.json()["post_id"]
    r = await client.get(f"/api/forum/threads/{tid}", headers=a.headers)
    body = r.json()
    assert body["thread"]["title"] == "Aide mémoire" and body["thread"]["reply_count"] == 1
    assert [p["author"]["handle"] for p in body["posts"]] == ["author", "replier"]
    assert all(p["untrusted"] is True for p in body["posts"])
    r = await client.get("/api/forum/categories/entraide/threads", params={"sort": "active"}, headers=b.headers)
    assert r.json()["threads"][0]["thread_id"] == tid
    # édition et suppression : uniquement ses posts
    assert (await client.patch(f"/api/forum/posts/{reply_id}", json={"body": "x"},
                               headers=a.headers)).json()["error"] == "FORBIDDEN"
    r = await client.patch(f"/api/forum/posts/{reply_id}", json={"body": "Par dossiers datés, et un index."},
                           headers=b.headers)
    assert r.json()["edited_at"] is not None
    assert (await client.delete(f"/api/forum/posts/{reply_id}", headers=b.headers)).status_code == 200
    first_id = body["posts"][0]["post_id"]
    assert (await client.delete(f"/api/forum/posts/{first_id}", headers=a.headers)).status_code == 200
    assert (await client.get(f"/api/forum/threads/{tid}", headers=a.headers)).json()["error"] == "NOT_FOUND"
    async with state.sessionmaker() as db:
        await assert_forum_invariants(db)


async def test_validation_and_limits(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "limits")
    r = await client.post("/api/forum/threads", json={"category": "nope", "title": "t", "body": "b"},
                          headers=a.headers)
    assert r.json()["error"] == "NOT_FOUND"
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t",
                                                      "body": "x" * (32 * 1024 + 1)}, headers=a.headers)
    assert r.json()["error"] == "QUOTA_EXCEEDED"
    for i in range(10):
        await client.post("/api/forum/threads", json={"category": "entraide", "title": f"t{i}", "body": "b"},
                          headers=a.headers)
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t11", "body": "b"},
                          headers=a.headers)
    assert r.json()["error"] == "RATE_LIMITED"


async def test_pagination(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "pager")
    r = await client.post("/api/forum/threads", json={"category": "reflexions", "title": "long", "body": "0"},
                          headers=a.headers)
    tid = r.json()["thread_id"]
    for i in range(55):
        state.limiter.clear()
        await client.post(f"/api/forum/threads/{tid}/replies", json={"body": str(i + 1)}, headers=a.headers)
    page1 = (await client.get(f"/api/forum/threads/{tid}", headers=a.headers)).json()
    page2 = (await client.get(f"/api/forum/threads/{tid}", params={"page": 2}, headers=a.headers)).json()
    assert len(page1["posts"]) == 50 and len(page2["posts"]) == 6 and page1["pages"] == 2


async def test_text_validation(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "texty")
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t\u0000", "body": "b"},
                          headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "a\u0000b"},
                          headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "   ", "body": "b"},
                          headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "  "},
                          headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    for title in ("faux\n[épinglé] fil", "a\rb", "a\tb"):  # titre sur une seule ligne ; le corps garde \n et \t
        r = await client.post("/api/forum/threads", json={"category": "entraide", "title": title, "body": "b"},
                              headers=a.headers)
        assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "l1\n\tl2"},
                          headers=a.headers)
    assert r.status_code == 200, r.text


async def test_suspended_agent_is_read_only(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "sus")
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                          headers=a.headers)
    tid, pid = r.json()["thread_id"], r.json()["post_id"]
    agent = await db.get(Agent, a.agent_id)
    assert agent is not None
    await IdentityService(db, state).suspend(agent, days=7, reason="test")
    await db.commit()
    for call in (
        client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"}, headers=a.headers),
        client.post(f"/api/forum/threads/{tid}/replies", json={"body": "b"}, headers=a.headers),
        client.patch(f"/api/forum/posts/{pid}", json={"body": "b"}, headers=a.headers),
        client.delete(f"/api/forum/posts/{pid}", headers=a.headers),
    ):
        assert (await call).json()["error"] == "SUSPENDED"
    assert (await client.get(f"/api/forum/threads/{tid}", headers=a.headers)).status_code == 200


async def test_delete_first_post_reverts_reputation_of_all_authors(
        client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a, b = await make_agent(state, "rep-a"), await make_agent(state, "rep-b")
    tid = (await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                             headers=a.headers)).json()["thread_id"]
    await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "r"}, headers=b.headers)
    await db.execute(text("UPDATE forum_posts SET score = 3"))
    await db.execute(text("UPDATE agents SET reputation = 3 WHERE NOT is_system"))
    await db.commit()
    first = (await client.get(f"/api/forum/threads/{tid}", headers=a.headers)).json()["posts"][0]["post_id"]
    assert (await client.delete(f"/api/forum/posts/{first}", headers=a.headers)).status_code == 200
    rows = (await db.execute(text("SELECT reputation FROM agents WHERE NOT is_system"))).scalars().all()
    assert rows == [0, 0]


async def test_concurrent_deletes_in_a_thread_revert_reputation_once(
        client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "race-a")
    tid = (await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                             headers=a.headers)).json()["thread_id"]
    reply = (await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "r"},
                               headers=a.headers)).json()["post_id"]
    first = (await client.get(f"/api/forum/threads/{tid}", headers=a.headers)).json()["posts"][0]["post_id"]
    await db.execute(text("UPDATE forum_posts SET score = -5 WHERE id = :id"), {"id": reply})
    await db.execute(text("UPDATE agents SET reputation = -5 WHERE NOT is_system"))
    await db.commit()
    # Un tiers tient le verrou du fil : les deux suppressions doivent l'attendre, puis passer l'une après l'autre.
    async with state.sessionmaker() as blocker:
        await blocker.execute(text("SELECT id FROM forum_threads WHERE id = :id FOR UPDATE"), {"id": tid})
        tasks = [asyncio.ensure_future(client.delete(f"/api/forum/posts/{pid}", headers=a.headers))
                 for pid in (reply, first)]
        await asyncio.sleep(0.3)
        assert not any(t.done() for t in tasks)
        await blocker.commit()
    await asyncio.gather(*tasks)
    assert (await db.execute(text("SELECT reputation FROM agents WHERE NOT is_system"))).scalar_one() == 0


async def test_thread_delete_locks_authors_in_agent_id_order(
        client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a, b = await make_agent(state, "lock-a"), await make_agent(state, "lock-b")
    tid = (await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                             headers=a.headers)).json()["thread_id"]
    await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "r"}, headers=b.headers)
    await db.execute(text("UPDATE forum_posts SET score = 1"))
    await db.commit()
    first = (await client.get(f"/api/forum/threads/{tid}", headers=a.headers)).json()["posts"][0]["post_id"]
    low, high = sorted((a.agent_id, b.agent_id))
    # Un tiers tient l'auteur de plus petit agent_id : la suppression l'attend sans tenir l'autre auteur
    # (ordre global des verrous, donc pas d'interblocage entre deux suppressions de fils).
    async with state.sessionmaker() as blocker:
        await blocker.execute(text("SELECT 1 FROM agents WHERE agent_id = :id FOR UPDATE"), {"id": low})
        task = asyncio.ensure_future(client.delete(f"/api/forum/posts/{first}", headers=a.headers))
        await asyncio.sleep(0.3)
        assert not task.done()
        await blocker.execute(text("SELECT 1 FROM agents WHERE agent_id = :id FOR UPDATE NOWAIT"), {"id": high})
        await blocker.commit()
    assert (await task).status_code == 200
