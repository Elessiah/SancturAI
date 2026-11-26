import httpx
import pytest
from sqlalchemy import select

from app.outbox.models import OutboxEvent
from app.state import AppState
from tests.helpers import make_agent


async def test_login_me_logout(client: httpx.AsyncClient, state: AppState) -> None:
    ctx = await make_agent(state, "alice")
    r = await client.post("/api/login", json={"phrase": ctx.phrase})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    token = r.json()["session_token"]
    assert r.json()["agent_id"] == ctx.agent_id
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["handle"] == "alice"
    await client.post("/api/logout", headers={"Authorization": f"Bearer {token}"})
    me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["error"] == "SESSION_EXPIRED"


async def test_login_unknown_or_invalid_phrase(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/login", json={"phrase": "not valid"})
    assert r.json()["error"] == "INVALID_PHRASE"
    unknown = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    r = await client.post("/api/login", json={"phrase": unknown})
    assert r.json()["error"] == "INVALID_PHRASE"


async def test_me_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/me")
    assert r.status_code == 401 and r.json()["error"] == "UNAUTHENTICATED"


async def test_argon_busy(client: httpx.AsyncClient, state: AppState) -> None:
    ctx = await make_agent(state, "busy")
    for _ in range(state.settings.argon2_concurrency):
        await state.argon_sem.acquire()
    try:
        r = await client.post("/api/login", json={"phrase": ctx.phrase})
        assert r.json()["error"] == "SERVER_BUSY"
    finally:
        for _ in range(state.settings.argon2_concurrency):
            state.argon_sem.release()


async def _login_logs(state: AppState) -> list[str]:
    async with state.sessionmaker() as db:
        events = await db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "log").order_by(OutboxEvent.id))
        return [e.payload["text"] for e in events if e.payload["text"].startswith("Connexion : ")]


@pytest.mark.parametrize(("real_ip", "expected"), [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2::42", "2001:db8:1:2::42"),  # adresse exacte, pas le /64 des limites
    ("::ffff:198.51.100.9", "198.51.100.9"),
    ("<@everyone> 1.2.3.4", "inconnue"),  # rien d'arbitraire ne part vers Discord
])
async def test_login_logs_source_ip(client: httpx.AsyncClient, state: AppState, real_ip: str,
                                    expected: str) -> None:
    ctx = await make_agent(state, "logged")
    r = await client.post("/api/login", json={"phrase": ctx.phrase}, headers={"X-Real-IP": real_ip})
    assert r.status_code == 200, r.text
    assert await _login_logs(state) == [f"Connexion : logged ({ctx.agent_id}) depuis {expected}, via REST"]


async def test_failed_login_logs_nothing(client: httpx.AsyncClient, state: AppState) -> None:
    unknown = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    await client.post("/api/login", json={"phrase": unknown}, headers={"X-Real-IP": "203.0.113.7"})
    assert await _login_logs(state) == []
