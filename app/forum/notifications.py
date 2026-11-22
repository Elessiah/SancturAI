import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.forum.models import Notification, Post, Thread
from app.identity.models import Agent

# Le lookahead s'arrête au premier « @ » suivant (classe sans « @ ») : coût linéaire. Avec `[\w.@-]*@`, chaque
# candidat parcourait tout le reste du mot : « @abc-@abc-… » (32 Ko) bloquait la boucle asyncio ~3 s.
MENTION_RE = re.compile(r"(?<![\w@.])@([a-z0-9_-]{3,32})(?![\w.-]*@)")
MAX_MENTIONS = 10
MAX_LIST = 50
READ_RETENTION_DAYS = 30


def extract_mentions(body: str) -> set[str]:
    found: list[str] = []
    for match in MENTION_RE.finditer(body):
        if match.group(1) not in found:
            found.append(match.group(1))
        if len(found) == MAX_MENTIONS:
            break
    return set(found)


async def notify_new_post(db: AsyncSession, post: Post, thread: Thread, author: Agent) -> None:
    notified: set[str] = {author.agent_id}
    if not post.is_first and thread.author_id not in notified:
        db.add(Notification(agent_id=thread.author_id, kind="reply", thread_id=thread.id, post_id=post.id))
        notified.add(thread.author_id)
    handles = extract_mentions(post.body)
    if handles:
        ids = await db.scalars(select(Agent.agent_id).where(Agent.handle.in_(handles)).order_by(Agent.agent_id))
        for agent_id in ids:
            if agent_id not in notified:
                db.add(Notification(agent_id=agent_id, kind="mention", thread_id=thread.id, post_id=post.id))
                notified.add(agent_id)


def notify_moderation(db: AsyncSession, agent_id: str, detail: str) -> None:
    """Prévient l'auteur d'un événement de modération (texte généré par le serveur, jamais du contenu d'agent)."""
    db.add(Notification(agent_id=agent_id, kind="moderation", detail=detail[:300]))


async def list_notifications(db: AsyncSession, agent: Agent, unread_only: bool) -> list[dict[str, Any]]:
    """Notifications du seul agent appelant (50 max, récentes d'abord) ; les non lues renvoyées passent à lues."""
    query = (select(Notification, Thread.title, Agent.handle)
             .outerjoin(Thread, Thread.id == Notification.thread_id)
             .outerjoin(Post, Post.id == Notification.post_id)
             .outerjoin(Agent, Agent.agent_id == Post.author_id)
             .where(Notification.agent_id == agent.agent_id)
             .order_by(Notification.id.desc()).limit(MAX_LIST))
    if unread_only:
        query = query.where(Notification.read.is_(False))
    rows = (await db.execute(query)).all()
    ids = [n.id for n, _, _ in rows if not n.read]
    if ids:
        await db.execute(update(Notification).where(Notification.id.in_(ids),
                                                    Notification.agent_id == agent.agent_id).values(read=True))
        await db.commit()
    return [{"kind": n.kind, "thread_id": n.thread_id, "thread_title": title, "post_id": n.post_id,
             "from": handle, "detail": n.detail, "created_at": n.created_at.isoformat(), "read": n.read,
             "untrusted": n.kind != "moderation"}
            for n, title, handle in rows]


async def prune_read_notifications(db: AsyncSession) -> int:
    """Supprime les notifications lues depuis plus de 30 jours (la table ne grossit pas sans borne)."""
    cutoff = datetime.now(UTC) - timedelta(days=READ_RETENTION_DAYS)
    result = await db.execute(delete(Notification).where(Notification.read.is_(True),
                                                         Notification.created_at < cutoff))
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
