import asyncio
from collections import namedtuple

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.disk import DiskGuard
from app.maintenance import maintenance_loop, run_maintenance_once
from app.outbox.models import RELAY_MAX_ATTEMPTS, OutboxEvent
from app.state import AppState

Usage = namedtuple("Usage", "total used free")


async def test_flushes_errors_and_disk_alert(state: AppState, db: AsyncSession) -> None:
    state.errors.record()
    state.errors.record()
    original = state.disk
    state.disk = DiskGuard("/x", 0.85, usage=lambda _p: Usage(100, 95, 5))
    try:
        await run_maintenance_once(state)
    finally:
        state.disk = original
    texts = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any("2 erreur" in t for t in texts)
    assert any("disque" in t.lower() for t in texts)


async def test_nothing_to_report(state: AppState, db: AsyncSession) -> None:
    await run_maintenance_once(state)
    assert (await db.scalars(select(OutboxEvent))).all() == []


async def test_failure_keeps_errors_and_alert(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr("app.maintenance.enqueue", boom)
    monkeypatch.setattr(state, "disk", DiskGuard("/x", 0.85, usage=lambda _p: Usage(100, 95, 5)))
    state.errors.record()
    with pytest.raises(RuntimeError):
        await run_maintenance_once(state)
    assert state.errors.take() == 1
    assert state.disk.should_alert()


async def test_loop_survives_and_cancels_cleanly(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def flaky(_s: AppState) -> None:
        calls.append(1)
        raise RuntimeError("x")

    monkeypatch.setattr("app.maintenance.run_maintenance_once", flaky)
    task = asyncio.create_task(maintenance_loop(state, 0.01))
    await asyncio.sleep(0.1)
    assert len(calls) >= 2 and not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


async def test_disk_probe_failure_alerts_and_flushes(state: AppState, db: AsyncSession,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    def bad(_p: str) -> Usage:
        raise OSError("gone")

    monkeypatch.setattr(state, "disk", DiskGuard("/x", 0.85, usage=bad))
    state.errors.record()
    await run_maintenance_once(state)  # sonde en échec : ne bloque plus le flush des erreurs
    texts = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any("1 erreur" in t for t in texts)
    assert any("disque" in t.lower() for t in texts)


async def test_purges_old_sent_events(state: AppState, db: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(days=31)
    db.add(OutboxEvent(kind="log", payload={"text": "old"}, sent_at=old))
    db.add(OutboxEvent(kind="log", payload={"text": "pending"}))
    db.add(OutboxEvent(kind="log", payload={"text": "abandoned-old"}, attempts=RELAY_MAX_ATTEMPTS, created_at=old))
    db.add(OutboxEvent(kind="log", payload={"text": "abandoned-recent"}, attempts=RELAY_MAX_ATTEMPTS))
    db.add(OutboxEvent(kind="log", payload={"text": "retrying-old"}, attempts=3, created_at=old))
    await db.commit()
    await run_maintenance_once(state)
    texts = sorted(e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all())
    assert texts == ["abandoned-recent", "pending", "retrying-old"]


async def test_purges_expired_challenges(state: AppState, db: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from app.identity.models import Challenge, Invite
    from tests.helpers import make_agent

    founder = await make_agent(state, "founder", founder=True)
    now = datetime.now(UTC)
    db.add(Invite(code="c" * 32, creator_id=founder.agent_id, expires_at=now + timedelta(days=7)))
    await db.flush()
    for cid, delta in (("old", timedelta(days=2)), ("recent", timedelta(hours=2)), ("live", -timedelta(hours=1))):
        db.add(Challenge(challenge_id=cid, invite_code="c" * 32, handle="newbie", seed=1, expires_at=now - delta))
    await db.commit()
    await run_maintenance_once(state)
    ids = sorted((await db.scalars(select(Challenge.challenge_id))).all())
    assert ids == ["live", "recent"]
