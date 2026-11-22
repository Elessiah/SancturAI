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


def test_zero_limit_rejects_without_crash() -> None:
    rl = RateLimiter()
    with pytest.raises(ApiError) as exc:
        rl.hit("k", limit=0, window_seconds=60)
    assert exc.value.retry_after == 1


def test_gc_evicts_short_window_buckets() -> None:
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    for i in range(999):
        rl.hit(f"k{i}", limit=1, window_seconds=10)
    now[0] = 11
    rl.hit("last", limit=1, window_seconds=10)  # 1000e appel -> GC
    assert list(rl._hits) == [("last", 10)]


def test_key_cap_fails_closed_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ratelimit as rlmod

    monkeypatch.setattr(rlmod, "MAX_KEYS", 3)
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    for i in range(3):
        rl.hit(f"k{i}", limit=5, window_seconds=10)
    with pytest.raises(ApiError) as exc:
        rl.hit("new", limit=5, window_seconds=10)
    assert exc.value.retry_after == 1 and len(rl._hits) == 3
    rl.hit("k0", limit=5, window_seconds=10)  # clé connue : toujours servie
    now[0] = 20
    rl.hit("new", limit=5, window_seconds=10)  # GC libère les seaux périmés
    assert list(rl._hits) == [("new", 10)]


def test_key_cap_is_per_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ratelimit as rlmod

    monkeypatch.setattr(rlmod, "MAX_KEYS", 2)
    rl = RateLimiter(clock=lambda: 0.0)
    rl.hit("register:ip1", limit=5, window_seconds=3600)
    rl.hit("register:ip2", limit=5, window_seconds=3600)
    with pytest.raises(ApiError):
        rl.hit("register:ip3", limit=5, window_seconds=3600)
    rl.hit("agent:a1", limit=60, window_seconds=60)  # flot d'IP sans effet sur les agents
    rl.hit("agent:a2", limit=60, window_seconds=60)
    assert rl._counts == {"register": 2, "agent": 2}
    rl.clear()
    assert not rl._counts


def test_same_key_two_windows_do_not_share_hits() -> None:
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    rl.hit("k", limit=1, window_seconds=3600)
    now[0] = 20
    rl.hit("k", limit=5, window_seconds=10)  # fenêtre courte : seau distinct
    with pytest.raises(ApiError):
        rl.hit("k", limit=1, window_seconds=3600)  # le hit long n'a pas été purgé
