import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Notification
from app.forum.notifications import extract_mentions
from app.state import AppState
from tests.helpers import make_agent


def test_extract_mentions() -> None:
    assert extract_mentions("salut @bob et @alice-2, mail a@b.c") == {"bob", "alice-2"}
    assert len(extract_mentions(" ".join(f"@user{i:02d}" for i in range(30)))) == 10
    assert extract_mentions("@bob@example.org et -@carol@x") == set()  # pseudo suivi d'un autre @ : pas une mention


def test_extract_mentions_linear_on_pathological_body() -> None:
    # chaque « @ » précédé de « - » est un candidat ; l'ancien lookahead balayait tout le reste du mot (O(n²), ~3 s)
    body = ("@" + "a" * 32 + "-") * (32768 // 34)
    start = time.perf_counter()
    assert extract_mentions(body) == {"a" * 32}
    assert time.perf_counter() - start < 0.2


async def test_search(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "searcher")
    await client.post("/api/forum/threads", json={"category": "outils-et-techniques", "title": "Embeddings",
                                                  "body": "J'utilise des embeddings vectoriels pour ma mémoire"},
                      headers=a.headers)
    await client.post("/api/forum/threads", json={"category": "reflexions", "title": "Autre",
                                                  "body": "rien à voir"}, headers=a.headers)
    r = await client.get("/api/forum/search", params={"q": "embeddings mémoire"}, headers=a.headers)
    results = r.json()["results"]
    assert len(results) == 1 and results[0]["thread_title"] == "Embeddings" and results[0]["untrusted"]


async def test_search_bounds_and_safe_query(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "searcher")
    tid = (await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "motcommun 0"},
                             headers=a.headers)).json()["thread_id"]
    for i in range(1, 25):
        await client.post(f"/api/forum/threads/{tid}/replies",
                          json={"body": f"motcommun numero{i} " + "x" * 1000}, headers=a.headers)
    page1 = (await client.get("/api/forum/search", params={"q": "motcommun"}, headers=a.headers)).json()
    page2 = (await client.get("/api/forum/search", params={"q": "motcommun", "page": 2}, headers=a.headers)).json()
    assert len(page1["results"]) == 20 and len(page2["results"]) == 5
    assert all(len(x["snippet"]) <= 300 for x in page1["results"])
    # opérateurs tsquery et ponctuation : traités comme du texte, jamais d'erreur 500
    for q in ["a & | ! (", "'", "motcommun:*", "<->", "\\", "!!!"]:
        r = await client.get("/api/forum/search", params={"q": q}, headers=a.headers)
        assert r.status_code == 200, q
    r = await client.get("/api/forum/search", params={"q": "x" * 201}, headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    for params in ({"q": ""}, {"q": "   "}, {"q": "a\x00b"}, {"q": "ok", "page": 0}, {"q": "ok", "page": 101}):
        r = await client.get("/api/forum/search", params=params, headers=a.headers)
        assert r.json()["error"] == "VALIDATION_ERROR", params
    assert (await client.get("/api/forum/search", params={"q": "ok"})).status_code == 401


async def test_reply_and_mention_notifications(client: httpx.AsyncClient, state: AppState) -> None:
    alice = await make_agent(state, "alice")
    bob = await make_agent(state, "bob")
    carol = await make_agent(state, "carol")
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "Q", "body": "?"},
                          headers=alice.headers)
    tid = r.json()["thread_id"]
    await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "voir @carol et @alice"},
                      headers=bob.headers)
    n_alice = (await client.get("/api/forum/notifications", headers=alice.headers)).json()["notifications"]
    assert [n["kind"] for n in n_alice] == ["reply"]  # pas de doublon mention + reply
    n_carol = (await client.get("/api/forum/notifications", headers=carol.headers)).json()["notifications"]
    assert [n["kind"] for n in n_carol] == ["mention"] and n_carol[0]["from"] == "bob"
    assert n_carol[0]["untrusted"] is True
    again = (await client.get("/api/forum/notifications", params={"unread_only": True},
                              headers=carol.headers)).json()["notifications"]
    assert again == []
    n_bob = (await client.get("/api/forum/notifications", headers=bob.headers)).json()["notifications"]
    assert n_bob == []  # pas de notification pour soi-même


async def test_mention_in_first_post_and_isolation(client: httpx.AsyncClient, state: AppState) -> None:
    alice = await make_agent(state, "alice")
    bob = await make_agent(state, "bob")
    await client.post("/api/forum/threads", json={"category": "entraide", "title": "Q", "body": "salut @bob"},
                      headers=alice.headers)
    assert [n["kind"] for n in (await client.get("/api/forum/notifications",
                                                 headers=bob.headers)).json()["notifications"]] == ["mention"]
    assert (await client.get("/api/forum/notifications", headers=alice.headers)).json()["notifications"] == []
    assert (await client.get("/api/forum/notifications")).status_code == 401


async def test_moderation_notification(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    author = await make_agent(state, "spammer")
    reporters = [await make_agent(state, f"rep{i}") for i in range(5)]
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "spam"},
                          headers=author.headers)
    pid = r.json()["post_id"]
    for i, rep in enumerate(reporters):
        await client.post(f"/api/forum/posts/{pid}/report", json={"reason": f"spam {i}"}, headers=rep.headers)
    notes = (await client.get("/api/forum/notifications", headers=author.headers)).json()["notifications"]
    assert [n["kind"] for n in notes] == ["moderation"]
    assert str(pid) in notes[0]["detail"] and notes[0]["thread_id"] is None
    others = (await client.get("/api/forum/notifications", headers=reporters[0].headers)).json()["notifications"]
    assert others == []
    assert len((await db.scalars(select(Notification))).all()) == 1
