import asyncio
import json
from typing import Any

import httpx
import pytest

import app.middleware as m
from app.middleware import BodyLimitMiddleware, body_cap

KIB = 1024
MIB = 1024 * KIB


def scope(path: str, length: int | None = None, method: str = "POST", chunked: bool = False) -> dict[str, Any]:
    headers = [(b"content-length", str(length).encode())] if length is not None else []
    if chunked:
        headers.append((b"transfer-encoding", b"chunked"))
    return {"type": "http", "method": method, "path": path, "root_path": "", "headers": headers}


class Recorder:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def __call__(self, message: Any) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return int(self.messages[0]["status"])

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        return list(self.messages[0]["headers"])

    @property
    def json(self) -> Any:
        return json.loads(b"".join(x.get("body", b"") for x in self.messages[1:]))


async def empty_receive() -> Any:
    return {"type": "http.request", "body": b"", "more_body": False}


async def ok(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_route_caps_follow_each_route_need() -> None:
    assert body_cap("PUT", "/api/vault/notes/a.md") == 16 * MIB
    assert body_cap("PUT", "/api/vault/by-id/abc") == 16 * MIB
    assert body_cap("POST", "/mcp/") == MIB  # décidé par l'utilisateur : gros fichiers par l'API REST
    assert body_cap("POST", "/mcp") == MIB
    assert body_cap("POST", "/api/forum/threads") == 128 * KIB
    assert body_cap("PATCH", "/api/forum/posts/1") == 128 * KIB
    assert body_cap("POST", "/api/login") == 64 * KIB
    assert body_cap("POST", "/api/vault/move") == 64 * KIB
    assert body_cap("POST", "/mcpx") == 64 * KIB
    assert body_cap("POST", "/internal/admin-replies") == 64 * KIB


async def test_declared_length_over_route_cap_rejected_before_app() -> None:
    called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    guard = BodyLimitMiddleware(inner)
    for path, length in (("/api/login", 64 * KIB + 1), ("/api/forum/threads", 128 * KIB + 1),
                         ("/mcp/", MIB + 1), ("/api/vault/a.md", 16 * MIB + 1)):
        method = "PUT" if path.startswith("/api/vault") else "POST"
        send = Recorder()
        await guard(scope(path, length, method), empty_receive, send)
        assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR", path
        assert (b"cache-control", b"no-store") in send.headers
    assert not called and guard.budget.used == 0


async def test_chunked_body_over_cap_rejected_while_streaming() -> None:
    reads = 0

    async def receive() -> Any:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * KIB, "more_body": reads < 1000}

    called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    guard = BodyLimitMiddleware(inner)
    send = Recorder()
    await guard(scope("/api/login", chunked=True), receive, send)
    assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR"
    assert not called and reads == 65  # arrêt dès le dépassement, pas tout le corps
    assert guard.budget.used == 0


async def test_chunked_body_under_cap_is_replayed_to_the_app() -> None:
    chunks = [b"ab", b"cd", b"ef"]

    async def receive() -> Any:
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    got: list[bytes] = []
    reserved: list[int] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reserved.append(guard.budget.used)
        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        got.append(body)
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(inner)
    send = Recorder()
    await guard(scope("/api/vault/a.md", method="PUT", chunked=True), receive, send)
    assert send.status == 200 and got == [b"abcdef"]
    assert reserved == [6]  # surplus de la réservation (plafond de la route) rendu après lecture
    assert guard.budget.used == 0


async def test_in_flight_budget_is_shared_then_server_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_BUDGET_WAIT", 0.05)
    release = asyncio.Event()

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await release.wait()
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(inner, budget_bytes=17 * MIB)  # valeur de production
    first, small = Recorder(), Recorder()
    big = asyncio.create_task(guard(scope("/api/vault/b.md", 16 * MIB, "PUT"), empty_receive, first))
    await asyncio.sleep(0.01)
    busy = Recorder()
    await guard(scope("/api/vault/a.md", 4 * MIB, "PUT"), empty_receive, busy)
    assert busy.status == 503 and busy.json["error"] == "SERVER_BUSY"
    assert (b"cache-control", b"no-store") in busy.headers
    mcp_busy = Recorder()
    await guard(scope("/mcp/", MIB), empty_receive, mcp_busy)  # REST et MCP : même budget (MCP compté double)
    assert mcp_busy.status == 503
    assert (b"retry-after", b"5") in busy.headers
    small_task = asyncio.create_task(guard(scope("/api/login", KIB), empty_receive, small))  # tient dans le reste
    await asyncio.sleep(0.01)
    assert guard.budget.used == 16 * MIB + KIB
    release.set()
    await asyncio.gather(big, small_task)
    assert first.status == 200 and small.status == 200 and guard.budget.used == 0


async def test_waiter_proceeds_when_budget_frees(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_BUDGET_WAIT", 2.0)
    running = 0
    peak = 0

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(inner, budget_bytes=18 * MIB)
    sends = [Recorder() for _ in range(3)]
    await asyncio.gather(*(guard(scope("/api/vault/a.md", 10 * MIB, "PUT"), empty_receive, s) for s in sends))
    assert [s.status for s in sends] == [200, 200, 200] and peak == 1  # gros corps : un à la fois
    sends = [Recorder() for _ in range(3)]
    await asyncio.gather(*(guard(scope("/mcp/", 100), empty_receive, s) for s in sends))
    assert peak == 3  # petits corps : en parallèle


async def test_hold_is_bounded_and_json_when_no_response_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_HOLD", 0.05)

    async def slow_upload(scope: Any, receive: Any, send: Any) -> None:  # aucune réponse avant la borne
        await asyncio.sleep(3600)

    guard = BodyLimitMiddleware(slow_upload)
    send = Recorder()
    await asyncio.wait_for(guard(scope("/mcp/", MIB), empty_receive, send), 2)
    assert send.status == 503 and (b"retry-after", b"5") in send.headers
    assert send.json["error"] == "SERVER_BUSY" and guard.budget.used == 0

    async def stalled(scope: Any, receive: Any, send: Any) -> None:  # lecteur lent : la réponse ne finit jamais
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(3600)

    guard = BodyLimitMiddleware(stalled)
    send = Recorder()
    await asyncio.wait_for(guard(scope("/mcp/", MIB), empty_receive, send), 2)
    assert [x["type"] for x in send.messages] == ["http.response.start"] and guard.budget.used == 0


async def test_bodyless_requests_are_not_held_or_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_HOLD", 0.05)

    async def sse(scope: Any, receive: Any, send: Any) -> None:  # GET /mcp/ : flux SSE de longue durée
        assert guard.budget.used == 0
        await asyncio.sleep(0.15)
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(sse)
    send = Recorder()
    await guard(scope("/mcp/", method="GET"), empty_receive, send)
    assert send.status == 200


async def test_rest_routes_get_their_cap(client: httpx.AsyncClient) -> None:
    pad = b'{"phrase":"' + b"a" * (64 * KIB) + b'"}'
    r = await client.post("/api/login", content=pad, headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "VALIDATION_ERROR"
    assert "aaa" not in r.json()["message"]
    r = await client.post("/does-not-exist", content=pad)
    assert r.json()["error"] == "VALIDATION_ERROR"  # route inconnue : même plafond par défaut
    r = await client.post("/api/forum/threads", json={"pad": "x" * 100_000})
    assert r.json()["error"] == "UNAUTHENTICATED"  # forum : 128 Kio admis
    r = await client.post("/api/forum/threads", json={"pad": "x" * 140_000})
    assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.put("/api/vault/a.md", json={"content": "x" * 1_000_000})
    assert r.json()["error"] == "UNAUTHENTICATED"  # écriture vault : 16 Mio admis


async def test_mcp_bodies_are_charged_double() -> None:
    seen: list[int] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(guard.budget.used)
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(inner)
    await guard(scope("/mcp/", 1000), empty_receive, Recorder())
    await guard(scope("/api/login", 1000), empty_receive, Recorder())
    assert seen == [2000, 1000]  # SDK : json.loads + copie model_dump ≈ 2 × le coût REST
    assert guard.budget.used == 0


async def test_oversized_mcp_body_points_to_rest(client: httpx.AsyncClient) -> None:
    r = await client.post("/mcp/", content=b"x" * (MIB + 1), headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "VALIDATION_ERROR"
    assert "/api/vault/" in r.json()["hint"] and r.headers["cache-control"] == "no-store"


async def test_transfer_encoding_wins_over_content_length() -> None:
    reads = 0

    async def receive() -> Any:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * KIB, "more_body": True}

    called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    guard = BodyLimitMiddleware(inner)
    sc = scope("/api/login", 0, chunked=True)  # CL: 0 + TE: chunked (h11 l'admet et lit en chunked)
    send = Recorder()
    await guard(sc, receive, send)
    assert send.status == 422 and not called and reads == 65
    send = Recorder()
    bad = {**scope("/api/login"), "headers": [(b"content-length", b"12x")]}
    await guard(bad, empty_receive, send)
    assert send.status == 422 and not called and guard.budget.used == 0


async def test_slow_chunked_upload_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_HOLD", 0.05)

    async def stuck() -> Any:
        await asyncio.sleep(3600)

    guard = BodyLimitMiddleware(ok)
    send = Recorder()
    await asyncio.wait_for(guard(scope("/api/vault/a.md", method="PUT", chunked=True), stuck, send), 2)
    assert send.status == 503 and send.json["error"] == "SERVER_BUSY" and guard.budget.used == 0


async def test_lease_keeps_budget_until_last_holder_releases() -> None:
    leases: list[Any] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        lease = scope[m.BODY_LEASE_KEY]
        assert lease.retain()  # usage au-delà de l'échange HTTP (outil MCP encore en cours)
        leases.append(lease)
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(inner)
    await guard(scope("/api/login", 100), empty_receive, Recorder())
    assert guard.budget.used == 100  # échange HTTP fini, usage retenu : toujours compté
    leases[0].release()
    assert guard.budget.used == 0
    assert not leases[0].retain()  # réservation rendue : plus rien à retenir
