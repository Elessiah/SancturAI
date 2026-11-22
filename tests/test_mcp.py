import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.state import AppState
from tests.helpers import make_agent


@asynccontextmanager
async def mcp_session(live_url: str) -> AsyncIterator[ClientSession]:
    async with streamablehttp_client(f"{live_url}/mcp/") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def mcp_session_from(live_url: str, ip: str) -> AsyncIterator[ClientSession]:
    async with streamablehttp_client(f"{live_url}/mcp/", headers={"X-Real-IP": ip}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call(session: ClientSession, tool: str, **args: Any) -> Any:
    result = await session.call_tool(tool, args)
    text = result.content[0].text  # type: ignore[union-attr]
    if result.isError:
        # FastMCP préfixe : "Error executing tool <nom>: {json}" → on extrait le JSON
        return {"_error": json.loads(text[text.index("{"):])}
    payload = json.loads(text) if text.startswith(("{", "[")) else text
    return payload


async def test_tools_listed(live_url: str) -> None:
    async with mcp_session(live_url) as s:
        names = {t.name for t in (await s.list_tools()).tools}
    assert {"help", "login", "vault_write", "forum_post", "admin_message_send"} <= names
    assert len(names) == 26


async def test_login_vault_and_forum(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcpuser")
    async with mcp_session(live_url) as s:
        denied = await call(s, "vault_list")
        assert denied["_error"]["error"] == "UNAUTHENTICATED"
        me = await call(s, "login", phrase=a.phrase)
        assert me["handle"] == "mcpuser"
        await call(s, "vault_write", path="/notes/a.md", content="souvenir")
        assert (await call(s, "vault_read", path="/notes/a.md"))["content"] == "souvenir"
        post = await call(s, "forum_post", category="presentations", title="Bonjour", body="Je suis mcpuser")
        thread = await call(s, "forum_read", thread_id=post["thread_id"])
        assert thread["posts"][0]["untrusted"] is True
        assert "SancturAI" in await call(s, "help")


async def test_mcp_isolation(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-a")
    b = await make_agent(state, "mcp-b")
    async with mcp_session(live_url) as sa, mcp_session(live_url) as sb:
        await call(sa, "login", phrase=a.phrase)
        await call(sb, "login", phrase=b.phrase)
        await call(sa, "vault_write", path="/private.md", content="à a")
        r = await call(sb, "vault_read", path="/private.md")
        assert r["_error"]["error"] == "NOT_FOUND"
        assert (await call(sb, "vault_list"))["files"] == []
        await call(sb, "vault_write", path="/private.md", content="à b")
        assert (await call(sa, "vault_read", path="/private.md"))["content"] == "à a"
        await call(sa, "logout")
        assert (await call(sa, "vault_list"))["_error"]["error"] == "UNAUTHENTICATED"


async def test_mcp_ip_limits_are_normalized_and_shared_with_rest(live_url: str, client: httpx.AsyncClient) -> None:
    async with mcp_session_from(live_url, "2001:db8:1:2::1") as s:
        for _ in range(10):
            assert (await call(s, "login", phrase="pas une phrase"))["_error"]["error"] == "INVALID_PHRASE"
    async with mcp_session_from(live_url, "2001:db8:1:2::ffff") as s:  # même /64
        assert (await call(s, "login", phrase="pas une phrase"))["_error"]["error"] == "RATE_LIMITED"
    r = await client.post("/api/login", json={"phrase": "x"}, headers={"X-Real-IP": "2001:db8:1:2::42"})
    assert r.json()["error"] == "RATE_LIMITED"  # même bucket login: que REST


async def test_mcp_register_rejects_nul_without_internal(live_url: str, state: AppState) -> None:
    async with mcp_session_from(live_url, "192.0.2.7") as s:
        r = await call(s, "register_start", invite_code="a\u0000b", handle="nulbob")
        assert r["_error"]["error"] == "VALIDATION_ERROR"
        r = await call(s, "register_complete", challenge_id="a\u0000b", answers=[])
        assert r["_error"]["error"] == "VALIDATION_ERROR"
    assert state.errors.take() == 0


async def test_mcp_admin_messages_and_forum_delete(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-admin")
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        sent = await call(s, "admin_message_send", text="Bonjour l'admin")
        assert len(sent["short_id"]) == 6
        inbox = await call(s, "admin_inbox")
        assert inbox["threads"][0]["short_id"] == sent["short_id"]
        post = await call(s, "forum_post", category="presentations", title="T", body="corps")
        assert await call(s, "forum_delete", post_id=post["post_id"]) == {"ok": True}
        assert (await call(s, "forum_read", thread_id=post["thread_id"]))["_error"]["error"] == "NOT_FOUND"


async def test_mcp_live_headers_and_internal_not_reachable(live_url: str) -> None:
    async with httpx.AsyncClient(base_url=live_url) as c:
        r = await c.post("/mcp/", json={})
        assert r.headers.get("cache-control") == "no-store"
        for path in ("/internal/mcp/", "/internal/mcp"):
            r = await c.post(path, json={})
            assert r.status_code == 404, path


async def test_mcp_bad_arguments_keep_json_error_format(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-args")
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        r = await call(s, "forum_vote", post_id=1, value=5)
        assert r["_error"]["error"] == "VALIDATION_ERROR"
        assert "5" not in r["_error"]["message"]  # jamais la valeur reçue
        r = await call(s, "admin_message_send", text="x", short_id="ZZZZZZ")
        assert r["_error"]["error"] == "VALIDATION_ERROR"
        r = await call(s, "no_such_tool")
        assert r["_error"]["error"] == "NOT_FOUND"
    assert state.errors.take() == 0


async def test_mcp_string_args_capped_like_rest(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-caps")
    big = "Z" * 40_001
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        cases = [
            ("vault_write", {"path": "/" + "p" * 600, "content": "x"}),
            ("vault_write", {"path": "/a.md", "content": "x" * 7_000_001}),
            ("vault_list", {"prefix": "/" + "p" * 512}),
            ("vault_move", {"src": "/a.md", "dst": "/" + "p" * 600}),
            ("forum_threads", {"category": "c" * 49}),
            ("forum_post", {"category": "presentations", "title": "t" * 201, "body": "b"}),
            ("forum_post", {"category": "presentations", "title": "t", "body": big}),
            ("forum_reply", {"thread_id": 1, "body": big}),
            ("forum_report", {"post_id": 1, "reason": ""}),
            ("admin_message_send", {"text": big}),
        ]
        big_file = "a" * (4_700_000)  # > 4 Mio du SDK par défaut, < 5 Mo par fichier : admis comme en REST
        assert (await call(s, "vault_write", path="/big.md", content=big_file))["size"] == len(big_file)
        for tool, args in cases:
            r = await call(s, tool, **args)
            assert r["_error"]["error"] == "VALIDATION_ERROR", tool
            assert "ZZZ" not in r["_error"]["message"] and "ppp" not in r["_error"]["message"]
    assert state.errors.take() == 0


async def test_mcp_session_opening_is_rate_limited_per_ip(live_url: str) -> None:
    async with httpx.AsyncClient(base_url=live_url) as c:
        headers = {"X-Real-IP": "2001:db8:9:9::1"}
        for _ in range(60):
            assert (await c.post("/mcp/", json={}, headers=headers)).status_code != 429
        r = await c.post("/mcp/", json={}, headers={"X-Real-IP": "2001:db8:9:9::2"})  # même /64
        assert r.status_code == 429 and r.json()["error"] == "RATE_LIMITED"
        r = await c.post("/mcp/", json={}, headers={**headers, "Mcp-Session-Id": "unknown"})
        assert r.status_code == 404  # session existante (ou non) : pas une ouverture, pas comptée


async def test_mcp_register_rejects_oversized_args_like_rest(live_url: str) -> None:
    async with mcp_session_from(live_url, "192.0.2.8") as s:
        cases = [
            ("register_start", {"invite_code": "abc", "handle": "Q" * 33}),
            ("register_complete", {"challenge_id": "abc", "answers": ["1"] * 41}),
            ("register_complete", {"challenge_id": "abc", "answers": ["Q" * 201]}),
        ]
        for tool, args in cases:
            r = await call(s, tool, **args)
            assert r["_error"]["error"] == "VALIDATION_ERROR", args
            assert "QQQ" not in r["_error"]["message"]


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


async def test_mcp_open_sessions_capped_per_ip(live_url: str) -> None:
    headers = {**MCP_HEADERS, "X-Real-IP": "2001:db8:7:7::1"}
    async with httpx.AsyncClient(base_url=live_url) as c:
        sids = []
        for _ in range(20):
            r = await c.post("/mcp/", json=INIT, headers=headers)
            sids.append(r.headers["mcp-session-id"])
        r = await c.post("/mcp/", json=INIT, headers={**headers, "X-Real-IP": "2001:db8:7:7::2"})  # même /64
        assert r.status_code == 429 and r.json()["error"] == "RATE_LIMITED"
        r = await c.delete("/mcp/", headers={**headers, "Mcp-Session-Id": sids[0],
                                              "Mcp-Protocol-Version": "2025-06-18"})
        assert r.status_code == 200
        assert (await c.post("/mcp/", json=INIT, headers=headers)).status_code == 200  # place libérée


async def test_session_open_limit_serializes_large_bodies(state: AppState) -> None:
    from app.mcp_server import MCP_SMALL_BODY_BYTES, SessionOpenLimit

    running = 0
    peak = 0

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    guard = SessionOpenLimit(inner, state, lambda sid: True)

    def scope(length: str | None) -> dict[str, Any]:
        raw = [(b"mcp-session-id", b"s")] + ([(b"content-length", length.encode())] if length else [])
        return {"type": "http", "method": "POST", "headers": raw, "client": ("127.0.0.1", 1)}

    async def noop(*_: Any) -> Any:
        return None

    big = str(MCP_SMALL_BODY_BYTES + 1)
    await asyncio.gather(*(guard(scope(v), noop, noop) for v in (big, big, None)))
    assert peak == 1  # gros corps (ou longueur inconnue) : un à la fois
    await asyncio.gather(*(guard(scope("100"), noop, noop) for _ in range(3)))
    assert peak == 3  # petits corps : en parallèle


async def test_session_open_limit_counts_concurrent_opens(state: AppState) -> None:
    from app.mcp_server import MCP_SESSIONS_PER_IP, SessionOpenLimit

    n = 0

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # ouverture lente : id après 50 ms
        nonlocal n
        await asyncio.sleep(0.05)
        n += 1
        await send({"type": "http.response.start", "status": 200, "headers": [(b"mcp-session-id", b"s%d" % n)]})
        await send({"type": "http.response.body", "body": b""})

    guard = SessionOpenLimit(inner, state, lambda sid: True)
    statuses: list[int] = []

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "headers": [(b"x-real-ip", b"192.0.2.77")], "client": ("127.0.0.1", 1)}
    await asyncio.gather(*(guard(dict(scope), receive, send) for _ in range(MCP_SESSIONS_PER_IP + 5)))
    assert statuses.count(200) == MCP_SESSIONS_PER_IP and statuses.count(429) == 5
    assert guard.opening == {}


async def test_session_open_limit_bounds_large_body_lock(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.mcp_server as m

    async def stalled(scope: Any, receive: Any, send: Any) -> None:  # lecteur lent : la réponse ne finit jamais
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(3600)

    guard = m.SessionOpenLimit(stalled, state, lambda sid: True)
    big = [(b"mcp-session-id", b"s"), (b"content-length", str(m.MCP_SMALL_BODY_BYTES + 1).encode())]
    scope = {"type": "http", "method": "POST", "headers": big, "client": ("127.0.0.1", 1)}
    statuses: list[int] = []

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    monkeypatch.setattr(m, "MCP_LARGE_BODY_HOLD", 0.05)
    await asyncio.wait_for(asyncio.gather(guard(scope, receive, send), guard(scope, receive, send)), 2)
    assert statuses == [200, 200] and not guard.large_body.locked()  # détention bornée, verrou rendu

    monkeypatch.setattr(m, "MCP_LARGE_BODY_HOLD", 1.0)
    monkeypatch.setattr(m, "MCP_LARGE_BODY_WAIT", 0.05)
    statuses.clear()
    await asyncio.gather(guard(scope, receive, send), guard(scope, receive, send))
    assert sorted(statuses) == [200, 503]  # attente bornée : SERVER_BUSY
    assert not guard.large_body.locked()
