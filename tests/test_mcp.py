import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
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
