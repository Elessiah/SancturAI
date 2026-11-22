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
        self._attempts = 0  # calculs terminés
        self._error: Exception | None = None  # échec du dernier calcul, partagé avec les requêtes qui l'attendaient
        self._lock = asyncio.Lock()

    async def get(self, compute: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        seen = self._attempts
        async with self._lock:  # requêtes simultanées à l'expiration : un seul calcul
            if self._value is not None and self._clock() - self._at < self._ttl:
                return self._value
            if self._attempts != seen and self._error is not None:
                raise self._error  # base en panne : pas une nouvelle requête count par requête en attente
            try:
                self._value = await compute()
            except Exception as exc:
                self._error = exc
                raise
            finally:
                self._attempts += 1  # à la fin du calcul : les requêtes arrivées pendant celui-ci en voient l'issue
            self._error = None
            self._at = self._clock()
            return self._value

    def clear(self) -> None:
        self._value = None
        self._at = float("-inf")
        self._error = None
