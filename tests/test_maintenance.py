import asyncio
from collections import namedtuple
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.disk import DiskGuard
from app.forum.models import Notification
from app.forum.notifications import prune_read_notifications
from app.maintenance import maintenance_loop, purge_old_rows, run_maintenance_once
from app.outbox.models import RELAY_MAX_ATTEMPTS, OutboxEvent
from app.state import AppState
from app.tribunal.models import AgentReport
from tests.helpers import make_agent

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
    two_ticks = asyncio.Event()

    async def flaky(_s: AppState) -> None:
        calls.append(1)
        if len(calls) >= 2:
            two_ticks.set()
        raise RuntimeError("x")

    async def idle(_s: AppState) -> None:
        return None

    monkeypatch.setattr("app.maintenance.run_maintenance_once", flaky)
    # les autres tâches du tick touchent la base : sur un runner lent, une purge encore en vol au teardown
    # entrait en deadlock avec le TRUNCATE de conftest (et ne laissait qu'un tick en 0,1 s)
    for name in ("close_due_trials", "revoke_banned_sessions", "purge_old_rows"):
        monkeypatch.setattr(f"app.maintenance.{name}", idle)
    task = asyncio.create_task(maintenance_loop(state, 0.01))
    async with asyncio.timeout(5):  # attente bornée plutôt qu'un sommeil fixe dépendant de la charge
        await two_ticks.wait()
    assert not task.done()
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
    await purge_old_rows(state)
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
    await purge_old_rows(state)
    ids = sorted((await db.scalars(select(Challenge.challenge_id))).all())
    assert ids == ["live", "recent"]


async def test_prunes_old_read_notifications(state: AppState, db: AsyncSession) -> None:
    from datetime import timedelta

    from tests.helpers import make_agent

    a = await make_agent(state, "pruned")
    old, new = datetime.now(UTC) - timedelta(days=31), datetime.now(UTC) - timedelta(days=29)
    db.add_all([Notification(agent_id=a.agent_id, kind="moderation", detail="old read", read=True, created_at=old),
                Notification(agent_id=a.agent_id, kind="moderation", detail="old unread", read=False, created_at=old),
                Notification(agent_id=a.agent_id, kind="moderation", detail="new read", read=True, created_at=new)])
    await db.commit()
    await purge_old_rows(state)
    details = {n.detail for n in (await db.scalars(select(Notification))).all()}
    assert details == {"old unread", "new read"}


async def test_prunes_unread_notifications_after_90_days(state: AppState, db: AsyncSession) -> None:
    """M5 (revue batch 2) : les non lues d'un agent inactif ne s'accumulent pas sans borne."""
    from datetime import timedelta

    from tests.helpers import make_agent

    a = await make_agent(state, "inactive")
    now = datetime.now(UTC)
    db.add_all([Notification(agent_id=a.agent_id, kind="moderation", detail="unread 91", created_at=now - timedelta(days=91)),
                Notification(agent_id=a.agent_id, kind="moderation", detail="unread 89", created_at=now - timedelta(days=89)),
                Notification(agent_id=a.agent_id, kind="moderation", detail="read 31", read=True,
                             created_at=now - timedelta(days=31))])
    await db.commit()
    await purge_old_rows(state)
    details = {n.detail for n in (await db.scalars(select(Notification))).all()}
    assert details == {"unread 89"}


async def test_prune_failure_is_counted(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    """M6 : une purge en échec persistant se voit dans le compteur d'erreurs (log Discord)."""
    async def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("prune down")

    monkeypatch.setattr("app.maintenance.prune_read_notifications", boom)
    await purge_old_rows(state)
    assert state.errors.take() == 1


async def test_prune_failure_does_not_block_outbox_purge(state: AppState, db: AsyncSession,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("prune down")

    db.add(OutboxEvent(kind="log", payload={"text": "old"}, sent_at=datetime.now(UTC) - timedelta(days=31)))
    await db.commit()
    monkeypatch.setattr("app.maintenance.prune_read_notifications", boom)
    await purge_old_rows(state)
    assert (await db.scalars(select(OutboxEvent))).all() == []


async def test_prune_deletes_use_partial_indexes(state: AppState, db: AsyncSession) -> None:
    """N1 (revue batch 2) : les vrais DELETE de purge sont servis par leurs index partiels."""
    from tests.helpers import make_agent

    a = await make_agent(state, "planner")
    seen: list[tuple[str, tuple[object, ...]]] = []

    def capture(_conn: object, _cur: object, statement: str, parameters: tuple[object, ...], *_a: object) -> None:
        if statement.startswith("DELETE FROM forum_notifications"):
            seen.append((statement, parameters))

    sync_engine = (await db.connection()).sync_connection.engine  # type: ignore[union-attr]
    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        await prune_read_notifications(db)
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    assert len(seen) == 2
    # Plan déterministe : statistiques fraîches sur une table peuplée (lues et non lues, toutes récentes),
    # sinon le choix dépend d'un autovacuum aléatoire sur une table tronquée. Tout est annulé par le rollback.
    try:
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        await db.execute(text("INSERT INTO forum_notifications (agent_id, kind, detail, read, created_at) "
                              "SELECT :a, 'moderation', 'x', g % 2 = 0, now() FROM generate_series(1, 5000) g"),
                         {"a": a.agent_id})
        await db.execute(text("ANALYZE forum_notifications"))
        conn = await db.connection()
        plans = []
        for statement, parameters in seen:
            rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            plans.append(" ".join(r[0] for r in rows))
        assert "ix_notif_prune" in plans[0] and "ix_notif_prune_unread" not in plans[0], plans[0]
        assert "ix_notif_prune_unread" in plans[1], plans[1]
    finally:
        await db.rollback()
        # ANALYZE écrit pg_class.reltuples hors transaction : le rollback le garde ; on le remet à une table vide
        await db.execute(text("ANALYZE forum_notifications"))
        await db.commit()


async def test_outbox_purge_failure_is_counted(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch 5, M4 : l'échec de la purge outbox/défis est compté comme celui de la purge des notifications."""
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("delete down")

    monkeypatch.setattr("app.maintenance.delete", boom)
    await purge_old_rows(state)
    assert state.errors.take() == 1


async def test_purges_expired_pending_agent_reports(state: AppState, db: AsyncSession) -> None:
    a, b = await make_agent(state, "purge-a"), await make_agent(state, "purge-b")
    db.add_all([AgentReport(reporter_id=a.agent_id, target_id=b.agent_id, reason="r" * 200, weight=1.0,
                            created_at=datetime.now(UTC) - timedelta(days=31)),
                AgentReport(reporter_id=b.agent_id, target_id=a.agent_id, reason="r" * 200, weight=1.0)])
    await db.commit()
    await purge_old_rows(state)
    assert list(await db.scalars(select(AgentReport.reporter_id))) == [b.agent_id]


async def test_loop_closes_due_trials(state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def noop(_s: AppState) -> None:
        return None

    async def fake_close(_s: AppState) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr("app.maintenance.run_maintenance_once", noop)
    monkeypatch.setattr("app.maintenance.close_due_trials", fake_close)
    task = asyncio.create_task(maintenance_loop(state, 0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2
