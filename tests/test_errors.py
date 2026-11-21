import httpx

from app.errors import HINTS, STATUS, ApiError

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
