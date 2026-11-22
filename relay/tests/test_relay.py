import json
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from relay import MAX_ATTEMPTS, backoff_seconds, format_payload, process_batch

SCHEMA = """
CREATE TABLE outbox (id bigint generated always as identity primary key, kind varchar(32) not null,
  payload jsonb not null, created_at timestamptz not null default now(),
  next_attempt_at timestamptz not null default now(), sent_at timestamptz, attempts integer not null default 0);
"""


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    with PostgresContainer("postgres:17-alpine", driver=None) as pg:
        with psycopg.connect(pg.get_connection_url(), autocommit=True) as c:
            c.execute(SCHEMA)
            yield c


def _insert(conn: psycopg.Connection, kind: str, payload: dict[str, object]) -> None:
    conn.execute("INSERT INTO outbox (kind, payload) VALUES (%s, %s)", (kind, json.dumps(payload)))


def test_format_payload() -> None:
    p = format_payload("agent_message", {"text": "hi", "short_id": "ab12", "agent": {"handle": "x", "agent_id": "y"}})
    assert p == {"kind": "agent_message", "text": "hi", "short_id": "ab12", "agent": {"handle": "x", "agent_id": "y"}}
    assert format_payload("log", {"text": "t", "extra": 1}) == {"kind": "log", "text": "t"}


def test_success_and_failure(conn: psycopg.Connection) -> None:
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
        assert process_batch(conn, http, "http://bot/notify", "tok") == 2
        assert process_batch(conn, http, "http://bot/notify", "tok") == 0  # backoff : rien d'éligible
    rows = conn.execute("SELECT payload->>'text', sent_at IS NOT NULL, attempts FROM outbox ORDER BY id").fetchall()
    assert rows == [("ok", True, 0), ("fail", False, 1)]


def test_no_row_lock_held_during_http_call(conn: psycopg.Connection) -> None:
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
        assert process_batch(conn, http, "http://bot/notify", "tok") == 1
    assert seen == [(True,)]  # bail posé pendant l'appel : un second relais ne reprendrait pas l'événement


def test_gives_up_after_max_attempts(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "dead"})
    conn.execute("UPDATE outbox SET attempts = %s", (MAX_ATTEMPTS,))
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as http:
        assert process_batch(conn, http, "http://bot/notify", "tok") == 0


def test_backoff_survives_a_48h_bot_outage() -> None:
    assert backoff_seconds(0) == 5 and backoff_seconds(3) == 40
    assert max(backoff_seconds(a) for a in range(MAX_ATTEMPTS)) == 3600  # plafond 1 h
    # dernière tentative après la somme des attentes des MAX_ATTEMPTS - 1 échecs précédents
    assert sum(backoff_seconds(a) for a in range(MAX_ATTEMPTS - 1)) >= 48 * 3600


def test_failure_schedules_next_attempt_with_backoff(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "fail"})
    conn.execute("UPDATE outbox SET attempts = 20")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        assert process_batch(conn, http, "http://bot/notify", "tok") == 1
    delay = conn.execute("SELECT extract(epoch FROM next_attempt_at - now()) FROM outbox").fetchone()
    assert delay is not None and 3500 < float(delay[0]) <= 3600


def test_400_is_permanent_and_not_retried(conn: psycopg.Connection, caplog: pytest.LogCaptureFixture) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "bad"})
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "invalid"})

    with caplog.at_level("WARNING", logger="sancturai-relay"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            assert process_batch(conn, http, "http://bot/notify", "tok") == 1
            conn.execute("UPDATE outbox SET next_attempt_at = now()")
            assert process_batch(conn, http, "http://bot/notify", "tok") == 0
    assert calls == 1
    row = conn.execute("SELECT attempts, sent_at FROM outbox").fetchone()
    assert row == (MAX_ATTEMPTS, None)  # abandonnée : la purge la retirera
    assert any("abandon" in r.getMessage() for r in caplog.records)
    assert "bad" not in caplog.text  # le texte n'est pas journalisé


def test_401_and_503_are_retried(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE outbox")
    _insert(conn, "log", {"text": "a"})
    for status in (401, 503):
        conn.execute("UPDATE outbox SET next_attempt_at = now()")
        with httpx.Client(transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s))) as http:
            process_batch(conn, http, "http://bot/notify", "tok")
    assert conn.execute("SELECT attempts, sent_at FROM outbox").fetchone() == (2, None)
