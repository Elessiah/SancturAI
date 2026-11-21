import math
import time
from collections import deque
from collections.abc import Callable

from app.errors import ApiError


class RateLimiter:
    """Fenêtre glissante en mémoire (worker unique)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # clé -> (horodatages des hits, fenêtre utilisée) ; la fenêtre sert au GC.
        self._hits: dict[str, tuple[deque[float], float]] = {}
        self._calls = 0

    def hit(self, key: str, limit: int, window_seconds: float) -> None:
        now = self._clock()
        entry = self._hits.get(key)
        if entry is None:
            bucket: deque[float] = deque()
            self._hits[key] = (bucket, window_seconds)
        else:
            bucket = entry[0]
            self._hits[key] = (bucket, window_seconds)
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry = max(1, math.ceil(bucket[0] + window_seconds - now)) if bucket else 1
            raise ApiError("RATE_LIMITED", f"limit {limit} per {int(window_seconds)}s", retry_after=retry)
        bucket.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:
            self._gc(now)

    def _gc(self, now: float) -> None:
        stale = [k for k, (b, w) in self._hits.items() if not b or b[-1] <= now - w]
        for key in stale:
            del self._hits[key]

    def clear(self) -> None:
        self._hits.clear()
