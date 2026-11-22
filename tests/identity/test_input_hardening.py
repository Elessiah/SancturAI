import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.errors import HINTS, ApiError, install_error_handlers
from app.identity.auth import REGISTER_COMPLETE_RATE
from app.identity.challenge import DURATION_SECONDS, PASS_THRESHOLD
from app.identity.constants import HANDLE_PATTERN, MAX_CHALLENGES_PER_INVITE
from app.identity.models import HANDLE_CHECK
from app.identity.service import HANDLE_RE, INSTRUCTIONS, IdentityService
from app.state import AppState
from tests.helpers import make_agent


@pytest.mark.parametrize(("path", "body"), [
    ("/api/register/start", {"invite_code": "a\u0000b", "handle": "nulbob"}),
    ("/api/register/complete", {"challenge_id": "a\u0000b", "answers": []}),
    ("/api/register/start", {"invite_code": "a b", "handle": "nulbob"}),
    ("/api/register/complete", {"challenge_id": "x" * 33, "answers": []}),
])
async def test_token_fields_reject_bad_charset(client: httpx.AsyncClient, state: AppState, path: str,
                                                body: dict[str, object]) -> None:
    r = await client.post(path, json=body)
    assert (r.status_code, r.json()["error"]) == (422, "VALIDATION_ERROR")
    assert "\x00" not in r.text
    assert state.errors.take() == 0  # aucune alerte 5xx


async def test_service_rejects_bad_tokens_for_every_transport(state: AppState) -> None:
    async with state.sessionmaker() as db:
        svc = IdentityService(db, state)
        for call in (svc.start_registration("a\x00b", "bob"), svc.complete_registration("a\x00b", [])):
            with pytest.raises(ApiError) as exc:
                await call
            assert exc.value.code == "VALIDATION_ERROR"


async def test_register_complete_is_ip_rate_limited(client: httpx.AsyncClient) -> None:
    limit, _ = REGISTER_COMPLETE_RATE
    for _ in range(limit):
        r = await client.post("/api/register/complete", json={"challenge_id": "unknown", "answers": []})
        assert r.json()["error"] == "CHALLENGE_FAILED"
    r = await client.post("/api/register/complete", json={"challenge_id": "unknown", "answers": []})
    assert r.json()["error"] == "RATE_LIMITED"


async def test_db_data_errors_are_validation_errors(state: AppState) -> None:
    count = 0

    def record() -> None:
        nonlocal count
        count += 1

    app = FastAPI()
    install_error_handlers(app, record)

    @app.get("/nul")
    async def nul() -> None:
        async with state.sessionmaker() as db:
            await db.execute(text("SELECT CAST(:v AS text)"), {"v": "a\x00b"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/nul")
    assert (r.status_code, r.json()["error"]) == (422, "VALIDATION_ERROR")
    assert count == 0


async def test_logout_is_not_rate_limited(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "limited")
    for _ in range(60):
        await client.get("/api/me", headers=a.headers)
    assert (await client.get("/api/me", headers=a.headers)).json()["error"] == "RATE_LIMITED"
    r = await client.post("/api/logout", headers=a.headers)
    assert r.status_code == 200, r.text
    assert state.sessions.get(a.token) is None


async def test_derive_busy_gives_retry_after_and_registration_waits(state: AppState) -> None:
    phrase = (await make_agent(state, "deriver")).phrase
    n = state.settings.argon2_concurrency
    for _ in range(n):
        await state.argon_sem.acquire()
    try:
        async with state.sessionmaker() as db:
            svc = IdentityService(db, state)
            with pytest.raises(ApiError) as exc:
                await svc.derive(phrase)
            assert exc.value.code == "SERVER_BUSY" and exc.value.retry_after == 1
            with pytest.raises(ApiError) as exc:
                await svc.derive(phrase, wait=0.05)
            assert exc.value.retry_after == 1
            waiting = asyncio.create_task(svc.derive(phrase, wait=5))
            await asyncio.sleep(0.05)
            state.argon_sem.release()
            n -= 1
            assert (await waiting).agent_id
    finally:
        for _ in range(n):
            state.argon_sem.release()


def test_constants_have_a_single_source() -> None:
    assert HANDLE_RE.pattern == HANDLE_PATTERN and HANDLE_PATTERN in HANDLE_CHECK
    assert HANDLE_PATTERN in HINTS["HANDLE_TAKEN"]
    assert f"{MAX_CHALLENGES_PER_INVITE} défis" in HINTS["INVITE_EXHAUSTED"]
    assert f"{PASS_THRESHOLD} bonnes" in HINTS["CHALLENGE_FAILED"] and f"{PASS_THRESHOLD} bonnes" in INSTRUCTIONS
    assert f"{DURATION_SECONDS} s" in HINTS["CHALLENGE_EXPIRED"] and f"{DURATION_SECONDS} secondes" in INSTRUCTIONS


async def test_login_phrase_bound_is_the_crypto_bound(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/login", json={"phrase": "x" * 500})
    assert r.json()["error"] == "INVALID_PHRASE"  # pas un VALIDATION_ERROR à 300 caractères
