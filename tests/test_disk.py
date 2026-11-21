from collections import namedtuple

import pytest

from app.disk import DiskGuard
from app.errors import ApiError

Usage = namedtuple("Usage", "total used free")


def make(used: int, now: list[float]) -> DiskGuard:
    return DiskGuard("/x", 0.85, clock=lambda: now[0], usage=lambda _p: Usage(100, used, 100 - used))


def test_busy_above_threshold() -> None:
    now = [0.0]
    assert not make(84, now).is_busy()
    guard = make(86, now)
    assert guard.is_busy()
    with pytest.raises(ApiError) as exc:
        guard.ensure_writable()
    assert exc.value.code == "SERVER_BUSY"


def test_alert_once_per_hour() -> None:
    now = [0.0]
    guard = make(90, now)
    assert guard.should_alert()
    assert not guard.should_alert()
    now[0] += 3601
    assert guard.should_alert()


def test_cache_ten_seconds() -> None:
    now = [0.0]
    calls = []

    def usage(_p: str) -> Usage:
        calls.append(1)
        return Usage(100, 10, 90)

    guard = DiskGuard("/x", 0.85, clock=lambda: now[0], usage=usage)
    guard.is_busy()
    guard.is_busy()
    assert len(calls) == 1
    now[0] += 11
    guard.is_busy()
    assert len(calls) == 2


def _gone(_p: str) -> Usage:
    raise OSError("gone")


@pytest.mark.parametrize("usage", [_gone, lambda _p: Usage(0, 0, 0)])
def test_probe_failure_fails_closed(usage: object) -> None:
    guard = DiskGuard("/x", 0.85, clock=lambda: 0.0, usage=usage)  # type: ignore[arg-type]
    with pytest.raises(ApiError) as exc:
        guard.ensure_writable()
    assert exc.value.code == "SERVER_BUSY"
    assert guard.should_alert()
