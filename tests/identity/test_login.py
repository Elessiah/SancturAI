import httpx

from app.state import AppState
from tests.helpers import make_agent


async def test_login_me_logout(client: httpx.AsyncClient, state: AppState) -> None:
    ctx = await make_agent(state, "alice")
    r = await client.post("/api/login", json={"phrase": ctx.phrase})
    assert r.status_code == 200, r.text
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
