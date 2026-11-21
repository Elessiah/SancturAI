import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.crypto import derive_keys, generate_phrase
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


async def make_agent(state: AppState, handle: str, *, founder: bool = False,
                     sponsor_id: str | None = None, age_days: int = 0) -> AgentCtx:
    phrase = generate_phrase()
    keys = derive_keys(phrase)
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
