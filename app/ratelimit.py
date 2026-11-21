import math
import time
from collections import deque
from collections.abc import Callable

from app.errors import ApiError

# Plafond de clés suivies : au-delà (clés tournantes, ex. IPv6), refus fail-closed
# plutôt qu'une croissance mémoire non bornée sur l'hôte partagé.
MAX_KEYS = 100_000


class RateLimiter:
    """Fenêtre glissante en mémoire (worker unique)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # clé -> (horodatages des hits, fenêtre utilisée) ; la fenêtre sert au GC.
        self._hits: dict[str, tuple[deque[float], float]] = {}
        self._calls = 0
        self._full_gc_at = -math.inf

    def hit(self, key: str, limit: int, window_seconds: float) -> None:
        now = self._clock()
        entry = self._hits.get(key)
        if entry is None and len(self._hits) >= MAX_KEYS:
            if now - self._full_gc_at >= 1:  # GC O(n) au plus 1 fois/s quand plein
                self._full_gc_at = now
                self._gc(now)
            if len(self._hits) >= MAX_KEYS:
                raise ApiError("RATE_LIMITED", "too many tracked clients", retry_after=1)
        bucket: deque[float] = entry[0] if entry else deque()
        if entry is None or entry[1] != window_seconds:
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
