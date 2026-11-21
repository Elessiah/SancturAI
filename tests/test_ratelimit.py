import pytest

from app.errors import ApiError
from app.ratelimit import RateLimiter


def test_limit_then_retry_after() -> None:
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    for _ in range(3):
        rl.hit("k", limit=3, window_seconds=60)
    with pytest.raises(ApiError) as exc:
        rl.hit("k", limit=3, window_seconds=60)
    assert exc.value.code == "RATE_LIMITED"
    assert exc.value.retry_after == 60
    now[0] = 61
    rl.hit("k", limit=3, window_seconds=60)


def test_keys_are_independent() -> None:
    rl = RateLimiter()
    rl.hit("a", limit=1, window_seconds=60)
    rl.hit("b", limit=1, window_seconds=60)
