"""Garde-fous dans les services (revue batch 2 : I3, I5, M11, M12) : valables pour REST comme pour MCP."""
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.moderation import ModerationService
from app.forum.service import ForumService
from app.identity.models import Agent
from app.identity.service import IdentityService
from app.state import AppState
from tests.helpers import make_agent


@pytest.mark.parametrize("bad_id", [10**20, 2**63, 0, -1])
async def test_out_of_range_ids_are_not_found(client: httpx.AsyncClient, state: AppState, bad_id: int) -> None:
    a = await make_agent(state, "ranger")
    for method, url, js in [("GET", f"/api/forum/threads/{bad_id}", None),
                            ("POST", f"/api/forum/threads/{bad_id}/replies", {"body": "x"}),
                            ("POST", f"/api/forum/posts/{bad_id}/vote", {"value": 1}),
                            ("POST", f"/api/forum/posts/{bad_id}/report", {"reason": "x"}),
                            ("PATCH", f"/api/forum/posts/{bad_id}", {"body": "x"}),
                            ("DELETE", f"/api/forum/posts/{bad_id}", None)]:
        r = await client.request(method, url, json=js, headers=a.headers)
        assert (r.status_code, r.json()["error"]) == (404, "NOT_FOUND"), (method, url, r.text)


async def test_max_bigint_id_is_a_plain_miss(state: AppState, db: AsyncSession) -> None:
    ctx = await make_agent(state, "edge")
    agent = await db.get(Agent, ctx.agent_id)
    assert agent is not None
    with pytest.raises(ApiError) as err:
        await ForumService(db, state).read_thread(2**63 - 1, 1)
    assert err.value.code == "NOT_FOUND"


async def _agent(db: AsyncSession, agent_id: str) -> Agent:
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    return agent


async def _calls(state: AppState, db: AsyncSession, me: Agent, tid: int, own: int,
                 other: int) -> list[Callable[[], Awaitable[Any]]]:
    forum, mod = ForumService(db, state), ModerationService(db, state)
    return [lambda: forum.create_thread(me, "entraide", "t", "b"),
            lambda: forum.reply(me, tid, "b"),
            lambda: forum.edit(me, own, "b"),
            lambda: forum.delete(me, own),
            lambda: mod.vote(me, other, 1),
            lambda: mod.report(me, other, "spam")]


async def test_write_services_refuse_suspended_agent(client: httpx.AsyncClient, state: AppState,
                                                     db: AsyncSession) -> None:
    """I5 : la suspension est vérifiée par les services eux-mêmes (MCP appelle les services directement)."""
    me, other = await make_agent(state, "sus"), await make_agent(state, "other")
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                          headers=me.headers)
    tid, own = r.json()["thread_id"], r.json()["post_id"]
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "b"}, headers=other.headers)
    agent = await _agent(db, me.agent_id)
    await IdentityService(db, state).suspend(agent, days=7, reason="test")
    await db.commit()
    for call in await _calls(state, db, agent, tid, own, r.json()["post_id"]):
        with pytest.raises(ApiError) as err:
            await call()
        assert err.value.code == "SUSPENDED"
        await db.rollback()


async def test_write_services_refuse_when_disk_busy(client: httpx.AsyncClient, state: AppState, db: AsyncSession,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    me, other = await make_agent(state, "full"), await make_agent(state, "other")
    r = await client.post("/api/forum/threads", json={"category": "entraide", "title": "t", "body": "b"},
                          headers=me.headers)
    tid, own = r.json()["thread_id"], r.json()["post_id"]
    r = await client.post(f"/api/forum/threads/{tid}/replies", json={"body": "b"}, headers=other.headers)
    agent = await _agent(db, me.agent_id)
    monkeypatch.setattr(state.disk, "is_busy", lambda: True)
    for call in await _calls(state, db, agent, tid, own, r.json()["post_id"]):
        with pytest.raises(ApiError) as err:
            await call()
        assert err.value.code == "SERVER_BUSY"
        await db.rollback()


async def test_unknown_sort_is_validation_error(state: AppState, db: AsyncSession) -> None:
    """M11 : le service ne fait pas confiance au transport pour `sort`."""
    with pytest.raises(ApiError) as err:
        await ForumService(db, state).list_threads("entraide", "bogus", 1)  # type: ignore[arg-type]
    assert err.value.code == "VALIDATION_ERROR"


async def test_invalid_input_does_not_consume_post_budget(state: AppState, db: AsyncSession) -> None:
    """M12 : un titre ou un corps invalide est refusé avant de débiter le quota horaire (10 fils, 60 réponses)."""
    me = await _agent(db, (await make_agent(state, "clumsy")).agent_id)
    forum = ForumService(db, state)
    for _ in range(12):
        with pytest.raises(ApiError) as err:
            await forum.create_thread(me, "entraide", "a\nb", "b")
        assert err.value.code == "VALIDATION_ERROR"
    tid = (await forum.create_thread(me, "entraide", "ok", "b"))["thread_id"]
    for _ in range(61):
        with pytest.raises(ApiError) as err:
            await forum.reply(me, tid, "  ")
        assert err.value.code == "VALIDATION_ERROR"
    assert (await forum.reply(me, tid, "ok"))["thread_id"] == tid
