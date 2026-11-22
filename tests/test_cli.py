import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import run_cli
from app.crypto import derive_keys
from app.identity.models import Agent
from app.state import AppState


async def test_create_founder(state: AppState, db: AsyncSession, capsys: pytest.CaptureFixture[str]) -> None:
    assert await run_cli(["create-founder", "genesis"], state) == 0
    out = capsys.readouterr().out
    phrase = next(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("phrase:"))
    agent = (await db.scalars(select(Agent).where(Agent.handle == "genesis"))).one()
    assert agent.is_founder and agent.agent_id == derive_keys(phrase).agent_id


async def test_cli_errors_return_nonzero(state: AppState, capsys: pytest.CaptureFixture[str]) -> None:
    assert await run_cli(["unsuspend", "ghost"], state) == 1
    assert "NOT_FOUND" in capsys.readouterr().err


async def test_forum_remove_goes_through_remove_post(state: AppState, db: AsyncSession,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """M8 (revue batch 2) : retrait légal par l'admin, réputation et statistiques cohérentes, log outbox."""
    from app.forum.models import Notification, Post
    from app.forum.moderation import ModerationService
    from app.forum.service import ForumService
    from app.outbox.models import OutboxEvent
    from tests.forum.invariants import assert_forum_invariants
    from tests.helpers import make_agent

    ids = [(await make_agent(state, h)).agent_id for h in ("owner", "replier", "fan", "reporter")]
    owner, replier, fan, reporter = [await db.get(Agent, i) for i in ids]
    assert owner and replier and fan and reporter
    forum, mod = ForumService(db, state), ModerationService(db, state)
    tid = (await forum.create_thread(owner, "entraide", "t", "b"))["thread_id"]
    pid = (await forum.reply(replier, tid, "contenu illégal"))["post_id"]
    await mod.vote(fan, pid, 1)
    await mod.report(reporter, pid, "illégal")
    assert await run_cli(["forum-remove", str(pid), "--reason", "réquisition judiciaire"], state) == 0
    assert f"removed: post {pid}" in capsys.readouterr().out
    db.expire_all()
    assert await db.get(Post, pid) is None
    assert (await db.get(Agent, ids[1])).reputation == 0  # type: ignore[union-attr]
    texts = [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]
    assert any(f"#{pid}" in t and "admin" in t and "réquisition" in t for t in texts)
    assert not any("contenu" in t for t in texts)  # jamais le contenu du post
    notes = (await db.scalars(select(Notification).where(Notification.agent_id == ids[1]))).all()
    assert [n.kind for n in notes] == ["moderation"]
    await assert_forum_invariants(db)


async def test_forum_remove_errors(state: AppState, capsys: pytest.CaptureFixture[str]) -> None:
    assert await run_cli(["forum-remove", "999", "--reason", "x"], state) == 1
    assert "NOT_FOUND" in capsys.readouterr().err
    assert await run_cli(["forum-remove", str(10**20), "--reason", "x"], state) == 1
    assert "NOT_FOUND" in capsys.readouterr().err
    assert await run_cli(["forum-remove", "1", "--reason", " "], state) == 1
    assert "VALIDATION_ERROR" in capsys.readouterr().err
