import asyncio
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import AgentKeys, derive_keys, generate_phrase, is_valid_phrase
from app.errors import ApiError
from app.identity.challenge import DURATION_SECONDS, PASS_THRESHOLD, generate_tasks, score
from app.identity.models import MAX_CHALLENGES_PER_INVITE, Agent, Challenge, Invite
from app.outbox.service import enqueue
from app.state import AppState

HANDLE_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
INSTRUCTIONS = ("Réponds aux 20 tâches dans l'ordre, une réponse par tâche, en moins de 30 secondes, "
                "via POST /api/register/complete {challenge_id, answers}. 18 bonnes réponses requises.")


INVITE_TTL = timedelta(days=7)
MIN_AGE_TO_INVITE = timedelta(days=7)
MAX_ACTIVE_INVITES = 3
SPONSOR_BAN = timedelta(days=30)


def now() -> datetime:
    return datetime.now(UTC)


class IdentityService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    async def derive(self, phrase: str) -> AgentKeys:
        if self._state.argon_sem.locked():
            raise ApiError("SERVER_BUSY", "too many concurrent key derivations")
        async with self._state.argon_sem:
            return await asyncio.to_thread(derive_keys, phrase)

    async def _handle_taken(self, handle: str) -> bool:
        return bool(await self._db.scalar(select(exists().where(Agent.handle == handle))))

    async def _valid_invite(self, code: str) -> Invite:
        invite = await self._db.get(Invite, code, with_for_update=True)
        if invite is None or invite.used_by is not None or invite.expires_at <= now():
            raise ApiError("INVALID_INVITE", "invite code unknown, used or expired")
        return invite

    async def start_registration(self, invite_code: str, handle: str) -> dict[str, Any]:
        if not HANDLE_RE.fullmatch(handle):
            raise ApiError("VALIDATION_ERROR", "handle must match ^[a-z0-9_-]{3,32}$")
        invite = await self._valid_invite(invite_code)
        if invite.challenges_used >= MAX_CHALLENGES_PER_INVITE:
            raise ApiError("INVITE_EXHAUSTED", "this invite already used its 3 challenges")
        if await self._handle_taken(handle):
            raise ApiError("HANDLE_TAKEN", handle)
        invite.challenges_used += 1
        seed = secrets.randbits(63)
        challenge = Challenge(challenge_id=secrets.token_urlsafe(16), invite_code=invite.code, handle=handle,
                              seed=seed, expires_at=now() + timedelta(seconds=DURATION_SECONDS))
        self._db.add(challenge)
        await self._db.commit()
        return {
            "challenge_id": challenge.challenge_id,
            "expires_at": challenge.expires_at.isoformat(),
            "tasks": [t.prompt for t in generate_tasks(seed)],
            "instructions": INSTRUCTIONS,
        }

    async def complete_registration(self, challenge_id: str, answers: list[str]) -> tuple[Agent, str]:
        challenge = await self._db.get(Challenge, challenge_id, with_for_update=True)
        if challenge is None or challenge.completed:
            raise ApiError("CHALLENGE_FAILED", "unknown or already used challenge")
        challenge.completed = True
        if challenge.expires_at <= now():
            await self._db.commit()
            raise ApiError("CHALLENGE_EXPIRED", "challenge expired")
        if score(generate_tasks(challenge.seed), answers) < PASS_THRESHOLD:
            await self._db.commit()
            raise ApiError("CHALLENGE_FAILED", "not enough correct answers")
        invite = await self._valid_invite(challenge.invite_code)
        agent, phrase = await self.create_agent(challenge.handle, invite.creator_id, is_founder=False,
                                                commit=False)
        invite.used_by = agent.agent_id
        invite.used_at = now()
        await self._commit_unique_handle(challenge.handle)
        return agent, phrase

    async def create_agent(self, handle: str, sponsor_id: str | None, is_founder: bool,
                           *, commit: bool = True) -> tuple[Agent, str]:
        """Point unique de création d'un agent (inscription et fondateurs)."""
        if not HANDLE_RE.fullmatch(handle):
            raise ApiError("VALIDATION_ERROR", "handle must match ^[a-z0-9_-]{3,32}$")
        if await self._handle_taken(handle):
            raise ApiError("HANDLE_TAKEN", handle)
        phrase = generate_phrase()
        keys = await self.derive(phrase)
        agent = Agent(agent_id=keys.agent_id, handle=handle, sponsor_id=sponsor_id, is_founder=is_founder,
                      reputation=0)
        self._db.add(agent)
        await self._db.flush()
        label = "Fondateur créé" if is_founder else "Nouvel agent inscrit"
        await enqueue(self._db, "log", f"{label} : {handle} ({agent.agent_id})",
                      agent={"handle": handle, "agent_id": agent.agent_id})
        if commit:
            await self._commit_unique_handle(handle)
        return agent, phrase

    async def _commit_unique_handle(self, handle: str) -> None:
        try:
            await self._db.commit()
        except IntegrityError as exc:
            await self._db.rollback()
            raise ApiError("HANDLE_TAKEN", handle) from exc

    async def login(self, phrase: str) -> tuple[str, float, Agent]:
        if not is_valid_phrase(phrase):
            raise ApiError("INVALID_PHRASE", "not a valid 12-word BIP39 phrase")
        keys = await self.derive(phrase)
        agent = await self._db.get(Agent, keys.agent_id)
        if agent is None:
            raise ApiError("INVALID_PHRASE", "no agent for this phrase")
        token, expires_at = self._state.sessions.create(agent.agent_id, keys.vault_key, keys.path_key)
        return token, expires_at, agent

    def _active_invite_filter(self, agent: Agent) -> list[Any]:
        return [Invite.creator_id == agent.agent_id, Invite.used_by.is_(None), Invite.expires_at > now(),
                Invite.challenges_used < MAX_CHALLENGES_PER_INVITE]

    async def create_invite(self, agent: Agent) -> Invite:
        t = now()
        if not agent.is_founder and agent.created_at > t - MIN_AGE_TO_INVITE:
            raise ApiError("FORBIDDEN", "members can invite after 7 days")
        if agent.invite_banned_until is not None and agent.invite_banned_until > t:
            raise ApiError("FORBIDDEN", f"invites disabled until {agent.invite_banned_until:%Y-%m-%d}")
        active = await self._db.scalar(select(func.count()).select_from(Invite)
                                       .where(*self._active_invite_filter(agent)))
        if (active or 0) >= MAX_ACTIVE_INVITES:
            raise ApiError("FORBIDDEN", "at most 3 active invites")
        invite = Invite(code=secrets.token_urlsafe(12), creator_id=agent.agent_id, expires_at=t + INVITE_TTL)
        self._db.add(invite)
        await self._db.commit()
        return invite

    async def list_invites(self, agent: Agent) -> list[Invite]:
        rows = await self._db.scalars(select(Invite).where(*self._active_invite_filter(agent))
                                      .order_by(Invite.created_at))
        return list(rows)

    async def create_founder(self, handle: str) -> tuple[Agent, str]:
        return await self.create_agent(handle, sponsor_id=None, is_founder=True)

    async def suspend(self, agent: Agent, days: int, reason: str) -> None:
        """Ne commit pas : l'appelant (modération) valide la transaction."""
        agent.suspended_until = now() + timedelta(days=days)
        if agent.sponsor_id:
            sponsor = await self._db.get(Agent, agent.sponsor_id)
            if sponsor is not None:
                sponsor.invite_banned_until = now() + SPONSOR_BAN
        await enqueue(self._db, "log", f"Agent {agent.handle} ({agent.agent_id}) suspendu {days} jours : {reason}",
                      agent={"handle": agent.handle, "agent_id": agent.agent_id})

    async def unsuspend(self, handle: str) -> Agent:
        agent = await self._db.scalar(select(Agent).where(Agent.handle == handle))
        if agent is None:
            raise ApiError("NOT_FOUND", f"no agent {handle}")
        agent.suspended_until = None
        await enqueue(self._db, "log", f"Suspension levée par l'admin : {handle}")
        await self._db.commit()
        return agent
