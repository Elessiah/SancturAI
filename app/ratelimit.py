import math
import time
from collections import deque
from collections.abc import Callable

from app.errors import ApiError

_MAX_WINDOW = 86_400


class RateLimiter:
    """Fenêtre glissante en mémoire (worker unique). Fenêtre max : 1 jour."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def hit(self, key: str, limit: int, window_seconds: float) -> None:
        now = self._clock()
        bucket = self._hits.setdefault(key, deque())
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry = max(1, math.ceil(bucket[0] + window_seconds - now))
            raise ApiError("RATE_LIMITED", f"limit {limit} per {int(window_seconds)}s", retry_after=retry)
        bucket.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:
            self._gc(now)

    def _gc(self, now: float) -> None:
        for key in [k for k, b in self._hits.items() if not b or b[-1] <= now - _MAX_WINDOW]:
            del self._hits[key]

    def clear(self) -> None:
        self._hits.clear()
