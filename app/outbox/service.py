import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.models import OutboxEvent

MAX_TEXT = {"log": 1800, "agent_message": 4000}  # le bot découpe en DM de 2000 caractères
KINDS = set(MAX_TEXT)
EXTRA_KEYS = {"agent", "short_id"}  # contrat du bot : rien d'autre ne doit sortir vers Discord
# Contrôles C0/DEL sauf \t et \n : \x00 est refusé par JSONB (la transaction métier échouerait).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Un str Python ne contient que des surrogates isolés (JSON "\ud800") : non encodables en UTF-8.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _clean(text: str) -> str:
    return _SURROGATE.sub("\N{REPLACEMENT CHARACTER}", _CONTROL.sub("", text))


def _clean_value(value: object) -> object:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {_clean(str(k)): _clean_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean_value(v) for v in value]
    return value


async def enqueue(db: AsyncSession, kind: str, text: str, **extra: object) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown outbox kind {kind}")
    if unknown := set(extra) - EXTRA_KEYS:
        raise ValueError(f"unexpected outbox payload keys {sorted(unknown)}")
    payload = {"text": _clean(text[:MAX_TEXT[kind]]), **{k: _clean_value(v) for k, v in extra.items()}}
    db.add(OutboxEvent(kind=kind, payload=payload))
