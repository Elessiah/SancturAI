"""Cache en mémoire de /status.json (spec visibilité §5.3) : au plus un calcul (une requête count) par minute."""
import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

STATUS_TTL_SECONDS = 60.0


class StatusCache:
    def __init__(self, ttl: float = STATUS_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._value: dict[str, Any] | None = None
        self._at = float("-inf")
        self._lock = asyncio.Lock()

    async def get(self, compute: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        async with self._lock:  # requêtes simultanées à l'expiration : un seul calcul
            if self._value is None or self._clock() - self._at >= self._ttl:
                self._value = await compute()
                self._at = self._clock()
            return self._value

    def clear(self) -> None:
        self._value = None
        self._at = float("-inf")
