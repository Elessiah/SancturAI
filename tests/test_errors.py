import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.db import make_engine
from app.errors import HINTS, STATUS, ApiError, install_error_handlers

SPEC_CODES = {"UNAUTHENTICATED", "SESSION_EXPIRED", "INVALID_PHRASE", "INVALID_INVITE",
              "INVITE_EXHAUSTED", "HANDLE_TAKEN", "CHALLENGE_FAILED", "CHALLENGE_EXPIRED",
              "NOT_FOUND", "FORBIDDEN", "SUSPENDED", "QUOTA_EXCEEDED", "RATE_LIMITED",
              "SERVER_BUSY", "VALIDATION_ERROR", "INTERNAL",
              # spec visibilité §8
              "REGISTRATIONS_CLOSED", "PROBATION", "FROZEN", "BANNED", "TRIAL_COOLDOWN", "TRIAL_CLOSED",
              "DEFENSE_EXISTS"}


def test_every_code_has_hint_and_status() -> None:
    assert set(HINTS) == set(STATUS) == SPEC_CODES


def test_visibility_codes_status() -> None:
    assert {c: STATUS[c] for c in ("REGISTRATIONS_CLOSED", "PROBATION", "FROZEN", "BANNED", "TRIAL_COOLDOWN",
                                   "TRIAL_CLOSED", "DEFENSE_EXISTS")} == {
        "REGISTRATIONS_CLOSED": 503, "PROBATION": 403, "FROZEN": 403, "BANNED": 403, "TRIAL_COOLDOWN": 409,
        "TRIAL_CLOSED": 409, "DEFENSE_EXISTS": 409}
    assert "/status.json" in HINTS["REGISTRATIONS_CLOSED"]


def test_api_error_dict() -> None:
    err = ApiError("RATE_LIMITED", "slow down", retry_after=12)
    assert err.status == 429
    assert err.to_dict() == {
        "error": "RATE_LIMITED", "message": "slow down",
        "hint": HINTS["RATE_LIMITED"], "retry_after": 12,
    }


async def test_unknown_route_uses_error_format(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"] == "NOT_FOUND"
    assert "hint" in r.json()


async def test_unmapped_4xx_is_client_error_not_internal() -> None:
    app = FastAPI()
    install_error_handlers(app, lambda: None)

    @app.get("/bad")
    async def bad() -> None:
        raise HTTPException(400, "malformed")

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(502, "upstream")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/bad")
        assert (r.status_code, r.json()["error"]) == (422, "VALIDATION_ERROR")
        r = await c.get("/boom")
        assert (r.status_code, r.json()["error"]) == (500, "INTERNAL")


async def test_unexpected_exception_is_contained(caplog: pytest.LogCaptureFixture) -> None:
    count = 0

    def record() -> None:
        nonlocal count
        count += 1

    app = FastAPI()
    install_error_handlers(app, record)

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret-bound-param")

    # raise_app_exceptions=True (défaut) : le test échouerait si l'exception remontait au serveur
    transport = httpx.ASGITransport(app=app)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/crash")
    assert (r.status_code, r.json()["error"]) == (500, "INTERNAL")
    assert count == 1
    assert "RuntimeError" in caplog.text
    assert "secret-bound-param" not in caplog.text
    assert all(rec.exc_info is None for rec in caplog.records)


def test_engine_hides_bound_parameters() -> None:
    assert make_engine("postgresql+asyncpg://u:p@h/db").sync_engine.hide_parameters is True


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("deadlock detected: secret-bound-param")
        self.sqlstate = sqlstate


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
async def test_lock_conflicts_are_retryable_not_internal(sqlstate: str, caplog: pytest.LogCaptureFixture) -> None:
    """M1 (revue batch 2) : interblocage / échec de sérialisation → SERVER_BUSY réessayable, jamais compté 5xx."""
    from sqlalchemy.exc import DBAPIError

    from app.errors import is_retryable

    count = 0

    def record() -> None:
        nonlocal count
        count += 1

    app = FastAPI()
    install_error_handlers(app, record)
    exc = DBAPIError("SELECT 1", {}, _PgError(sqlstate))
    assert is_retryable(exc)
    assert not is_retryable(DBAPIError("SELECT 1", {}, _PgError("23505")))

    @app.get("/conflict")
    async def conflict() -> None:
        raise exc

    transport = httpx.ASGITransport(app=app)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/conflict")
    assert (r.status_code, r.json()["error"], r.json()["retry_after"]) == (503, "SERVER_BUSY", 1)
    assert r.headers["retry-after"] == "1"
    assert count == 0
    assert sqlstate in caplog.text and "secret-bound-param" not in caplog.text
