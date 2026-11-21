from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.models import OutboxEvent

MAX_TEXT = {"log": 1800, "agent_message": 4000}  # le bot découpe en DM de 2000 caractères
KINDS = set(MAX_TEXT)


async def enqueue(db: AsyncSession, kind: str, text: str, **extra: object) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown outbox kind {kind}")
    db.add(OutboxEvent(kind=kind, payload={"text": text[:MAX_TEXT[kind]], **extra}))
