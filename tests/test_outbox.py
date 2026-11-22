import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.outbox.models import OUTBOX_KINDS, OutboxEvent
from app.outbox.service import KINDS, enqueue


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


async def test_enqueue_strips_chars_jsonb_rejects(db: AsyncSession) -> None:
    await enqueue(db, "agent_message", "a\x00b\ud800c\x07d\ne\tf")
    await db.commit()
    row = (await db.scalars(select(OutboxEvent))).one()
    assert row.payload["text"] == "ab�cd\ne\tf"



async def test_enqueue_cleans_extra_values(db: AsyncSession) -> None:
    await enqueue(db, "agent_message", "hi", short_id="ab\x0012", agent={"handle": "x\ud800", "agent_id": "y"})
    await db.commit()
    row = (await db.scalars(select(OutboxEvent))).one()
    assert row.payload == {"text": "hi", "short_id": "ab12", "agent": {"handle": "x\N{REPLACEMENT CHARACTER}",
                                                                        "agent_id": "y"}}


async def test_enqueue_rejects_unknown_extra_keys(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await enqueue(db, "log", "hi", vault_key="secret")


def test_outbox_kinds_match_db_check() -> None:
    assert set(OUTBOX_KINDS) == KINDS
