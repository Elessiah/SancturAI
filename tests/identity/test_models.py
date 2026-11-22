from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Agent, Challenge, Invite


async def _founder(db: AsyncSession) -> Agent:
    agent = Agent(agent_id="A" * 44, handle="founder", is_founder=True)
    db.add(agent)
    await db.commit()
    return agent


@pytest.mark.parametrize("handle", ["ab", "Admin", "bad handle", "été", "x\n"])
async def test_agent_handle_format_enforced_by_db(db: AsyncSession, handle: str) -> None:
    db.add(Agent(agent_id="B" * 44, handle=handle))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_challenge_without_invite_is_allowed(db: AsyncSession) -> None:
    expires = datetime.now(UTC) + timedelta(seconds=30)
    db.add(Challenge(challenge_id="k" * 32, handle="stranger", seed=1, expires_at=expires))
    await db.commit()  # inscription ouverte : invite_code nullable


async def test_challenge_handle_format_enforced_by_db(db: AsyncSession) -> None:
    founder = await _founder(db)
    expires = datetime.now(UTC) + timedelta(days=7)
    db.add(Invite(code="c" * 32, creator_id=founder.agent_id, expires_at=expires))
    await db.commit()
    db.add(Challenge(challenge_id="k" * 32, invite_code="c" * 32, handle="NOPE!", seed=1, expires_at=expires))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_deleting_invite_consumer_is_rejected(db: AsyncSession) -> None:
    founder = await _founder(db)
    invitee = Agent(agent_id="B" * 44, handle="invitee", sponsor_id=founder.agent_id)
    db.add(invitee)
    await db.commit()
    expires = datetime.now(UTC) + timedelta(days=7)
    db.add(Invite(code="c" * 32, creator_id=founder.agent_id, expires_at=expires,
                  used_by=invitee.agent_id, used_at=datetime.now(UTC)))
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM agents WHERE agent_id = :a"), {"a": "B" * 44})
    await db.rollback()
