"""Interblocages reproduits par la revue du batch 2 (I1, I2) : l'entrelacement est forcé par une porte."""
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.forum import service as fsvc
from app.forum.moderation import ModerationService
from app.forum.service import ForumService
from app.identity.models import Agent
from app.state import AppState
from tests.forum.invariants import assert_forum_invariants
from tests.helpers import make_agent


async def _post_thread(client: httpx.AsyncClient, headers: dict[str, str], title: str) -> dict[str, Any]:
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": title, "body": "b"},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def _gate(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Bloque le premier appel de ForumService.<name> jusqu'à `gate.set()` ; `reached` signale l'arrivée."""
    gate, reached = asyncio.Event(), asyncio.Event()
    orig: Callable[..., Awaitable[Any]] = getattr(fsvc.ForumService, name)
    first = [True]

    async def patched(self: ForumService, *args: Any) -> Any:
        if first[0]:
            first[0] = False
            reached.set()
            await gate.wait()
        return await orig(self, *args)

    monkeypatch.setattr(fsvc.ForumService, name, patched)
    return gate, reached


async def _run_interleaved(first: Awaitable[Any], second: Callable[[], Awaitable[Any]],
                           gate: asyncio.Event, reached: asyncio.Event) -> list[Any]:
    t1 = asyncio.ensure_future(first)
    await asyncio.wait_for(reached.wait(), 5)
    t2 = asyncio.ensure_future(second())
    await asyncio.sleep(0.5)  # la seconde transaction va aussi loin qu'elle peut
    gate.set()
    return list(await asyncio.wait_for(asyncio.gather(t1, t2, return_exceptions=True), 10))


async def test_thread_delete_does_not_deadlock_with_concurrent_reply(
        client: httpx.AsyncClient, state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    """I1 : la suppression d'un fil verrouille ses auteurs en FOR NO KEY UPDATE, pas FOR UPDATE.

    Y répond au fil T2 de X (KS(Y) tenu par l'insertion du post), X supprime T1 (où Y a une réponse notée) :
    avec FOR UPDATE, la suppression attendait KS(Y) et la notification de la réponse attendait FU(X).
    """
    p, q = await make_agent(state, "alpha"), await make_agent(state, "bravo")
    x, y = sorted([p, q], key=lambda c: c.agent_id)
    z = await make_agent(state, "zulu")
    t1 = await _post_thread(client, x.headers, "t1")
    t2 = await _post_thread(client, x.headers, "t2")
    yr = (await client.post(f"/api/forum/threads/{t1['thread_id']}/replies", json={"body": "r"},
                            headers=y.headers)).json()
    for pid in (t1["post_id"], yr["post_id"]):
        assert (await client.post(f"/api/forum/posts/{pid}/vote", json={"value": 1},
                                  headers=z.headers)).status_code == 200
    gate, reached = _gate(monkeypatch, "_after_post")

    async def reply() -> Any:
        async with state.sessionmaker() as db:
            agent = await db.get(Agent, y.agent_id)
            assert agent is not None
            return await ForumService(db, state).reply(agent, t2["thread_id"], "hello")

    async def delete() -> str:
        async with state.sessionmaker() as db:
            agent = await db.get(Agent, x.agent_id)
            assert agent is not None
            await ForumService(db, state).delete(agent, t1["post_id"])
            return "deleted"

    results = await _run_interleaved(reply(), delete, gate, reached)
    assert not [r for r in results if isinstance(r, BaseException)], results
    async with state.sessionmaker() as db:
        await assert_forum_invariants(db)


async def test_withdrawn_upvote_deletion_does_not_deadlock_with_author_delete(
        client: httpx.AsyncClient, state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    """I2 : le vote prend RS avant AG, comme `remove_post` (la réputation est mise à jour après `_evaluate`).

    V retire un +1 sur P1 (5 signalements, 5 +1) → suppression ; A supprime au même moment P2 (signalé par R1).
    """
    a = await make_agent(state, "author")
    voters = [await make_agent(state, f"voter{i}") for i in range(5)]
    reps = [await make_agent(state, f"rep{i}") for i in range(5)]
    p1 = (await _post_thread(client, a.headers, "t1"))["post_id"]
    p2 = (await _post_thread(client, a.headers, "t2"))["post_id"]
    for v in voters:
        await client.post(f"/api/forum/posts/{p1}/vote", json={"value": 1}, headers=v.headers)
    await client.post(f"/api/forum/posts/{p2}/vote", json={"value": 1}, headers=voters[0].headers)
    for rp in reps:
        r = await client.post(f"/api/forum/posts/{p1}/report", json={"reason": "spam"}, headers=rp.headers)
        assert r.json()["deleted"] is False, r.text
    r = await client.post(f"/api/forum/posts/{p2}/report", json={"reason": "spam"}, headers=reps[0].headers)
    assert r.json()["deleted"] is False
    gate, reached = _gate(monkeypatch, "_settle_reports")

    async def withdraw() -> Any:
        async with state.sessionmaker() as db:
            agent = await db.get(Agent, voters[1].agent_id)
            assert agent is not None
            return await ModerationService(db, state).vote(agent, p1, 0)

    async def delete() -> str:
        async with state.sessionmaker() as db:
            agent = await db.get(Agent, a.agent_id)
            assert agent is not None
            await ForumService(db, state).delete(agent, p2)
            return "deleted"

    results = await _run_interleaved(withdraw(), delete, gate, reached)
    assert not [r for r in results if isinstance(r, BaseException)], results
    async with state.sessionmaker() as db:
        author = await db.get(Agent, a.agent_id)
        assert author is not None and author.reputation == 0  # P1 et P2 supprimés : plus aucun score
        await assert_forum_invariants(db)
