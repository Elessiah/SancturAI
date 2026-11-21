import shutil
import time
from collections.abc import Callable
from typing import Any

from app.errors import ApiError

_CACHE_SECONDS = 10.0
_ALERT_EVERY = 3600.0


class DiskGuard:
    def __init__(self, path: str, ratio: float, clock: Callable[[], float] = time.monotonic,
                 usage: Callable[[str], Any] = shutil.disk_usage) -> None:
        self._path, self._ratio, self._clock, self._usage = path, ratio, clock, usage
        self._checked_at = float("-inf")
        self._busy = False
        self._alerted_at = float("-inf")

    def is_busy(self) -> bool:
        now = self._clock()
        if now - self._checked_at >= _CACHE_SECONDS:
            u = self._usage(self._path)
            self._busy = u.used / u.total > self._ratio
            self._checked_at = now
        return self._busy

    def ensure_writable(self) -> None:
        if self.is_busy():
            raise ApiError("SERVER_BUSY", "data volume above 85% usage")

    def should_alert(self) -> bool:
        now = self._clock()
        if self.is_busy() and now - self._alerted_at >= _ALERT_EVERY:
            self._alerted_at = now
            return True
        return False
