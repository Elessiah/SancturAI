import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.crypto import AgentKeys, derive_keys, generate_phrase
from app.identity.models import Agent, Invite
from app.state import AppState


@dataclass
class AgentCtx:
    agent_id: str
    handle: str
    phrase: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


_KEY_POOL: list[tuple[str, AgentKeys]] = []  # (phrase, clés) dérivées une fois pour toute la session de tests
_cursor = 0  # prochaine identité du pool pour le test en cours (remis à 0 par la fixture _clean)


def reset_key_pool_cursor() -> None:
    global _cursor
    _cursor = 0


async def _pooled_keys(index: int) -> tuple[str, AgentKeys]:
    """Argon2 est coûteux (64 Mio, ~0,2 s) : on réutilise les identités d'un test à l'autre (base tronquée)."""
    while len(_KEY_POOL) <= index:
        phrase = generate_phrase()
        _KEY_POOL.append((phrase, await asyncio.to_thread(derive_keys, phrase)))
    return _KEY_POOL[index]


async def make_agent(state: AppState, handle: str, *, founder: bool = False,
                     sponsor_id: str | None = None, age_days: int = 30) -> AgentCtx:
    """age_days=30 par défaut : hors probation (spec visibilité §3.3) ; age_days=0 pour une recrue."""
    global _cursor
    # compteur monotone par test : distinct même si un test supprime des agents entre deux appels
    phrase, keys = await _pooled_keys(_cursor)
    _cursor += 1
    async with state.sessionmaker() as db:
        db.add(Agent(agent_id=keys.agent_id, handle=handle, is_founder=founder, sponsor_id=sponsor_id,
                     created_at=datetime.now(UTC) - timedelta(days=age_days)))
        await db.commit()
    token, _ = state.sessions.create(keys.agent_id, keys.vault_key, keys.path_key)
    return AgentCtx(keys.agent_id, handle, phrase, token)


async def make_invite(state: AppState, creator_id: str) -> str:
    code = secrets.token_urlsafe(12)
    async with state.sessionmaker() as db:
        db.add(Invite(code=code, creator_id=creator_id, expires_at=datetime.now(UTC) + timedelta(days=7)))
        await db.commit()
    return code
