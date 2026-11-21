class ErrorAggregator:
    """Compte les erreurs 5xx ; le flush périodique vers l'outbox est dans app.maintenance."""

    def __init__(self) -> None:
        self._count = 0

    def record(self) -> None:
        self._count += 1

    def take(self) -> int:
        count, self._count = self._count, 0
        return count

    def restore(self, count: int) -> None:
        self._count += count
