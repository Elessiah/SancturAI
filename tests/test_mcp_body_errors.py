import asyncio
import json
import time
from typing import Any

import pytest

import app.middleware as m
from app.middleware import BodyLimitMiddleware, JsonRpcHead, jsonrpc_head
from tests.test_body_limit import Recorder, empty_receive, scope

KIB = 1024
CALL = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"vault_write","arguments":{"path":"/a.md",'


def head_of(raw: bytes) -> JsonRpcHead | None:
    return jsonrpc_head(raw[:m.MCP_HEAD_BYTES], raw[-m.MCP_HEAD_BYTES:])


def sdk_call(content: str) -> bytes:
    """Ordre de sérialisation du SDK Python officiel : id en fin de corps."""
    return json.dumps({"method": "tools/call", "params": {"name": "vault_write", "arguments": {
        "path": "/a.md", "content": content}}, "jsonrpc": "2.0", "id": 3}, separators=(",", ":")).encode()


def test_head_of_a_truncated_tool_call() -> None:
    assert head_of(CALL + b'"content":"' + b"x" * 10_000) == JsonRpcHead(3, "tools/call", "vault_write")
    assert head_of(b' \r\n{ "id" : "req-1" , "method":"tools/call", "params" : { "arguments" : {"a": [1, {"b": "}"}]},'
                   b' "name": "help" } }') == JsonRpcHead("req-1", "tools/call", "help")
    assert head_of(b'{"method":"tools/list","jsonrpc":"2.0","id":0}') == JsonRpcHead(0, "tools/list", None)
    assert head_of(b'{"jsonrpc":"2.0","id":-12,"method":"initialize","params":{"name":"x"}}') == \
        JsonRpcHead(-12, "initialize", "x")
    assert head_of(b'\xef\xbb\xbf{"id":5,"method":"ping"}') == JsonRpcHead(5, "ping", None)


def test_head_of_an_official_sdk_call_with_the_id_at_the_end() -> None:
    assert head_of(sdk_call("x" * 100_000)) == JsonRpcHead(3, "tools/call", "vault_write")
    assert head_of(sdk_call('a"b\\' * 3000)) == JsonRpcHead(3, "tools/call", "vault_write")
    assert head_of(b'{"params":{"name":"help"},"method":"tools/call","pad":"' + b"x" * 9000
                   + b'", "jsonrpc" : "2.0" ,\n "id" : "r-9"\n}\n') == JsonRpcHead("r-9", "tools/call", "help")
    # id en tête et en fin : accepté s'il est identique, refusé sinon
    assert head_of(b'{"id":3,"pad":"' + b"x" * 9000 + b'","id":3}') == JsonRpcHead(3, None, None)
    assert head_of(b'{"id":3,"pad":"' + b"x" * 9000 + b'","id":4}') is None
    assert head_of(b'{"method":"a","pad":"' + b"x" * 9000 + b'","method":"b","id":1}') == JsonRpcHead(1, None, None)


@pytest.mark.parametrize("raw", [
    b"", b"x", b"[", b"[{\"id\":1}]", b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
    b'{"id":null,"method":"ping"}', b'{"id":true}', b'{"id":1.5}', b'{"id":1e3}', b'{"id":{"a":1}}', b'{"id":[1]}',
    b'{"id":12345678901234567890}', b'{"id":"' + b"a" * 129 + b'"}', b'{"id":"a\\"b"}', b'{"id":"a\\u0041"}',
    b'{"id":"a\x01b"}', b'{"id":""}', b'{"id":1,"id":2}', b'{"id":"\xc3\xa9"}', b'{"id":01}', b'{"id":-}',
    b'{"id"', b'{"id":', b'{"id":"abc', b'{"id" 1}', b'{id:1}', b'{"a":1 "id":2}',
    # l'id n'est cherché qu'au premier niveau : jamais dans les arguments ou une chaîne
    b'{"method":"tools/call","params":{"name":"a","arguments":{"id":9}}}',
    b'{"method":"tools/call","params":{"name":"a","arguments":{"c":"\\"id\\":9"}}}',
    b'{"x":"\\"id\\":9,","y":1}',
    b'{"pad":"' + b"x" * 5000 + b'","id":1,"pad2":"' + b"y" * 5000 + b'"}',  # ni en tête ni en fin
    # fin de corps : jamais lue dans une chaîne ou un objet imbriqué
    b'{"a":"' + b"x" * 5000 + b'\\",\\"id\\":9}"}', b'{"a":"' + b"x" * 5000 + b'\\",\\"id\\":9}',
    b'{"a":"' + b"x" * 5000 + b'\\\\\\",\\"id\\":9}"}', b'{"a":{"pad":"' + b"x" * 5000 + b'","id":9}}',
    b'{"a":["' + b"x" * 5000 + b'",{"id":9}]}', b'{"pad":"' + b"x" * 5000 + b'","id":9',
    b'{"pad":"' + b"x" * 5000 + b'" "id":9}', b'{"pad":"' + b"x" * 5000 + b'",id:9}',
    b'{"pad":"' + b"x" * 5000 + b'","id":"a\\"b"}', b'{"pad":"' + b"x" * 5000 + b'","id":"' + b"a" * 129 + b'"}',
    b'{"pad":"' + b"x" * 5000 + b'","id":1.5}', b'{"pad":"' + b"x" * 5000 + b'","id":true}',
])
def test_no_usable_id(raw: bytes) -> None:
    assert head_of(raw) is None, raw[:60]


@pytest.mark.parametrize("name", [b'"<script>"', b'"a b"', b'"' + b"a" * 65 + b'"', b"1", b"null", b'["help"]',
                                  b'"vault\\u005fwrite"', b'"x\\n"'])
def test_abnormal_tool_name_is_dropped(name: bytes) -> None:
    raw = b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":' + name + b',"arguments":{}}}'
    assert head_of(raw) == JsonRpcHead(7, "tools/call", None)


def test_duplicate_or_abnormal_method_and_name() -> None:
    assert head_of(b'{"id":7,"method":"tools/call","method":"ping"}') == JsonRpcHead(7, None, None)
    assert head_of(b'{"id":7,"method":"tools/call","params":{"name":"a","name":"b"}}') == \
        JsonRpcHead(7, "tools/call", None)
    assert head_of(b'{"id":7,"method":3}') == JsonRpcHead(7, None, None)


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-16", "utf-32-le", "utf-32-be", "utf-32"])
def test_head_of_utf16_and_utf32_prefixes(codec: str) -> None:
    raw = (CALL.decode() + '"content":"é' + "Ģ" * 3000).encode(codec)
    for cut in (len(raw), m.MCP_HEAD_BYTES + 1, 4 * KIB - 1, 4 * KIB - 3):
        assert head_of(raw[:cut]) == JsonRpcHead(3, "tools/call", "vault_write"), (codec, cut)
    sdk = sdk_call("é" + "Ģ" * 3000).decode().encode(codec)
    assert head_of(sdk) == JsonRpcHead(3, "tools/call", "vault_write"), codec


@pytest.mark.parametrize("raw", [
    b'{"a":' + b"[" * 10_000, b'{"a":"' + b'\\"' * 100_000, b'{"a":' + b'"",' * 100_000, b'{' + b'"k":1,' * 100_000,
    b'{"a":{' + b'"k":{' * 50_000, b'{"a":' + b"[{" * 50_000 + b"x", b'{"' + b"\\" * 200_000, b"{" * 200_000,
    b'{"id":1,"params":' + b'{"a":' * 50_000, b'{"a":"' + b"\x00" * 200_000,
])
def test_extractor_is_bounded_and_linear(raw: bytes) -> None:
    tails = [raw[::-1][:5000], b'"' * 5000 + b"}", b'"\\' * 3000 + b"}", b":" * 5000 + b"}", b'":"a",' * 900 + b"}",
             b"1" * 5000 + b"}", b" " * 5000 + b"}", b'"a":1,' * 900 + b"}", raw[:5000] + b"}"]
    start = time.perf_counter()
    for _ in range(3):
        for tail in tails:
            jsonrpc_head(raw, tail)
    assert time.perf_counter() - start < 0.5


async def _guard_reply(raw: bytes, length: int | None = None, headers: list[tuple[bytes, bytes]] | None = None,
                       path: str = "/mcp/", chunk: int = 1024, **kw: Any) -> tuple[Recorder, int]:
    reads = 0

    async def receive() -> Any:
        nonlocal reads
        part = raw[reads * chunk:(reads + 1) * chunk]
        reads += 1
        return {"type": "http.request", "body": part, "more_body": reads * chunk < len(raw)}

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("rejected bodies never reach the app")

    sc = scope(path, len(raw) if length is None else length, **kw)
    sc["headers"] += headers or []
    send = Recorder()
    await BodyLimitMiddleware(inner)(sc, receive, send)
    return send, reads


def _header(send: Recorder, name: bytes) -> bytes | None:
    return dict(send.headers).get(name)


async def test_oversized_tool_call_gets_a_tool_error() -> None:
    raw = sdk_call("x" * (300 * KIB))
    send, reads = await _guard_reply(raw, headers=[(b"mcp-session-id", b"0123abcd")], chunk=64 * KIB)
    assert send.status == 200 and reads == 5  # lu jusqu'au bout (sans être gardé) : l'id est à la fin
    assert _header(send, b"content-type") == b"application/json"
    assert _header(send, b"mcp-session-id") == b"0123abcd" and _header(send, b"cache-control") == b"no-store"
    reply = send.json
    assert reply["jsonrpc"] == "2.0" and reply["id"] == 3 and reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["content"][0]["type"] == "text"
    assert text.startswith("Error executing tool vault_write: {")
    err = json.loads(text[text.index("{"):])
    assert err["error"] == "VALIDATION_ERROR" and "/api/vault/" in err["hint"]
    assert "xxxx" not in json.dumps(reply)


async def test_huge_declared_body_reads_only_the_head() -> None:
    # vidange bornée au plafond nginx de /mcp : un client direct ne garde pas une connexion pour 16 Mio
    assert m.MCP_DRAIN_BYTES == m.NGINX_MCP_BODY_BYTES == 1024 * 1024 >= m.MCP_BODY_BYTES
    head_first = CALL + b'"content":"x"}}}'
    send, reads = await _guard_reply(head_first, length=m.MCP_DRAIN_BYTES + 1, chunk=1024)
    assert send.status == 200 and reads == 1 and send.json["id"] == 3
    raw = sdk_call("x" * (64 * KIB))
    send, reads = await _guard_reply(raw, length=m.MCP_DRAIN_BYTES + 1, chunk=1024)
    assert send.status == 422 and reads == 4 and send.json["error"] == "VALIDATION_ERROR"


async def test_chunked_overflow_drains_to_find_the_id() -> None:
    raw = sdk_call("x" * (300 * KIB))
    send, reads = await _guard_reply(raw, length=None, chunk=64 * KIB, chunked=True)
    assert send.status == 200 and send.json["id"] == 3 and reads == 5


async def test_chunked_overflow_releases_its_reservation_before_draining() -> None:
    raw = sdk_call("x" * (600 * KIB))
    chunk = 64 * KIB
    used: list[int] = []
    reads = 0

    async def receive() -> Any:
        nonlocal reads
        used.append(guard.budget.used)
        part = raw[reads * chunk:(reads + 1) * chunk]
        reads += 1
        return {"type": "http.request", "body": part, "more_body": reads * chunk < len(raw)}

    guard = BodyLimitMiddleware(empty_receive)  # type: ignore[arg-type]
    send = Recorder()
    await guard(scope("/mcp/", chunked=True), receive, send)
    assert send.status == 200 and send.json["id"] == 3
    overflow = m.MCP_BODY_BYTES // chunk + 1  # lecture qui dépasse le plafond, puis vidange
    assert reads > overflow + 2  # lectures de vidange observées
    assert used[:overflow] == [m.MCP_BODY_BYTES] * overflow and used[overflow:] == [0] * (reads - overflow)
    assert guard.budget.used == 0


def _stalling_receive(first: bytes, then: str) -> Any:
    """Premier morceau, puis client muet (`stall`) ou parti (`disconnect`)."""
    calls = 0

    async def receive() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": first, "more_body": True}
        if then == "stall":
            await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return receive


@pytest.mark.parametrize("then", ["stall", "disconnect"])
async def test_drain_stops_on_timeout_or_disconnect(monkeypatch: pytest.MonkeyPatch, then: str) -> None:
    monkeypatch.setattr(m, "MCP_DRAIN_WAIT", 0.2)
    for raw, chunked in ((sdk_call("x" * (300 * KIB)), False), (sdk_call("x" * (300 * KIB)), True),
                         (CALL + b'"content":"' + b"x" * (300 * KIB), False)):
        first = raw[:64 * KIB]
        guard = BodyLimitMiddleware(empty_receive)  # type: ignore[arg-type]
        send = Recorder()
        start = time.perf_counter()
        sc = scope("/mcp/", chunked=True) if chunked else scope("/mcp/", len(raw))
        if chunked:  # le corps chunked dépasse le plafond en lecture : premier morceau de 256 Kio + 1
            first = raw[:m.MCP_BODY_BYTES + 1]
        await guard(sc, _stalling_receive(first, then), send)
        assert time.perf_counter() - start < 2 and guard.budget.used == 0
        assert send.messages[0]["type"] == "http.response.start" and len(send.messages) == 2  # une seule réponse
        if raw.startswith(CALL):  # id en tête : erreur d'outil malgré la fin inconnue
            assert send.status == 200 and send.json["id"] == 3
        else:  # id en fin, jamais reçue : statut HTTP habituel
            assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR"


async def test_other_requests_get_a_jsonrpc_error() -> None:
    raw = b'{"jsonrpc":"2.0","id":"i-1","method":"initialize","params":{"pad":"' + b"x" * (300 * KIB) + b'"}}'
    send, _ = await _guard_reply(raw, headers=[(b"mcp-session-id", b"bad\x7fid")])
    assert send.status == 200 and _header(send, b"mcp-session-id") is None
    reply = send.json
    assert reply["id"] == "i-1" and "result" not in reply
    assert reply["error"]["code"] == -32600
    assert reply["error"]["message"].startswith("VALIDATION_ERROR: request body exceeds")
    assert reply["error"]["data"]["error"] == "VALIDATION_ERROR" and "/api/vault/" in reply["error"]["data"]["hint"]
    named = b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"<b>","x":"' + b"x" * (300 * KIB) + b'"}}'
    send, _ = await _guard_reply(named)
    assert send.json["id"] == 4 and send.json["error"]["code"] == -32600 and "<b>" not in json.dumps(send.json)


async def test_dense_and_utf16_mcp_bodies_get_tool_errors() -> None:
    dense = CALL + b'"content":"x"},"junk":[' + b"{}," * 2000 + b"{}]}}"
    send, _ = await _guard_reply(dense, chunk=len(dense))
    text = send.json["result"]["content"][0]["text"]
    assert send.status == 200 and "base64" in json.loads(text[text.index("{"):])["hint"]
    utf16 = (CALL.decode() + '"content":"x"}}}').encode("utf-16")
    send, _ = await _guard_reply(utf16, chunk=len(utf16))
    text = send.json["result"]["content"][0]["text"]
    assert send.status == 200 and send.json["id"] == 3 and "UTF-8" in json.loads(text[text.index("{"):])["hint"]


async def test_busy_tool_call_keeps_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "BODY_BUDGET_WAIT", 0.05)
    raw = CALL + b'"content":"x"}}}'
    reads = 0

    async def receive() -> Any:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": raw, "more_body": False}

    guard = BodyLimitMiddleware(empty_receive, budget_bytes=10)  # type: ignore[arg-type]
    send = Recorder()
    await guard(scope("/mcp/", len(raw)), receive, send)
    assert send.status == 200 and _header(send, b"retry-after") == b"5"
    err_text = send.json["result"]["content"][0]["text"]
    err = json.loads(err_text[err_text.index("{"):])
    assert err["error"] == "SERVER_BUSY" and err["retry_after"] == 5 and guard.budget.used == 0


async def test_without_id_or_outside_mcp_post_status_codes_are_unchanged() -> None:
    notif = b'{"jsonrpc":"2.0","method":"notifications/initialized","pad":"' + b"x" * (300 * KIB) + b'"}'
    send, _ = await _guard_reply(notif)
    assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR"
    rest = CALL + b'"content":"' + b"x" * (100 * KIB) + b'"}}}'
    send, reads = await _guard_reply(rest, path="/api/login")
    assert send.status == 422 and send.json["error"] == "VALIDATION_ERROR" and reads == 0
    send, _ = await _guard_reply(b'{"id":1,"pad":"' + b"x" * (300 * KIB) + b'"}', method="PUT")
    assert send.status == 422
