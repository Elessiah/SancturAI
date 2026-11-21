class ErrorAggregator:
    """Compte les erreurs 5xx ; le flush périodique vers l'outbox est ajouté en Task 6."""

    def __init__(self) -> None:
        self._count = 0

    def record(self) -> None:
        self._count += 1

    def take(self) -> int:
        count, self._count = self._count, 0
        return count
