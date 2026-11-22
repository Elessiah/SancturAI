from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.identity.models import Agent
from app.identity.sessions import Session
from app.state import AppState

AGENT_RATE = (60, 60.0)
REGISTER_RATE = (5, 3600.0)
# Un register/start donne un défi, donc un complete : 10/h couvre 5 défis et une relance réseau chacun.
REGISTER_COMPLETE_RATE = (10, 3600.0)


def limit_register(state: AppState, ip: str) -> None:
    state.limiter.hit(f"register:{ip}", *REGISTER_RATE)


def limit_register_complete(state: AppState, ip: str) -> None:
    state.limiter.hit(f"register-complete:{ip}", *REGISTER_COMPLETE_RATE)


def limit_login(state: AppState, ip: str) -> None:
    state.limiter.hit(f"login:{ip}", 10, 60)


def resolve_session(state: AppState, token: str | None, *, count: bool = True) -> Session:
    """count=False (logout) : ne consomme pas le budget agent, pour qu'un agent limité efface ses clés."""
    if not token:
        raise ApiError("UNAUTHENTICATED", "missing session token")
    session = state.sessions.get(token)
    if session is None:
        raise ApiError("SESSION_EXPIRED", "unknown or expired session")
    if count:
        state.limiter.hit(f"agent:{session.agent_id}", *AGENT_RATE)
    return session


async def load_agent(db: AsyncSession, state: AppState, session: Session) -> Agent:
    agent = await db.get(Agent, session.agent_id)
    if agent is None:
        state.sessions.revoke_agent(session.agent_id)
        raise ApiError("UNAUTHENTICATED", "agent no longer exists")
    return agent


def is_suspended(agent: Agent) -> bool:
    return agent.suspended_until is not None and agent.suspended_until > datetime.now(UTC)


def ensure_writable(state: AppState, agent: Agent) -> None:
    state.disk.ensure_writable()
    if is_suspended(agent):
        raise ApiError("SUSPENDED", f"suspended until {agent.suspended_until:%Y-%m-%d %H:%M} UTC")
