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
