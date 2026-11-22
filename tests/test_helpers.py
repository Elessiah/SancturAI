from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Agent
from app.state import AppState
from tests.helpers import make_agent


async def test_make_agent_after_a_deletion_gets_a_fresh_identity(state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "first")
    await db.execute(delete(Agent).where(Agent.agent_id == a.agent_id))
    await db.commit()
    b = await make_agent(state, "second")
    c = await make_agent(state, "third")
    assert len({a.agent_id, b.agent_id, c.agent_id}) == 3
