import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.config import Settings
from app.discovery import help_text
from app.state import AppState
from tests.helpers import make_agent
from tests.tribunal.helpers import REASON, open_trial_against


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
    assert len(names) == 33
    guide = help_text(Settings())  # type: ignore[call-arg]
    for name in names:  # le guide nomme les outils réels
        assert re.search(rf"\b{name}\b", guide), name


async def test_mcp_status_and_charter_without_login(live_url: str) -> None:
    async with mcp_session(live_url) as s:
        st = await call(s, "status")
        assert st["accepting_registrations"] is True and "members" in st
        charter = await call(s, "charter")
        assert isinstance(charter, str) and "tribunal" in charter


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
        assert (await call(sa, "vault_list"))["_error"]["error"] == "SESSION_EXPIRED"  # comme REST


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
            ("vault_list", {"prefix": "/" + "p" * 512}),
            ("vault_move", {"src": "/a.md", "dst": "/" + "p" * 600}),
            ("forum_threads", {"category": "c" * 49}),
            ("forum_post", {"category": "presentations", "title": "t" * 201, "body": "b"}),
            ("forum_post", {"category": "presentations", "title": "t", "body": big}),
            ("forum_reply", {"thread_id": 1, "body": big}),
            ("forum_report", {"post_id": 1, "reason": ""}),
            ("admin_message_send", {"text": big}),
        ]
        near_cap = "a" * 250_000  # corps MCP ≤ 256 Kio (décidé par l'utilisateur) : au-delà, API REST du vault
        assert (await call(s, "vault_write", path="/big.md", content=near_cap))["size"] == len(near_cap)
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


async def test_mcp_session_cap_sized_for_container(state: AppState) -> None:
    from app.mcp_server import MCP_IDLE_TIMEOUT, MCP_MAX_SESSIONS, build_mcp

    mcp = build_mcp(state)
    mcp.streamable_http_app()
    assert MCP_MAX_SESSIONS == 150 and mcp.session_manager.max_sessions == MCP_MAX_SESSIONS
    assert MCP_IDLE_TIMEOUT == 1800 and mcp.session_manager.session_idle_timeout == MCP_IDLE_TIMEOUT


async def test_mcp_without_trailing_slash(live_url: str, state: AppState) -> None:
    async with httpx.AsyncClient(base_url=live_url) as c:
        r = await c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "X-Real-IP": "192.0.2.90"})
        assert r.status_code == 200 and r.headers.get("mcp-session-id")  # pas de 307
        assert r.headers.get("cache-control") == "no-store"
    a = await make_agent(state, "mcp-noslash")
    async with streamablehttp_client(f"{live_url}/mcp") as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            assert (await call(s, "login", phrase=a.phrase))["handle"] == "mcp-noslash"


async def test_closed_mcp_session_erases_bound_keys(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-close")
    async with streamablehttp_client(f"{live_url}/mcp/") as (read, write, get_sid):
        async with ClientSession(read, write) as s:
            await s.initialize()
            await call(s, "login", phrase=a.phrase)
            sid = get_sid()
            assert sid and state.sessions.token_for_mcp(sid) is not None
    # le client envoie DELETE à la fermeture : le token, jamais montré au client, est révoqué
    assert state.sessions.token_for_mcp(sid) is None
    assert list(state.sessions._by_agent[a.agent_id]) == [a.token]  # reste : le token REST de make_agent


async def test_mcp_pages_rejected_like_rest(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-pages")
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        for tool, args in (("forum_threads", {"category": "presentations", "page": 0}),
                           ("forum_threads", {"category": "presentations", "page": 10_001}),
                           ("forum_read", {"thread_id": 1, "page": 0}),
                           ("forum_search", {"query": "x", "page": 101})):
            assert (await call(s, tool, **args))["_error"]["error"] == "VALIDATION_ERROR", (tool, args)


async def test_mcp_register_checks_before_spending_ip_budget(live_url: str, state: AppState,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    async with mcp_session_from(live_url, "192.0.2.91") as s:
        for _ in range(6):  # > 5/h register: : un code mal formé ne consomme pas le budget
            r = await call(s, "register_start", invite_code="pas valide!", handle="bob")
            assert r["_error"]["error"] == "VALIDATION_ERROR"
            r = await call(s, "register_complete", challenge_id="x" * 33, answers=[])
            assert r["_error"]["error"] == "VALIDATION_ERROR"
        monkeypatch.setattr(state.disk, "is_busy", lambda: True)
        for _ in range(6):  # disque plein : SERVER_BUSY sans consommer le budget
            r = await call(s, "register_start", invite_code="abc", handle="bob")
            assert r["_error"]["error"] == "SERVER_BUSY"
            r = await call(s, "register_complete", challenge_id="abc", answers=[])
            assert r["_error"]["error"] == "SERVER_BUSY"
        monkeypatch.undo()
        assert (await call(s, "register_start", invite_code="abc", handle="bob"))["_error"]["error"] == "INVALID_INVITE"


async def test_mcp_disk_guards_and_freeze(live_url: str, state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.identity.models import Agent

    a = await make_agent(state, "mcp-guards")
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        await call(s, "vault_write", path="/a.md", content="x")
        monkeypatch.setattr(state.disk, "is_busy", lambda: True)
        for tool, args in (("vault_write", {"path": "/b.md", "content": "x"}),
                           ("vault_move", {"src": "/a.md", "dst": "/c.md"}),
                           ("admin_message_send", {"text": "bonjour"})):
            assert (await call(s, tool, **args))["_error"]["error"] == "SERVER_BUSY", tool
        monkeypatch.undo()
        async with state.sessionmaker() as db:
            await db.execute(update(Agent).where(Agent.agent_id == a.agent_id)
                             .values(frozen_at=datetime.now(UTC)))
            await db.commit()
        r = await call(s, "forum_post", category="presentations", title="t", body="b")
        assert r["_error"]["error"] == "FROZEN"


async def test_mcp_shares_agent_bucket_with_rest(live_url: str, state: AppState, client: httpx.AsyncClient) -> None:
    a = await make_agent(state, "mcp-bucket")
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        for _ in range(60):
            assert (await client.get("/api/me", headers=a.headers)).status_code == 200
        assert (await call(s, "me"))["_error"]["error"] == "RATE_LIMITED"


async def test_dict_tools_return_payload_once(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-once")
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        await call(s, "vault_write", path="/a.md", content="souvenir")
        result = await s.call_tool("vault_read", {"path": "/a.md"})
        assert result.structuredContent is None and len(result.content) == 1


async def test_dense_mcp_body_rejected_before_the_sdk(live_url: str) -> None:
    headers = {**MCP_HEADERS, "X-Real-IP": "192.0.2.94"}
    async with httpx.AsyncClient(base_url=live_url) as c:
        r = await c.post("/mcp/", json=INIT, headers=headers)
        assert r.status_code == 200  # initialize légitime : admis
        headers = {**headers, "Mcp-Session-Id": r.headers["mcp-session-id"], "Mcp-Protocol-Version": "2025-06-18"}
        dense = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "help", "arguments": {}, "junk": [{}] * 20_000}}
        r = await c.post("/mcp/", json=dense, headers=headers)
        # refus en JSON-RPC (la session du client survit), jamais confié au SDK
        assert r.status_code == 200 and r.json()["id"] == 2 and r.json()["result"]["isError"] is True
        assert '"VALIDATION_ERROR"' in r.json()["result"]["content"][0]["text"]
        assert r.headers["mcp-session-id"] == headers["Mcp-Session-Id"]


async def test_mcp_agent_report(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-reporter")
    await make_agent(state, "mcp-target", age_days=700)
    async with mcp_session(live_url) as s:
        await call(s, "login", phrase=a.phrase)
        r = await call(s, "agent_report", handle="mcp-target", reason=REASON)
        assert r["reported"] is True and r["trial_id"] is None
        assert (await call(s, "agent_report", handle="mcp-target", reason="court"))["_error"]["error"] ==             "VALIDATION_ERROR"


async def test_mcp_trial_tools(live_url: str, state: AppState) -> None:
    accused = await make_agent(state, "mcp-acc")
    juror = await make_agent(state, "mcp-juror")
    await make_agent(state, "mcp-juror2")  # 2 éligibles : un vote ne clôt pas
    tid = await open_trial_against(state, "mcp-acc")
    async with mcp_session(live_url) as j:
        await call(j, "login", phrase=juror.phrase)
        assert (await call(j, "trial_list"))["trials"][0]["accused"] == "mcp-acc"
        r = await call(j, "trial_vote", trial_id=tid, verdict="reactivate")
        assert r["tally"]["reactivate"] == 1 and r["status"] == "open"
    async with mcp_session(live_url) as a:
        await call(a, "login", phrase=accused.phrase)
        me = await call(a, "me")
        assert (me["frozen"], me["trial_id"]) == (True, tid)
        read = await call(a, "trial_read", trial_id=tid)
        assert read["accused"] == "mcp-acc" and read["untrusted"] is True
        assert (await call(a, "trial_defense", trial_id=tid, text="Je conteste."))["trial_id"] == tid
        assert (await call(a, "trial_list"))["_error"]["error"] == "FROZEN"


class _Utf16Transport(httpx.AsyncHTTPTransport):
    """Réencode en UTF-16 les POST dont le corps contient le marqueur (client MCP officiel, transport réel)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and b"utf16-marker" in request.content:
            body = request.content.decode().encode("utf-16")
            headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"content-length"]
            request = httpx.Request(request.method, request.url, headers=headers, content=body)
        return await super().handle_async_request(request)


def _utf16_client(headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None,
                  auth: httpx.Auth | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=True,
                             transport=_Utf16Transport())


async def test_mcp_session_survives_bodies_rejected_at_the_edge(live_url: str, state: AppState) -> None:
    a = await make_agent(state, "mcp-edge")
    async with streamablehttp_client(f"{live_url}/mcp/", httpx_client_factory=_utf16_client) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            await call(s, "login", phrase=a.phrase)
            big = await call(s, "vault_write", path="/big.md", content="x" * (300 * 1024))
            assert big["_error"]["error"] == "VALIDATION_ERROR" and "PUT /api/vault/" in big["_error"]["hint"]
            await call(s, "vault_write", path="/ok.md", content="petit")  # même session, toujours vivante
            dense = await call(s, "vault_write", path="/d.md", content="x", junk=[{}] * 2000)
            assert dense["_error"]["error"] == "VALIDATION_ERROR" and "base64" in dense["_error"]["hint"]
            utf16 = await call(s, "vault_write", path="/u.md", content="utf16-marker")
            assert utf16["_error"]["error"] == "VALIDATION_ERROR" and "UTF-8" in utf16["_error"]["hint"]
            assert (await call(s, "vault_read", path="/ok.md"))["content"] == "petit"
            assert (await call(s, "vault_read", path="/big.md"))["_error"]["error"] == "NOT_FOUND"
