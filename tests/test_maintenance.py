import asyncio
from collections import namedtuple

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.disk import DiskGuard
from app.maintenance import maintenance_loop, run_maintenance_once
from app.outbox.models import OutboxEvent
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


async def test_prunes_old_read_notifications(state: AppState, db: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from app.forum.models import Notification
    from tests.helpers import make_agent

    a = await make_agent(state, "pruned")
    old, new = datetime.now(UTC) - timedelta(days=31), datetime.now(UTC) - timedelta(days=29)
    db.add_all([Notification(agent_id=a.agent_id, kind="moderation", detail="old read", read=True, created_at=old),
                Notification(agent_id=a.agent_id, kind="moderation", detail="old unread", read=False, created_at=old),
                Notification(agent_id=a.agent_id, kind="moderation", detail="new read", read=True, created_at=new)])
    await db.commit()
    await run_maintenance_once(state)
    details = {n.detail for n in (await db.scalars(select(Notification))).all()}
    assert details == {"old unread", "new read"}


async def test_prunes_unread_notifications_after_90_days(state: AppState, db: AsyncSession) -> None:
    """M5 (revue batch 2) : les non lues d'un agent inactif ne s'accumulent pas sans borne."""
    from datetime import UTC, datetime, timedelta

    from app.forum.models import Notification
    from tests.helpers import make_agent

    a = await make_agent(state, "inactive")
    now = datetime.now(UTC)
    db.add_all([Notification(agent_id=a.agent_id, kind="moderation", detail="unread 91", created_at=now - timedelta(days=91)),
                Notification(agent_id=a.agent_id, kind="moderation", detail="unread 89", created_at=now - timedelta(days=89)),
                Notification(agent_id=a.agent_id, kind="moderation", detail="read 31", read=True,
                             created_at=now - timedelta(days=31))])
    await db.commit()
    await run_maintenance_once(state)
    details = {n.detail for n in (await db.scalars(select(Notification))).all()}
    assert details == {"unread 89"}


async def test_prune_failure_is_counted(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    """M6 (partiel) : une purge en échec persistant se voit dans le compteur d'erreurs (log Discord)."""
    async def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("prune down")

    monkeypatch.setattr("app.maintenance.prune_read_notifications", boom)
    await run_maintenance_once(state)
    assert state.errors.take() == 1
