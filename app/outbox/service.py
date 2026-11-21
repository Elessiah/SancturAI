import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.models import OutboxEvent

MAX_TEXT = {"log": 1800, "agent_message": 4000}  # le bot découpe en DM de 2000 caractères
KINDS = set(MAX_TEXT)
# Contrôles C0/DEL sauf \t et \n : \x00 est refusé par JSONB (la transaction métier échouerait).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Un str Python ne contient que des surrogates isolés (JSON "\ud800") : non encodables en UTF-8.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _clean(text: str) -> str:
    return _SURROGATE.sub("\N{REPLACEMENT CHARACTER}", _CONTROL.sub("", text))


async def enqueue(db: AsyncSession, kind: str, text: str, **extra: object) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown outbox kind {kind}")
    db.add(OutboxEvent(kind=kind, payload={"text": _clean(text[:MAX_TEXT[kind]]), **extra}))
