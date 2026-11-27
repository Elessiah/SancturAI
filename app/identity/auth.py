from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
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


LOGINS_PER_AGENT = 10  # par minute, toutes IP confondues


def limit_login(state: AppState, ip: str) -> None:
    state.limiter.hit(f"login:{ip}", 10, 60)


def limit_login_agent(state: AppState, agent_id: str) -> None:
    # par agent, appelée APRÈS la preuve (phrase dérivée ou signature valide) : un tiers ne peut pas l'épuiser.
    # Borne les journaux « Connexion » (outbox, 30 jours) qu'un compte ferait écrire depuis des milliers d'IP.
    state.limiter.hit(f"login-agent:{agent_id}", LOGINS_PER_AGENT, 60)


def limit_login_challenge(state: AppState, ip: str) -> None:
    # compteur distinct de login: (spec connexion signée §4) : une connexion signée coûte un hit de chaque
    state.limiter.hit(f"login-challenge:{ip}", 10, 60)


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


# Espace (clé à deux entiers, disjoint de la clé bigint des inscriptions) du verrou consultatif de statut d'un agent.
STANDING_LOCK_SPACE = 0x5A4E_C702


async def lock_standing(db: AsyncSession, agent_id: str, *, exclusive: bool = False) -> None:
    """Verrou consultatif de transaction sur le statut d'un agent (revue secu Task 6, S1).

    Partagé : pris par `load_agent` en tête de chaque requête authentifiée, tenu jusqu'à son commit. Exclusif :
    pris par le bannissement avant tout autre verrou ; il attend la fin des requêtes en vol de l'agent (leurs
    écritures sont donc purgées) et fait attendre les suivantes, qui relisent alors `banned_at`. Un verrou
    consultatif ne bloque aucun verrou de ligne : l'ordre global des verrous du forum est inchangé.
    """
    lock = func.pg_advisory_xact_lock if exclusive else func.pg_advisory_xact_lock_shared
    await db.execute(select(lock(STANDING_LOCK_SPACE, func.hashtext(agent_id))))


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
    await lock_standing(db, session.agent_id)  # avant la lecture : un bannissement en cours est attendu, puis lu
    agent = await db.get(Agent, session.agent_id, populate_existing=True)
    if agent is None:
        state.sessions.revoke_agent(session.agent_id)
        raise ApiError("UNAUTHENTICATED", "agent no longer exists")
    if agent.banned_at is not None:
        state.sessions.revoke_agent(agent.agent_id)
    ensure_standing(agent, allow_frozen=allow_frozen)
    return agent


def ensure_writable(state: AppState, agent: Agent) -> None:
    state.disk.ensure_writable()
    ensure_standing(agent)
