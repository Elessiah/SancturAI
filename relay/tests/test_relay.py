import json
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from relay import MAX_ATTEMPTS, MAX_PAYLOAD_BYTES, backoff_seconds, format_payload, process_batch

ROOT = Path(__file__).resolve().parents[2]
RELAY_PASSWORD = "relay-test-pw"  # noqa: S105
# Exactement les droits de deploy/deploy.sh
RELAY_GRANTS = """
CREATE ROLE relay LOGIN PASSWORD 'relay-test-pw';
REVOKE UPDATE ON outbox FROM relay;
GRANT USAGE ON SCHEMA public TO relay;
GRANT SELECT, UPDATE (sent_at, attempts, next_attempt_at) ON outbox TO relay;
"""
GOOD_AGENT = {"handle": "hhh", "agent_id": "y"}


@pytest.fixture(scope="module")
def pg() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:17-alpine", driver=None) as container:
        yield container


@pytest.fixture(scope="module")
def conn(pg: PostgresContainer) -> Iterator[psycopg.Connection]:
    """Connexion superutilisateur (préparation des données) ; le schéma vient des migrations Alembic de l'app."""
    admin_url = pg.get_connection_url()
    env = {**os.environ, "SANCTURAI_DATABASE_URL": admin_url.replace("postgresql://", "postgresql+asyncpg://", 1)}
    subprocess.run(["uv", "run", "--project", str(ROOT), "alembic", "upgrade", "head"],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(RELAY_GRANTS)  # type: ignore[arg-type]
        yield c


@pytest.fixture(scope="module")
def rconn(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Connexion avec le rôle `relay` aux droits de production."""
    with psycopg.connect(conn.info.dsn, user="relay", password=RELAY_PASSWORD, autocommit=True) as c:
        yield c


def _insert(conn: psycopg.Connection, kind: str, payload: dict[str, object]) -> None:
    conn.execute("INSERT INTO outbox (kind, payload) VALUES (%s, %s)", (kind, json.dumps(payload)))


def test_format_payload() -> None:
    p = format_payload("agent_message", {"text": "hi", "short_id": "ab12", "agent": {"handle": "xyz", "agent_id": "y"}})
    assert p == {"kind": "agent_message", "text": "hi", "short_id": "ab12", "agent": {"handle": "xyz", "agent_id": "y"}}


@pytest.mark.parametrize("short_id", ["ab12", "ab123", "ab12cd"])
def test_format_payload_accepts_4_to_6_hex_short_id(short_id):
    p = format_payload("agent_message", {"text": "hi", "short_id": short_id, "agent": {"handle": "xyz", "agent_id": "y"}})
    assert p["short_id"] == short_id
    assert format_payload("log", {"text": "t", "extra": 1}) == {"kind": "log", "text": "t"}


def test_success_and_failure(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "ok"})
    _insert(conn, "log", {"text": "fail"})
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["x-internal-token"] == "tok"
        return httpx.Response(500 if body["text"] == "fail" else 200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 2
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 0  # backoff : rien d'éligible
    rows = conn.execute("SELECT payload->>'text', sent_at IS NOT NULL, attempts FROM outbox ORDER BY id").fetchall()
    assert rows == [("ok", True, 0), ("fail", False, 1)]


def test_no_row_lock_held_during_http_call(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "slow"})
    seen: list[tuple[object, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with psycopg.connect(conn.info.dsn, password=conn.info.password, autocommit=True) as other:
            other.execute("SET lock_timeout = '500ms'")
            other.execute("UPDATE outbox SET attempts = attempts")  # bloquerait si la ligne était verrouillée
            seen.append(tuple(other.execute("SELECT next_attempt_at > now() FROM outbox").fetchone() or ()))
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 1
    assert seen == [(True,)]  # bail posé pendant l'appel : un second relais ne reprendrait pas l'événement


def test_gives_up_after_max_attempts(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "dead"})
    conn.execute("UPDATE outbox SET attempts = %s", (MAX_ATTEMPTS,))
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 0


def test_backoff_survives_a_48h_bot_outage() -> None:
    assert backoff_seconds(0) == 5 and backoff_seconds(3) == 40
    assert max(backoff_seconds(a) for a in range(MAX_ATTEMPTS)) == 3600  # plafond 1 h
    # dernière tentative après la somme des attentes des MAX_ATTEMPTS - 1 échecs précédents
    assert sum(backoff_seconds(a) for a in range(MAX_ATTEMPTS - 1)) >= 48 * 3600


def test_failure_schedules_next_attempt_with_backoff(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "fail"})
    conn.execute("UPDATE outbox SET attempts = 20")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 1
    delay = conn.execute("SELECT extract(epoch FROM next_attempt_at - now()) FROM outbox").fetchone()
    assert delay is not None and 3500 < float(delay[0]) <= 3600


def test_400_is_permanent_and_not_retried(conn: psycopg.Connection, rconn: psycopg.Connection,
                                          caplog: pytest.LogCaptureFixture) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "bad"})
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "invalid"})

    with caplog.at_level("WARNING", logger="sancturai-relay"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            assert process_batch(rconn, http, "http://bot/notify", "tok") == 1
            conn.execute("UPDATE outbox SET next_attempt_at = now()")
            assert process_batch(rconn, http, "http://bot/notify", "tok") == 0
    assert calls == 1
    row = conn.execute("SELECT attempts, sent_at FROM outbox").fetchone()
    assert row == (MAX_ATTEMPTS, None)  # abandonnée : la purge la retirera
    assert any("abandon" in r.getMessage() for r in caplog.records)
    assert "bad" not in caplog.text  # le texte n'est pas journalisé


def test_401_and_503_are_retried(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "a"})
    for status in (401, 503):
        conn.execute("UPDATE outbox SET next_attempt_at = now()")
        with httpx.Client(transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s))) as http:
            process_batch(rconn, http, "http://bot/notify", "tok")
    assert conn.execute("SELECT attempts, sent_at FROM outbox").fetchone() == (2, None)


@pytest.mark.parametrize(("kind", "payload"), [
    ("other", {"text": "x"}),
    ("log", {"text": {"nested": "x"}}),
    ("log", {"text": "x" * 1801}),
    ("log", {"text": ""}),
    ("log", {"text": " \n\t"}),
    ("log", {}),
    ("agent_message", {"text": "x", "short_id": "ab12"}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "h" * 33, "agent_id": "y"}}),
    ("agent_message", {"text": "x", "short_id": ["ab12"], "agent": GOOD_AGENT}),
    ("agent_message", {"text": "x", "short_id": "AB12", "agent": GOOD_AGENT}),
    ("agent_message", {"text": "x", "short_id": "abc", "agent": GOOD_AGENT}),
    ("agent_message", {"text": "x", "short_id": "ab12345", "agent": GOOD_AGENT}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "", "agent_id": "y"}}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "HHH", "agent_id": "y"}}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "hh", "agent_id": "y"}}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "hhh", "agent_id": ""}}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "hhh", "agent_id": "a\u202eb"}}),
    ("agent_message", {"text": "x", "short_id": "ab12", "agent": {"handle": "hhh", "agent_id": "a\nb"}}),
])
def test_format_payload_rejects_out_of_contract_rows(kind: str, payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        format_payload(kind, payload)


def test_format_payload_keeps_only_allow_listed_agent_fields() -> None:
    p = format_payload("agent_message", {"text": "hi", "short_id": "ab12",
                                         "agent": {"handle": "xyz", "agent_id": "y", "url": "http://evil"}})
    assert p["agent"] == {"handle": "xyz", "agent_id": "y"}


def test_out_of_contract_and_oversized_rows_are_abandoned_unsent(conn: psycopg.Connection, rconn: psycopg.Connection,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": {"secret-agent-text": 1}})
    _insert(conn, "log", {"text": "ok", "pad": "secret-agent-text" * (MAX_PAYLOAD_BYTES // 10)})
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with caplog.at_level("WARNING", logger="sancturai-relay"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            assert process_batch(rconn, http, "http://bot/notify", "tok") == 2
    assert calls == 0
    assert conn.execute("SELECT attempts, sent_at FROM outbox").fetchall() == [(MAX_ATTEMPTS, None)] * 2
    assert "secret-agent-text" not in caplog.text


def test_poison_rows_do_not_crash_or_stall_the_batch(conn: psycopg.Connection, rconn: psycopg.Connection,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    """I1 : un JSONB indécodable par Python est abandonné ; son voisin du lot part quand même."""
    conn.execute("TRUNCATE outbox")
    deep = '{"text":"a","d":' + "[" * 5000 + "]" * 5000 + "}"
    big_int = '{"text":"a","n":' + "9" * 5000 + "}"
    for raw in (deep, big_int):
        conn.execute("INSERT INTO outbox (kind, payload) VALUES ('log', %s::jsonb)", (raw,))
    _insert(conn, "log", {"text": "good"})
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200)

    with caplog.at_level("WARNING", logger="sancturai-relay"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            assert process_batch(rconn, http, "http://bot/notify", "tok") == 3
    # la limite de récursion du décodeur varie selon l'interpréteur : une ligne profonde qu'il décode est un
    # journal valide (seul `text` est relayé) ; l'entier géant, lui, est toujours refusé (int_max_str_digits)
    deep_ok = _decodes(deep)
    assert sent == (["a", "good"] if deep_ok else ["good"])
    rows = conn.execute("SELECT attempts, sent_at IS NOT NULL FROM outbox ORDER BY id").fetchall()
    assert rows == [(0, True) if deep_ok else (MAX_ATTEMPTS, False), (MAX_ATTEMPTS, False), (0, True)]


def _decodes(raw: str) -> bool:
    try:
        json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return True
    assert "violates the notify contract" in caplog.text


def test_unexpected_error_on_one_row_does_not_stop_the_batch(conn: psycopg.Connection, rconn: psycopg.Connection,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    import relay

    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "boom"})
    _insert(conn, "log", {"text": "good"})
    real = relay.format_payload

    def flaky(kind: str, payload: dict[str, object]) -> dict[str, object]:
        if payload["text"] == "boom":
            raise KeyError("unexpected")
        return real(kind, payload)

    monkeypatch.setattr(relay, "format_payload", flaky)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 2
    rows = conn.execute("SELECT attempts, sent_at IS NOT NULL FROM outbox ORDER BY id").fetchall()
    assert rows == [(1, False), (0, True)]


def test_mark_is_ignored_when_the_lease_was_lost(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    """M3 : si un autre relais a repris la ligne (bail changé), notre règlement tardif n'écrase rien."""
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "slow"})

    def handler(request: httpx.Request) -> httpx.Response:
        conn.execute("UPDATE outbox SET next_attempt_at = now() + interval '1 hour'")  # bail repris ailleurs
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 1
    assert conn.execute("SELECT sent_at IS NOT NULL FROM outbox").fetchone() == (False,)


def test_batch_stops_when_budget_is_exhausted(conn: psycopg.Connection, rconn: psycopg.Connection,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import relay

    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "a"})
    _insert(conn, "log", {"text": "b"})
    monkeypatch.setattr(relay, "BATCH_BUDGET", -1)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert process_batch(rconn, http, "http://bot/notify", "tok") == 2
    assert calls == 0  # rien d'envoyé ; les bails expireront


def test_relay_role_has_only_column_level_update(conn: psycopg.Connection, rconn: psycopg.Connection) -> None:
    """M6 : le rôle de production ne peut pas toucher payload/kind."""
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "x"})
    for stmt in ("UPDATE outbox SET payload = '{}'", "UPDATE outbox SET kind = 'log'", "DELETE FROM outbox",
                 "INSERT INTO outbox (kind, payload) VALUES ('log', '{}')"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rconn.execute(stmt)  # type: ignore[call-overload]


def test_relay_works_with_deploy_grants_only(conn: psycopg.Connection) -> None:
    """Droits réels de prod (extraits de deploy.sh) : bail, envoi, échec et abandon passent, kind/payload restent intouchables."""
    sh = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    m = re.search(r'-c "(REVOKE UPDATE ON outbox FROM relay;[^"]*)"', sh)
    assert m is not None
    grants = m.group(1).replace(" relay;", " relay_deploy;").replace(" relay ", " relay_deploy ")
    assert "relay_deploy" in grants and " relay" not in grants.replace("relay_deploy", "")
    conn.execute("TRUNCATE outbox")
    conn.execute("DROP ROLE IF EXISTS relay_deploy")
    conn.execute("CREATE ROLE relay_deploy LOGIN PASSWORD 'relay-test-pw'")
    conn.execute(grants)  # type: ignore[arg-type]
    for text in ("ok", "fail", "bad"):
        _insert(conn, "log", {"text": text})
    _insert(conn, "agent_message", {"text": "x"})  # hors contrat (short_id/agent absents) : abandon

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response({"ok": 200, "fail": 503, "bad": 400}[json.loads(request.content)["text"]])

    try:
        with psycopg.connect(conn.info.dsn, user="relay_deploy", password="relay-test-pw", autocommit=True) as rc:
            with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                assert process_batch(rc, http, "http://bot/notify", "tok") == 4
            for column in ("payload = '{}'", "kind = 'log'"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    rc.execute(f"UPDATE outbox SET {column}")  # type: ignore[call-overload]  # noqa: S608
        rows = conn.execute("SELECT kind, sent_at IS NOT NULL, attempts FROM outbox ORDER BY id").fetchall()
        assert rows == [("log", True, 0), ("log", False, 1), ("log", False, MAX_ATTEMPTS), ("agent_message", False, MAX_ATTEMPTS)]
    finally:
        conn.execute("DROP OWNED BY relay_deploy")
        conn.execute("DROP ROLE relay_deploy")


@pytest.mark.parametrize("token", [None, "", "  "])
def test_main_refuses_to_start_without_bot_token(monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    import relay

    monkeypatch.setenv("SANCTURAI_RELAY_DSN", "postgresql://unused")
    if token is None:
        monkeypatch.delenv("SANCTURAI_BOT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("SANCTURAI_BOT_TOKEN", token)
    monkeypatch.setattr(relay.httpx, "Client", lambda **kw: pytest.fail("client created without token"))
    with pytest.raises(SystemExit):
        relay.main()


def test_main_http_client_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import relay

    monkeypatch.setenv("SANCTURAI_RELAY_DSN", "postgresql://unused")
    monkeypatch.setenv("SANCTURAI_BOT_TOKEN", "tok")
    seen: dict[str, object] = {}

    class Stop(Exception):
        pass

    def fake_client(**kw: object) -> None:
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(relay.httpx, "Client", fake_client)
    with pytest.raises(Stop):
        relay.main()
    assert seen["trust_env"] is False
