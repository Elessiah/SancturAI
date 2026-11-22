import hmac
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_messages.models import AdminMessage, AdminThread
from app.config import Settings
from app.errors import ApiError
from app.identity.models import Agent
from app.outbox.service import MAX_TEXT as OUTBOX_MAX_TEXT
from app.outbox.service import enqueue
from app.state import AppState
from app.text import check_text

MAX_TEXT = OUTBOX_MAX_TEXT["agent_message"]  # le DM Discord ne doit jamais tronquer le message
MAX_RAW_TEXT = MAX_TEXT + 100  # marge d'espaces avant strip() ; borne le corps côté schéma
DAILY_LIMIT = 3
_SHORT_ID_TRIES = 20
_SHORT_ID_CONSTRAINT = "admin_threads_short_id_key"
INBOX_THREADS = 50
INBOX_MESSAGES_PER_THREAD = 50  # les plus récents ; l'historique complet ne doit pas être relu à chaque appel


# Exactement la réponse d'une route inexistante (handler StarletteHTTPException) : aucun oracle d'existence.
INTERNAL_NOT_FOUND_MESSAGE = "Not Found"


def verify_internal(request: Request, settings: Settings) -> None:
    """Réservé à l'hôte (bot). Toute requête relayée par nginx porte X-Forwarded-For → refusée."""
    provided = request.headers.get("x-internal-token", "")
    if (not settings.internal_token or "x-forwarded-for" in request.headers
            or not hmac.compare_digest(provided.encode(), settings.internal_token.encode())):
        raise ApiError("NOT_FOUND", INTERNAL_NOT_FOUND_MESSAGE)


def _check_text(text: str) -> str:
    text = check_text(text, "text").strip()  # NUL / contrôles C0 → VALIDATION_ERROR (revue batch 1, I1)
    if not text or len(text) > MAX_TEXT:
        raise ApiError("VALIDATION_ERROR", f"text must be 1..{MAX_TEXT} chars")
    return text


def _violated_constraint(exc: IntegrityError) -> str | None:
    cause = getattr(exc.orig, "__cause__", None) or exc.orig
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


class AdminMessageService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    async def _check_daily_limit(self, agent: Agent) -> None:
        since = datetime.now(UTC) - timedelta(days=1)
        stamps = (await self._db.scalars(
            select(AdminMessage.created_at).join(AdminThread, AdminThread.id == AdminMessage.thread_id)
            .where(AdminThread.agent_id == agent.agent_id, AdminMessage.sender == "agent",
                   AdminMessage.created_at >= since).order_by(AdminMessage.created_at))).all()
        if len(stamps) >= DAILY_LIMIT:
            retry = math.ceil((stamps[0] + timedelta(days=1) - datetime.now(UTC)).total_seconds())
            raise ApiError("RATE_LIMITED", f"{DAILY_LIMIT} messages per day", retry_after=max(1, retry))

    async def _create_thread(self, agent: Agent) -> AdminThread:
        # INSERT optimiste (1 requête/essai) : la contrainte UNIQUE tranche, y compris entre requêtes concurrentes
        for _ in range(_SHORT_ID_TRIES):
            thread = AdminThread(short_id=secrets.token_hex(2), agent_id=agent.agent_id)
            try:
                async with self._db.begin_nested():
                    self._db.add(thread)
                    await self._db.flush()
            except IntegrityError as exc:
                if _violated_constraint(exc) != _SHORT_ID_CONSTRAINT:
                    raise  # autre contrainte (FK agent supprimé…) : inutile de réessayer
                continue
            return thread
        raise ApiError("SERVER_BUSY", "no free short id")

    async def send(self, agent: Agent, text: str, short_id: str | None = None) -> dict[str, Any]:
        text = _check_text(text)
        await self._check_daily_limit(agent)
        if short_id:
            thread = await self._db.scalar(select(AdminThread).where(AdminThread.short_id == short_id,
                                                                     AdminThread.agent_id == agent.agent_id))
            if thread is None:
                raise ApiError("NOT_FOUND", f"no thread #{short_id}")
            thread.updated_at = datetime.now(UTC)
        else:
            thread = await self._create_thread(agent)
        # le compteur DB ci-dessus survit aux redémarrages ; le limiteur ferme la course entre requêtes concurrentes
        self._state.limiter.hit(f"admin-msg:{agent.agent_id}", DAILY_LIMIT, 86_400)
        message = AdminMessage(thread_id=thread.id, sender="agent", text=text, read_by_agent=True)
        self._db.add(message)
        await enqueue(self._db, "agent_message", text, short_id=thread.short_id,
                      agent={"handle": agent.handle, "agent_id": agent.agent_id})
        await self._db.commit()
        return {"short_id": thread.short_id, "message_id": message.id}

    async def reply(self, short_id: str, text: str) -> None:
        text = _check_text(text)
        thread = await self._db.scalar(select(AdminThread).where(AdminThread.short_id == short_id))
        if thread is None:
            raise ApiError("NOT_FOUND", f"no thread #{short_id}")
        self._db.add(AdminMessage(thread_id=thread.id, sender="admin", text=text, read_by_agent=False))
        thread.updated_at = datetime.now(UTC)
        await self._db.commit()

    async def inbox(self, agent: Agent) -> dict[str, Any]:
        threads = (await self._db.scalars(select(AdminThread).where(AdminThread.agent_id == agent.agent_id)
                                          .order_by(AdminThread.updated_at.desc()).limit(INBOX_THREADS))).all()
        by_thread: dict[int, list[AdminMessage]] = {t.id: [] for t in threads}
        if threads:
            rank = (func.row_number().over(partition_by=AdminMessage.thread_id, order_by=AdminMessage.id.desc())
                    .label("rn"))
            recent = (select(AdminMessage.id.label("id"), rank).where(AdminMessage.thread_id.in_(by_thread))
                      .subquery())
            msgs = await self._db.scalars(select(AdminMessage).join(recent, recent.c.id == AdminMessage.id)
                                          .where(recent.c.rn <= INBOX_MESSAGES_PER_THREAD)
                                          .order_by(AdminMessage.id))
            for m in msgs:
                by_thread[m.thread_id].append(m)
        out = [{"short_id": t.short_id, "unread": sum(1 for m in by_thread[t.id] if not m.read_by_agent),
                "messages": [{"sender": m.sender, "text": m.text, "created_at": m.created_at.isoformat()}
                             for m in by_thread[t.id]]} for t in threads]
        shown = [m.id for msgs in by_thread.values() for m in msgs if not m.read_by_agent]
        if shown:  # seulement les messages renvoyés : une réponse arrivée entre-temps reste non lue
            await self._db.execute(update(AdminMessage).where(AdminMessage.id.in_(shown)).values(read_by_agent=True))
            await self._db.commit()
        return {"threads": out}
