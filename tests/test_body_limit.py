import asyncio
import json
import random
import time
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
    assert body_cap("POST", "/mcp/") == 256 * KIB  # décidé par l'utilisateur : gros fichiers par l'API REST
    assert body_cap("POST", "/mcp") == 256 * KIB
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
                         ("/mcp/", 256 * KIB + 1), ("/api/vault/a.md", 16 * MIB + 1)):
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

    guard = BodyLimitMiddleware(inner, budget_bytes=16 * MIB + 200 * KIB)
    first, small = Recorder(), Recorder()
    big = asyncio.create_task(guard(scope("/api/vault/b.md", 16 * MIB, "PUT"), empty_receive, first))
    await asyncio.sleep(0.01)
    busy = Recorder()
    await guard(scope("/api/vault/a.md", 4 * MIB, "PUT"), empty_receive, busy)
    assert busy.status == 503 and busy.json["error"] == "SERVER_BUSY"
    assert (b"cache-control", b"no-store") in busy.headers
    mcp_busy = Recorder()
    await guard(scope("/mcp/", 256 * KIB), empty_receive, mcp_busy)  # REST et MCP : même budget
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
    await asyncio.wait_for(guard(scope("/mcp/", 100 * KIB), empty_receive, send), 2)
    assert send.status == 503 and (b"retry-after", b"5") in send.headers
    assert send.json["error"] == "SERVER_BUSY" and guard.budget.used == 0

    async def stalled(scope: Any, receive: Any, send: Any) -> None:  # lecteur lent : la réponse ne finit jamais
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(3600)

    guard = BodyLimitMiddleware(stalled)
    send = Recorder()
    await asyncio.wait_for(guard(scope("/mcp/", 100 * KIB), empty_receive, send), 2)
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


async def test_oversized_mcp_body_points_to_rest(client: httpx.AsyncClient) -> None:
    r = await client.post("/mcp/", content=b"x" * (256 * KIB + 1), headers={"content-type": "application/json"})
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


INIT_FULL = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18",
    "capabilities": {"roots": {"listChanged": True}, "sampling": {}, "elicitation": {},
                     "experimental": {"a": {"x": 1, "y": [1, 2, 3]}, "b": {"z": True}}},
    "clientInfo": {"name": "client", "title": "Client", "version": "1.2.3",
                   "icons": [{"src": "https://x/i.png", "mimeType": "image/png", "sizes": ["48x48", "96x96"]}]},
    "_meta": {"progressToken": 1}}}
REGISTER_40 = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
    "name": "register_complete", "arguments": {"challenge_id": "abc", "answers": [str(i * 37) for i in range(40)]},
    "_meta": {"progressToken": 7}}}
VAULT_WRITE = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
    "name": "vault_write", "arguments": {"path": "/a.md", "content": "{[,:\\\"" * 20_000, "encoding": "utf8"}}}


def test_json_structure_counts_outside_strings_only() -> None:
    assert m.json_structure(b'{"a": "{[,:", "b": [1, 2]}') == 6  # { : , : [ , hors chaînes
    # JSON {"k\\": "x\"y,:", "z": 1} : "k\\" se ferme, "x\"y,:" ne se ferme pas sur \" → { : , :
    assert m.json_structure(b'{"k\\\\": "x\\"y,:", "z": 1}') == 4
    assert m.json_structure(b"[" + b"{}," * 10_000 + b"{}]") > m.MAX_JSON_STRUCTURE
    for body in (INIT_FULL, REGISTER_40, VAULT_WRITE):  # messages légitimes les plus structurés : large marge
        assert m.json_structure(json.dumps(body).encode()) * 10 < m.MAX_JSON_STRUCTURE


async def test_dense_bodies_rejected_before_the_app() -> None:
    called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    dense = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"help","junk":[' + b"{}," * 5000 + b'{}]}}'

    async def receive() -> Any:
        return {"type": "http.request", "body": dense, "more_body": False}

    guard = BodyLimitMiddleware(inner)
    for path, method in (("/mcp/", "POST"), ("/api/vault/a.md", "PUT"), ("/api/login", "POST")):
        send = Recorder()
        await guard(scope(path, len(dense), method), receive, send)
        assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR", path
        assert "{}" not in send.json["message"] and "base64" in send.json["hint"]
    assert not called and guard.budget.used == 0


async def test_legit_bodies_reach_the_app_intact() -> None:
    got: list[bytes] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        got.append((await receive())["body"])
        await ok(scope, receive, send)

    guard = BodyLimitMiddleware(inner)
    for body in (INIT_FULL, REGISTER_40, VAULT_WRITE):
        raw = json.dumps(body).encode()

        async def receive(raw: bytes = raw) -> Any:
            return {"type": "http.request", "body": raw, "more_body": False}

        send = Recorder()
        await guard(scope("/mcp/", len(raw)), receive, send)
        assert send.status == 200 and got[-1] == raw


async def test_single_hold_never_sends_a_second_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_HOLD", 0.1)
    parts = [b"ab", b"cd"]

    async def slow_receive() -> Any:
        await asyncio.sleep(0.04)  # lecture lente : une partie de la borne déjà consommée
        return {"type": "http.request", "body": parts.pop(0), "more_body": bool(parts)}

    async def started_then_stalled(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(3600)

    guard = BodyLimitMiddleware(started_then_stalled)
    send = Recorder()
    await asyncio.wait_for(guard(scope("/api/vault/a.md", method="PUT", chunked=True), slow_receive, send), 2)
    assert [x["type"] for x in send.messages] == ["http.response.start"] and guard.budget.used == 0


def _structure(v: Any) -> int:
    """Oracle indépendant du scan : { [ , : que le parseur a réellement vus."""
    if isinstance(v, dict):
        return (2 * len(v) if v else 1) + sum(_structure(x) for x in v.values())
    if isinstance(v, list):
        return 1 + max(len(v) - 1, 0) + sum(_structure(x) for x in v)
    return 0


def _random_json(rng: random.Random, depth: int = 0) -> Any:
    alphabet = ['"', "\\", "{", "[", ",", ":", "a", "é", "Ģ", "😀", "\n", "\\\\", '\\"', "u"]
    kind = rng.randrange(5 if depth < 3 else 3)
    if kind == 0:
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
    if kind == 1:
        return rng.choice([1, -2.5, True, None])
    if kind == 2:
        return ""
    if kind == 3:
        return [_random_json(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {str(_random_json(rng, 3)): _random_json(rng, depth + 1) for _ in range(rng.randrange(4))}


def test_json_structure_matches_the_parser_on_valid_json() -> None:
    rng = random.Random(4)  # noqa: S311 - fuzz déterministe
    for _ in range(3000):
        value = _random_json(rng)
        raw = json.dumps(value, ensure_ascii=rng.random() < 0.5, indent=rng.choice([None, 1])).encode()
        assert m.json_structure(raw) == _structure(json.loads(raw)), raw


def test_json_structure_is_linear_on_adversarial_strings() -> None:
    # N5 : une chaîne non fermée faite de \" était rebalayée à chaque guillemet (O(N²) : 6,7 s pour 64 Kio)
    t = time.perf_counter()
    m.json_structure(b'"' + b'\\"' * (32 * KIB))
    assert time.perf_counter() - t < 0.5
    n = m.MAX_UPLOAD_BYTES  # plus grand plafond de route, scanné avant toute authentification
    for body in (b'"' + b'\\"' * (n // 2), b'"' * n, b'"' + b"\\" * n, b'"\\' * (n // 2), b'"a"' * (n // 3)):
        t = time.perf_counter()
        m.json_structure(body)
        assert time.perf_counter() - t < 1.0, body[:8]


async def test_non_utf8_bodies_rejected_before_the_scan() -> None:
    # N4 : en UTF-16, un caractère comme Ģ (22 01) fournit un faux octet guillemet et cache la structure au scan,
    # alors que json.loads (Starlette, SDK MCP) décode ces corps sans broncher.
    called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True
        await ok(scope, receive, send)

    value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "help", "arguments": {}, "a": "Ģ", "junk": [{}] * 5000, "z": "Ģ"}}
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    guard = BodyLimitMiddleware(inner)
    for codec in ("utf-16-le", "utf-16-be", "utf-16", "utf-32-le", "utf-32"):
        raw = text.encode(codec)
        assert len(raw) <= m.MCP_BODY_BYTES and json.loads(raw) == value

        async def receive(raw: bytes = raw) -> Any:
            return {"type": "http.request", "body": raw, "more_body": False}

        for path, method in (("/mcp/", "POST"), ("/api/vault/a.md", "PUT")):
            send = Recorder()
            await guard(scope(path, len(raw), method), receive, send)
            assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR", (codec, path)
            assert "UTF-8" in send.json["hint"] and "Ģ" not in json.dumps(send.json), codec
    assert not called and guard.budget.used == 0
    bom = b"\xef\xbb\xbf" + json.dumps(INIT_FULL).encode()  # utf-8-sig : accepté par json.loads, scanné normalement

    async def receive_bom() -> Any:
        return {"type": "http.request", "body": bom, "more_body": False}

    send = Recorder()
    await guard(scope("/mcp/", len(bom)), receive_bom, send)
    assert send.status == 200 and called
