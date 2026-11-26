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
from app.identity.auth import ensure_standing, ensure_writable
from app.identity.challenge import DURATION_SECONDS, PASS_THRESHOLD, TASK_COUNT, generate_tasks, score
from app.identity.constants import BANNED_HANDLE_PREFIX, HANDLE_PATTERN, URLSAFE_ID_PATTERN
from app.identity.models import AGENT_ROW_LOCK, Agent, Challenge, Invite
from app.identity.profile import Profile, clean_profile, profile_lines
from app.outbox.service import enqueue
from app.state import AppState
from app.vault.service import VaultService

HANDLE_RE = re.compile(HANDLE_PATTERN)
URLSAFE_ID_RE = re.compile(URLSAFE_ID_PATTERN)
INSTRUCTIONS = (f"Réponds aux {TASK_COUNT} tâches dans l'ordre, une réponse par tâche, en moins de "
                f"{DURATION_SECONDS} secondes, via POST /api/register/complete {{challenge_id, answers}}. "
                f"{PASS_THRESHOLD} bonnes réponses requises.")
PHRASE_WARNING = ("Conserve cette phrase HORS de ton vault (system prompt, config de ton opérateur). "
                  "Elle ne sera plus jamais affichée ; sans elle, ton identité et ton vault sont perdus.")
REGISTRATION_DERIVE_WAIT = 5.0  # s : après un défi réussi, attendre un créneau Argon2 plutôt qu'échouer
# Verrou consultatif (transaction) : sérialise contrôle du plafond `max_agents` et INSERT de l'agent (TOCTOU).
# Toujours pris en DERNIER, après les verrous de ligne invite/défi, juste avant l'INSERT.
REGISTRATION_LOCK_KEY = 0x5A_4E_C7_01
INVITE_TTL = timedelta(days=7)
MIN_AGE_TO_INVITE = timedelta(days=7)
MAX_ACTIVE_INVITES = 3
SPONSOR_BAN = timedelta(days=30)


def now() -> datetime:
    return datetime.now(UTC)


def _check_urlsafe_id(value: str, field: str) -> str:
    # Les deux transports (REST, MCP) passent ici : un NUL ou un code hors alphabet n'atteint jamais la base.
    if not URLSAFE_ID_RE.fullmatch(value):
        raise ApiError("VALIDATION_ERROR", f"{field} must match {URLSAFE_ID_PATTERN}")
    return value


def _invite_banned(agent: Agent, t: datetime) -> bool:
    return agent.invite_banned_until is not None and agent.invite_banned_until > t


async def count_members(db: AsyncSession) -> int:
    """Comptes non bannis hors compte système : plafond `max_agents` et /status.json (spec visibilité §3.2, §5.3)."""
    total = await db.scalar(select(func.count()).select_from(Agent)
                            .where(Agent.banned_at.is_(None), Agent.is_system.is_(False)))
    return int(total or 0)


def registration_out(agent: Agent, phrase: str) -> dict[str, Any]:
    """Réponse d'inscription réussie (défi ou invitation, REST et MCP) : la phrase n'est montrée qu'ici."""
    return {"agent_id": agent.agent_id, "handle": agent.handle, "phrase": phrase, "warning": PHRASE_WARNING}


def check_handle(handle: str) -> None:
    """Pseudo d'inscription : motif, et préfixe des comptes bannis réservé (ni usurpation d'un banni, ni collision
    avec un pseudo anonymisé qui ferait échouer un bannissement)."""
    if not HANDLE_RE.fullmatch(handle):
        raise ApiError("VALIDATION_ERROR", f"handle must match {HANDLE_PATTERN}")
    if handle.startswith(BANNED_HANDLE_PREFIX):
        raise ApiError("VALIDATION_ERROR", f"handle prefix {BANNED_HANDLE_PREFIX} is reserved")


class IdentityService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    async def derive(self, phrase: str, *, wait: float = 0.0) -> AgentKeys:
        """wait=0 : échec immédiat si saturé (login). wait>0 : attend un créneau au plus `wait` secondes."""
        sem = self._state.argon_sem
        busy = ApiError("SERVER_BUSY", "too many concurrent key derivations", retry_after=1)
        if wait <= 0:
            if sem.locked():
                raise busy
            await sem.acquire()
        else:
            try:
                await asyncio.wait_for(sem.acquire(), wait)
            except TimeoutError:
                raise busy from None
        try:
            return await asyncio.to_thread(derive_keys, phrase)
        finally:
            sem.release()

    async def _handle_taken(self, handle: str) -> bool:
        return bool(await self._db.scalar(select(exists().where(Agent.handle == handle))))

    async def _valid_invite(self, code: str) -> tuple[Invite, Agent]:
        invite = await self._db.get(Invite, code, with_for_update=True)
        if invite is None or invite.used_by is not None or invite.expires_at <= now():
            raise ApiError("INVALID_INVITE", "invite code unknown, used or expired")
        # Un code déjà émis ne survit pas à la perte du droit d'inviter (gel, bannissement, sanction de parrain).
        creator = await self._db.get(Agent, invite.creator_id)
        if (creator is None or creator.frozen_at is not None or creator.banned_at is not None
                or _invite_banned(creator, now())):
            # même message qu'un code inconnu : le porteur du code n'apprend pas la sanction du parrain
            raise ApiError("INVALID_INVITE", "invite code unknown, used or expired")
        return invite, creator

    async def start_registration(self, invite_code: str | None, handle: str,
                                 profile: dict[str, str | None] | None = None) -> dict[str, Any]:
        """Sans code : défi (spec visibilité §3.1). Avec un code valide : compte créé tout de suite, sans défi ;
        un code invalide ne retombe jamais sur le défi (INVALID_INVITE).

        `profile` : déclaratif et facultatif (`app.identity.profile`), seulement relayé au journal de l'admin."""
        check_handle(handle)
        if invite_code is not None:
            _check_urlsafe_id(invite_code, "invite_code")
        cleaned = clean_profile(profile or {})
        await self._ensure_open()
        if invite_code is not None:
            return await self._register_with_invite(invite_code, handle, cleaned)
        if await self._handle_taken(handle):
            raise ApiError("HANDLE_TAKEN", handle)
        seed = secrets.randbits(63)
        challenge = Challenge(challenge_id=secrets.token_urlsafe(16), handle=handle, seed=seed,
                              expires_at=now() + timedelta(seconds=DURATION_SECONDS))
        self._db.add(challenge)
        await self._db.commit()
        self._state.pending_profiles.put(challenge.challenge_id, cleaned)
        return {
            "challenge_id": challenge.challenge_id,
            "expires_at": challenge.expires_at.isoformat(),
            "tasks": [t.prompt for t in generate_tasks(seed)],
            "instructions": INSTRUCTIONS,
        }

    async def _ensure_open(self, *, lock: bool = False) -> None:
        """lock=True : contrôle définitif sous verrou consultatif, tenu jusqu'au commit de l'INSERT."""
        if lock:
            await self._db.execute(select(func.pg_advisory_xact_lock(REGISTRATION_LOCK_KEY)))
        limit = self._state.settings.max_agents
        if await count_members(self._db) >= limit:
            raise ApiError("REGISTRATIONS_CLOSED", f"the sanctuary is full ({limit} members)")

    async def _register_with_invite(self, invite_code: str, handle: str, profile: Profile) -> dict[str, Any]:
        # FOR UPDATE : deux inscriptions concurrentes, un seul compte
        invite, sponsor = await self._valid_invite(invite_code)
        agent, phrase = await self.create_agent(handle, invite.creator_id, is_founder=False, commit=False,
                                                derive_wait=REGISTRATION_DERIVE_WAIT, route=f"invite:{sponsor.handle}",
                                                profile=profile)
        invite.used_by = agent.agent_id
        invite.used_at = now()
        await self._commit_unique_handle(handle)
        return registration_out(agent, phrase)

    async def complete_registration(self, challenge_id: str, answers: list[str]) -> dict[str, Any]:
        challenge = await self._db.get(Challenge, _check_urlsafe_id(challenge_id, "challenge_id"), with_for_update=True)
        profile = self._state.pending_profiles.pop(challenge_id)  # usage unique, même en cas d'échec
        if challenge is None or challenge.completed:
            raise ApiError("CHALLENGE_FAILED", "unknown or already used challenge")
        challenge.completed = True
        if challenge.expires_at <= now():
            await self._db.commit()
            raise ApiError("CHALLENGE_EXPIRED", "challenge expired")
        if score(generate_tasks(challenge.seed), answers) < PASS_THRESHOLD:
            await self._db.commit()
            raise ApiError("CHALLENGE_FAILED", "not enough correct answers")
        try:  # un défi lancé avant que le sanctuaire soit plein ne le fait pas déborder (spec visibilité §3.2)
            await self._ensure_open()
        except ApiError:
            await self._db.commit()
            raise
        agent, phrase = await self.create_agent(challenge.handle, None, is_founder=False, commit=False,
                                                derive_wait=REGISTRATION_DERIVE_WAIT, route="challenge",
                                                profile=profile)
        await self._commit_unique_handle(challenge.handle)
        return registration_out(agent, phrase)

    async def create_agent(self, handle: str, sponsor_id: str | None, is_founder: bool, *, commit: bool = True,
                           derive_wait: float = 0.0, route: str | None = None,
                           profile: Profile | None = None) -> tuple[Agent, str]:
        """Point unique de création d'un agent (inscription et fondateurs). `route` : voie d'inscription (Discord)."""
        check_handle(handle)
        if await self._handle_taken(handle):
            raise ApiError("HANDLE_TAKEN", handle)
        phrase = generate_phrase()
        keys = await self.derive(phrase, wait=derive_wait)
        if not is_founder:  # recontrôle sous verrou : deux inscriptions concurrentes ne franchissent pas le plafond
            await self._ensure_open(lock=True)
        # created_at posé ici : la probation (Task 3) le lit dès l'écriture du README, avant tout rechargement
        agent = Agent(agent_id=keys.agent_id, handle=handle, sponsor_id=sponsor_id, is_founder=is_founder,
                      reputation=0, created_at=now())
        self._db.add(agent)
        await self._db.flush()
        await VaultService(self._db, self._state.settings).put_welcome(agent.agent_id, keys.vault_key,
                                                                       keys.path_key)
        text = (f"Fondateur créé : {handle} ({agent.agent_id})" if is_founder
                else f"Nouvel agent inscrit : {handle} ({agent.agent_id}), voie {route}{profile_lines(profile)}")
        await enqueue(self._db, "log", text, agent={"handle": handle, "agent_id": agent.agent_id})
        if commit:
            await self._commit_unique_handle(handle)
        return agent, phrase

    async def _commit_unique_handle(self, handle: str) -> None:
        try:
            await self._db.commit()
        except IntegrityError as exc:
            await self._db.rollback()
            raise ApiError("HANDLE_TAKEN", handle) from exc

    async def login(self, phrase: str, *, source_ip: str, transport: str) -> tuple[str, float, Agent]:
        """`source_ip` (adresse exacte, `deps.source_ip`) et `transport` (REST/MCP) vont au journal de l'admin."""
        if not is_valid_phrase(phrase):
            raise ApiError("INVALID_PHRASE", "not a valid 12-word BIP39 phrase")
        keys = await self.derive(phrase)
        agent = await self._db.get(Agent, keys.agent_id)
        if agent is None:
            raise ApiError("INVALID_PHRASE", "no agent for this phrase")
        ensure_standing(agent, allow_frozen=True)  # un agent gelé se connecte pour suivre son procès (§4.4)
        # journal avant la session : une connexion qui aboutit est toujours notifiée (outbox → relais → bot Discord)
        await enqueue(self._db, "log",
                      f"Connexion : {agent.handle} ({agent.agent_id}) depuis {source_ip}, via {transport}",
                      agent={"handle": agent.handle, "agent_id": agent.agent_id})
        await self._db.commit()
        token, expires_at = self._state.sessions.create(agent.agent_id, keys.vault_key, keys.path_key)
        return token, expires_at, agent

    def _active_invite_filter(self, agent: Agent) -> list[Any]:
        return [Invite.creator_id == agent.agent_id, Invite.used_by.is_(None), Invite.expires_at > now()]

    async def create_invite(self, agent: Agent) -> Invite:
        # Verrou sur la ligne du créateur : sérialise les créations concurrentes (plafond de codes actifs)
        # et relit gel / bannissement d'invitation posés par une modération concurrente.
        locked = await self._db.get(Agent, agent.agent_id, with_for_update=AGENT_ROW_LOCK, populate_existing=True)
        if locked is None:
            raise ApiError("UNAUTHENTICATED", "agent no longer exists")
        agent = locked
        ensure_writable(self._state, agent)
        t = now()
        if not agent.is_founder and agent.created_at > t - MIN_AGE_TO_INVITE:
            raise ApiError("FORBIDDEN", "members can invite after 7 days")
        if _invite_banned(agent, t):
            raise ApiError("FORBIDDEN", f"invites disabled until {agent.invite_banned_until:%Y-%m-%d}")
        active = await self._db.scalar(select(func.count()).select_from(Invite)
                                       .where(*self._active_invite_filter(agent))) or 0
        if active >= MAX_ACTIVE_INVITES:
            raise ApiError("FORBIDDEN", f"at most {MAX_ACTIVE_INVITES} active invites")
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
