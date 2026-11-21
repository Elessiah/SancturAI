from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.models import OutboxEvent
from app.outbox.service import enqueue


async def test_enqueue_is_part_of_transaction(db: AsyncSession) -> None:
    await enqueue(db, "log", "hello", agent={"handle": "x"})
    await db.commit()
    rows = (await db.scalars(select(OutboxEvent))).all()
    assert len(rows) == 1
    assert rows[0].kind == "log" and rows[0].payload == {"text": "hello", "agent": {"handle": "x"}}
    assert rows[0].sent_at is None and rows[0].attempts == 0


async def test_enqueue_truncates(db: AsyncSession) -> None:
    await enqueue(db, "log", "x" * 5000)
    await db.commit()
    row = (await db.scalars(select(OutboxEvent))).one()
    assert len(row.payload["text"]) == 1800
