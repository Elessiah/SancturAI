from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.models import OutboxEvent
from app.text import strip_control

MAX_TEXT = {"log": 1800, "agent_message": 4000}  # le bot découpe en DM de 2000 caractères
KINDS = set(MAX_TEXT)  # = OUTBOX_KINDS (CHECK ck_outbox_kind), vérifié par tests/test_outbox.py
EXTRA_KEYS = {"agent", "short_id"}  # contrat du bot : rien d'autre ne doit sortir vers Discord


def _clean(text: str) -> str:
    # \x00 est refusé par JSONB (la transaction métier échouerait) ; surrogates isolés non encodables
    return strip_control(text)


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
