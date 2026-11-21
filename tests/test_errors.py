import httpx
from fastapi import FastAPI, HTTPException

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
