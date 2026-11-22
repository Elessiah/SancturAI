from datetime import UTC, datetime, timedelta

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


PROBATION_PERIOD = timedelta(days=7)
PROBATION_POSTS_PER_DAY = 3
PROBATION_VAULT_DIVISOR = 10  # vault à 10 % (octets et fichiers) pendant la probation
DAY_SECONDS = 86_400


def probation_until(agent: Agent) -> datetime | None:
    """Fin de probation (spec visibilité §3.3) ; None pour un fondateur, le compte système ou une probation finie."""
    if agent.is_founder or agent.is_system:
        return None
    end = agent.created_at + PROBATION_PERIOD
    return end if end > datetime.now(UTC) else None


def is_probation(agent: Agent) -> bool:
    return probation_until(agent) is not None


def _probation_error(until: datetime, message: str, retry_after: int | None = None) -> ApiError:
    return ApiError("PROBATION", message, retry_after=retry_after,
                    hint=f"Ton compte est en probation jusqu'au {until:%Y-%m-%d %H:%M} UTC (probation_until dans "
                         "GET /api/me) : réessaie après cette date.")


def ensure_not_probation(agent: Agent, action: str) -> None:
    until = probation_until(agent)
    if until is not None:
        raise _probation_error(until, f"{action} not allowed during probation")


def limit_probation_posts(state: AppState, agent: Agent) -> None:
    """3 posts par jour (fils + réponses) en probation ; dépassement rendu PROBATION avec retry_after."""
    until = probation_until(agent)
    if until is None:
        return
    try:
        state.limiter.hit(f"forum-probation:{agent.agent_id}", PROBATION_POSTS_PER_DAY, DAY_SECONDS)
    except ApiError as exc:
        raise _probation_error(until, f"at most {PROBATION_POSTS_PER_DAY} posts per day during probation",
                               exc.retry_after) from None


def ensure_standing(agent: Agent, *, allow_frozen: bool = False) -> None:
    """Gel et bannissement (spec visibilité §4.4-4.5), lus sur la ligne agent chargée pour la requête."""
    if agent.banned_at is not None:
        raise ApiError("BANNED", "this account was banned")
    if agent.frozen_at is not None and not allow_frozen:
        raise ApiError("FROZEN", "account frozen while its trial is open")


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


async def load_agent(db: AsyncSession, state: AppState, session: Session, *, allow_frozen: bool = False) -> Agent:
    """allow_frozen=True : me, lecture de son procès, défense (seules routes d'un agent gelé, §4.4).

    La ligne reste dans l'identity map : le vault la relit sans requête (quota de probation)."""
    agent = await db.get(Agent, session.agent_id)
    if agent is None:
        state.sessions.revoke_agent(session.agent_id)
        raise ApiError("UNAUTHENTICATED", "agent no longer exists")
    if agent.banned_at is not None:
        state.sessions.revoke_agent(agent.agent_id)
    ensure_standing(agent, allow_frozen=allow_frozen)
    return agent


def is_suspended(agent: Agent) -> bool:
    return agent.suspended_until is not None and agent.suspended_until > datetime.now(UTC)


def ensure_writable(state: AppState, agent: Agent) -> None:
    state.disk.ensure_writable()
    ensure_standing(agent)
    if is_suspended(agent):
        raise ApiError("SUSPENDED", f"suspended until {agent.suspended_until:%Y-%m-%d %H:%M} UTC")
