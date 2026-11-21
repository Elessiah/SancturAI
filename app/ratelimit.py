import math
import time
from collections import deque
from collections.abc import Callable

from app.errors import ApiError

# Plafond de clés suivies PAR ESPACE DE NOMS (préfixe avant le premier ':', ex.
# "register", "agent") : au-delà (clés tournantes, ex. IPv6), refus fail-closed de
# ce seul espace, sans croissance mémoire non bornée et sans qu'un flot de clés IP
# ne bloque les clés des autres espaces (agents authentifiés).
MAX_KEYS = 100_000


def _namespace(key: str) -> str:
    head, sep, _ = key.partition(":")
    return head if sep else ""  # clés sans préfixe : espace commun


class RateLimiter:
    """Fenêtre glissante en mémoire (worker unique)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # (clé, fenêtre) -> horodatages des hits : une même clé utilisée avec deux
        # fenêtres a deux seaux distincts (la courte ne purge pas la longue).
        self._hits: dict[tuple[str, float], deque[float]] = {}
        self._counts: dict[str, int] = {}  # espace de noms -> nombre de seaux suivis
        self._calls = 0
        self._full_gc_at = -math.inf

    def hit(self, key: str, limit: int, window_seconds: float) -> None:
        now = self._clock()
        slot = (key, window_seconds)
        bucket = self._hits.get(slot)
        if bucket is None:
            ns = _namespace(key)
            if self._counts.get(ns, 0) >= MAX_KEYS:
                if now - self._full_gc_at >= 1:  # GC O(n) au plus 1 fois/s quand plein
                    self._full_gc_at = now
                    self._gc(now)
                if self._counts.get(ns, 0) >= MAX_KEYS:
                    raise ApiError("RATE_LIMITED", "too many tracked clients", retry_after=1)
            self._counts[ns] = self._counts.get(ns, 0) + 1
            bucket = self._hits[slot] = deque()
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
        stale = [s for s, b in self._hits.items() if not b or b[-1] <= now - s[1]]
        for slot in stale:
            del self._hits[slot]
            ns = _namespace(slot[0])
            self._counts[ns] -= 1
            if not self._counts[ns]:
                del self._counts[ns]

    def clear(self) -> None:
        self._hits.clear()
        self._counts.clear()
