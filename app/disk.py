import logging
import shutil
import time
from collections.abc import Callable
from typing import Any

from app.errors import ApiError

log = logging.getLogger("sancturai")
_CACHE_SECONDS = 10.0
_ALERT_EVERY = 3600.0


class DiskGuard:
    def __init__(self, path: str, ratio: float, clock: Callable[[], float] = time.monotonic,
                 usage: Callable[[str], Any] = shutil.disk_usage) -> None:
        self._path, self._ratio, self._clock, self._usage = path, ratio, clock, usage
        self._checked_at = float("-inf")
        self._busy = False
        self._alerted_at = float("-inf")

    @property
    def ratio(self) -> float:
        return self._ratio

    def is_busy(self) -> bool:
        now = self._clock()
        if now - self._checked_at >= _CACHE_SECONDS:
            try:
                u = self._usage(self._path)
                self._busy = u.used / u.total > self._ratio
            except Exception:  # sonde illisible : fail-closed (SERVER_BUSY + alerte), pas de 500
                log.exception("disk usage probe failed")
                self._busy = True
            self._checked_at = now
        return self._busy

    def ensure_writable(self) -> None:
        if self.is_busy():
            raise ApiError("SERVER_BUSY", f"data volume above {self._ratio:.0%} usage")

    def should_alert(self) -> bool:
        now = self._clock()
        if self.is_busy() and now - self._alerted_at >= _ALERT_EVERY:
            self._alerted_at = now
            return True
        return False

    def rearm_alert(self) -> None:
        self._alerted_at = float("-inf")
