import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.db import make_engine
from app.errors import HINTS, STATUS, ApiError, install_error_handlers

SPEC_CODES = {"UNAUTHENTICATED", "SESSION_EXPIRED", "INVALID_PHRASE", "INVALID_INVITE",
              "INVITE_EXHAUSTED", "HANDLE_TAKEN", "CHALLENGE_FAILED", "CHALLENGE_EXPIRED",
              "NOT_FOUND", "FORBIDDEN", "SUSPENDED", "QUOTA_EXCEEDED", "RATE_LIMITED",
              "SERVER_BUSY", "VALIDATION_ERROR", "INTERNAL"}


def test_every_code_has_hint_and_status() -> None:
    assert set(HINTS) == set(STATUS) == SPEC_CODES


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
