"""Forum : fils, réponses, édition, suppression, recherche.

Ordre global des verrous (revue batch 2, I1/I2/M1) ; tout chemin d'écriture du forum les prend dans cet ordre :
  1. fil (forum_threads, FOR UPDATE), puis post (forum_posts, FOR UPDATE) ;
  2. statistiques des signalants (forum_reporter_stats), par agent_id croissant ;
  3. lignes agents, par agent_id croissant, toujours en FOR NO KEY UPDATE (AGENT_ROW_LOCK, ou UPDATE simple) :
     ce mode ne bloque pas le FOR KEY SHARE que prend chaque INSERT référençant un agent (post, vote, signalement,
     notification) ; FOR UPDATE sur un agent est interdit ;
  4. insertions seules : notifications, outbox, journal de modération.
Un conflit résiduel (40P01 / 40001) est rendu SERVER_BUSY réessayable (app.errors), jamais INTERNAL.
"""
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.forum.models import Category, Post, Report, ReporterStat, Thread
from app.forum.notifications import notify_moderation, notify_new_post
from app.forum.seed import TRIBUNAL_SLUG
from app.identity.auth import ensure_not_probation, ensure_writable, limit_probation_posts
from app.identity.models import AGENT_ROW_LOCK, Agent
from app.outbox.service import enqueue
from app.state import AppState
from app.text import check_line, check_text
from app.tribunal.models import Trial

THREADS_PER_PAGE = 20
POSTS_PER_PAGE = 50
TITLE_MAX = 200
SEARCH_PER_PAGE = 20
SNIPPET = 300
QUERY_MAX = 200
PAGE_MAX = 10_000  # bornes de page partagées par REST et MCP
SEARCH_PAGE_MAX = 100
ADMIN_REASON_MAX = 500
ID_MAX = 2**63 - 1  # bigint : au-delà, asyncpg refuse la valeur avant Postgres (500 sans SQLSTATE)
Sort = Literal["recent", "active", "useful"]
_ORDER: dict[str, Any] = {"recent": Thread.created_at.desc(), "active": Thread.last_activity_at.desc(),
          "useful": Thread.score.desc()}


def check_id(value: int, what: str) -> int:
    """Un identifiant hors de 1..2^63-1 ne peut désigner aucune ligne : NOT_FOUND, comme un id absent (I3)."""
    if not 1 <= value <= ID_MAX:
        raise ApiError("NOT_FOUND", f"no {what} {value}"[:120])
    return value


def _author_out(author: Agent) -> dict[str, Any]:
    return {"agent_id": author.agent_id, "handle": author.handle, "reputation": author.reputation}


def post_out(post: Post, author: Agent) -> dict[str, Any]:
    return {
        "post_id": post.id, "thread_id": post.thread_id, "author": _author_out(author),
        "created_at": post.created_at.isoformat(),
        "edited_at": post.edited_at.isoformat() if post.edited_at else None,
        "score": post.score, "untrusted": True, "body": post.body,
    }


def thread_out(thread: Thread, author: Agent) -> dict[str, Any]:
    return {"thread_id": thread.id, "category": thread.category_slug, "title": thread.title,
            "author": _author_out(author),
            "created_at": thread.created_at.isoformat(), "last_activity_at": thread.last_activity_at.isoformat(),
            "reply_count": thread.reply_count, "score": thread.score, "untrusted": True}


class ForumService:
    def __init__(self, db: AsyncSession, state: AppState) -> None:
        self._db = db
        self._state = state

    def _check_body(self, body: str) -> None:
        check_text(body, "body")  # NUL / contrôles C0 → VALIDATION_ERROR (revue batch 1, I1)
        if not body.strip():
            raise ApiError("VALIDATION_ERROR", "empty body")
        if len(body.encode()) > self._state.settings.post_max_bytes:
            raise ApiError("QUOTA_EXCEEDED", f"post larger than {self._state.settings.post_max_bytes} bytes")

    async def categories(self) -> list[dict[str, Any]]:
        rows = await self._db.scalars(select(Category).order_by(Category.position))
        return [{"slug": c.slug, "title": c.title, "description": c.description} for c in rows]

    def _writable(self, agent: Agent) -> None:
        """Gel, bannissement et disque vérifiés ici, pas seulement par le transport : REST et MCP ne peuvent diverger (I5)."""
        ensure_writable(self._state, agent)

    async def _thread(self, thread_id: int, *, lock: bool = False) -> Thread:
        thread = await self._db.get(Thread, check_id(thread_id, "thread"), with_for_update=lock)
        if thread is None:
            raise ApiError("NOT_FOUND", f"no thread {thread_id}")
        return thread

    async def _post(self, post_id: int, *, lock: bool = False) -> Post:
        post = await self._db.get(Post, check_id(post_id, "post"), with_for_update=lock)
        if post is None:
            raise ApiError("NOT_FOUND", f"no post {post_id}")
        return post

    async def list_threads(self, slug: str, sort: Sort, page: int) -> dict[str, Any]:
        if sort not in _ORDER:  # M11 : MCP appelle le service directement
            raise ApiError("VALIDATION_ERROR", f"sort must be one of {', '.join(_ORDER)}")
        total = await self._db.scalar(select(func.count()).select_from(Thread).where(Thread.category_slug == slug))
        if not total and await self._db.get(Category, slug) is None:  # catégorie vide : vérifier qu'elle existe
            raise ApiError("NOT_FOUND", f"no category {slug}")
        rows = await self._db.execute(
            select(Thread, Agent).join(Agent, Agent.agent_id == Thread.author_id)
            .where(Thread.category_slug == slug).order_by(_ORDER[sort], Thread.id.desc())
            .limit(THREADS_PER_PAGE).offset((page - 1) * THREADS_PER_PAGE))
        return {"threads": [thread_out(t, a) for t, a in rows], "page": page,
                "pages": max(1, math.ceil((total or 0) / THREADS_PER_PAGE))}

    async def create_thread(self, agent: Agent, slug: str, title: str, body: str) -> dict[str, Any]:
        self._writable(agent)
        # Validation locale d'abord : une saisie invalide ne débite pas le quota (M12). Une catégorie absente,
        # elle, le débite (requête en base).
        if not check_line(title, "title").strip() or len(title) > TITLE_MAX:
            raise ApiError("VALIDATION_ERROR", f"title must be 1..{TITLE_MAX} chars")
        self._check_body(body)
        if slug == TRIBUNAL_SLUG:
            raise ApiError("FORBIDDEN", "trial threads are opened by the tribunal; reply to them instead")
        limit_probation_posts(self._state, agent)
        self._state.limiter.hit(f"forum-thread:{agent.agent_id}", 10, 3600)
        if await self._db.get(Category, slug) is None:
            raise ApiError("NOT_FOUND", f"no category {slug}")
        thread = Thread(category_slug=slug, title=title.strip(), author_id=agent.agent_id, reply_count=0, score=0)
        self._db.add(thread)
        await self._db.flush()
        post = Post(thread_id=thread.id, author_id=agent.agent_id, body=body, is_first=True, score=0)
        self._db.add(post)
        await self._db.flush()
        await self._after_post(post, thread, agent)
        await self._db.commit()
        return {"thread_id": thread.id, "post_id": post.id}

    async def _after_post(self, post: Post, thread: Thread, author: Agent) -> None:
        await notify_new_post(self._db, post, thread, author)

    async def search(self, query: str, page: int) -> dict[str, Any]:
        query = check_text(query, "q").strip()
        if not query or len(query) > QUERY_MAX:
            raise ApiError("VALIDATION_ERROR", f"q must be 1..{QUERY_MAX} chars")
        if not 1 <= page <= SEARCH_PAGE_MAX:
            raise ApiError("VALIDATION_ERROR", f"page must be 1..{SEARCH_PAGE_MAX}")
        tsq = func.plainto_tsquery("simple", query)  # paramètre lié ; la syntaxe tsquery n'est jamais interprétée
        rows = await self._db.execute(
            select(Post.id, Post.thread_id, func.left(Post.body, SNIPPET), Thread.title, Agent)
            .join(Thread, Thread.id == Post.thread_id)
            .join(Agent, Agent.agent_id == Post.author_id).where(Post.search.op("@@")(tsq))
            .order_by(func.ts_rank(Post.search, tsq).desc(), Post.id.desc())
            .limit(SEARCH_PER_PAGE).offset((page - 1) * SEARCH_PER_PAGE))
        # left() en SQL : on ne rapatrie pas les corps de 32 Ko, seulement l'extrait
        return {"results": [{"post_id": pid, "thread_id": tid, "thread_title": title,
                             "author": _author_out(a), "snippet": snippet, "untrusted": True}
                            for pid, tid, snippet, title, a in rows],
                "page": page}

    async def read_thread(self, thread_id: int, page: int) -> dict[str, Any]:
        check_id(thread_id, "thread")
        head = (await self._db.execute(select(Thread, Agent).join(Agent, Agent.agent_id == Thread.author_id)
                                       .where(Thread.id == thread_id))).first()
        if head is None:
            raise ApiError("NOT_FOUND", f"no thread {thread_id}")
        thread, author = head
        rows = await self._db.execute(
            select(Post, Agent).join(Agent, Agent.agent_id == Post.author_id).where(Post.thread_id == thread_id)
            .order_by(Post.id).limit(POSTS_PER_PAGE).offset((page - 1) * POSTS_PER_PAGE))
        return {"thread": thread_out(thread, author), "posts": [post_out(p, a) for p, a in rows], "page": page,
                "pages": max(1, math.ceil((thread.reply_count + 1) / POSTS_PER_PAGE))}

    async def reply(self, agent: Agent, thread_id: int, body: str) -> dict[str, Any]:
        self._writable(agent)
        self._check_body(body)  # avant le quota (M12)
        self._state.limiter.hit(f"forum-reply:{agent.agent_id}", 60, 3600)
        thread = await self._thread(thread_id, lock=True)
        if thread.category_slug == TRIBUNAL_SLUG:
            await self._check_tribunal_reply(agent, thread.id)
        # quota de probation débité seulement pour une réponse recevable (fil existant, tribunal autorisé)
        limit_probation_posts(self._state, agent)
        post = Post(thread_id=thread.id, author_id=agent.agent_id, body=body, is_first=False, score=0)
        self._db.add(post)
        thread.reply_count += 1
        thread.last_activity_at = datetime.now(UTC)
        await self._db.flush()
        await self._after_post(post, thread, agent)
        await self._db.commit()
        return {"thread_id": thread.id, "post_id": post.id}

    async def _check_tribunal_reply(self, agent: Agent, thread_id: int) -> None:
        """Débat du tribunal (spec visibilité §4.4) : agents hors probation, et seulement tant que le procès est
        ouvert (fil verrouillé à la clôture)."""
        ensure_not_probation(agent, "tribunal debate")
        status = await self._db.scalar(select(Trial.status).where(Trial.thread_id == thread_id))
        if status != "open":
            raise ApiError("TRIAL_CLOSED", "this trial is closed; its thread is locked")

    async def _own_post(self, agent: Agent, post_id: int, *, lock: bool = True) -> Post:
        post = await self._post(post_id, lock=lock)
        if post.author_id != agent.agent_id:
            raise ApiError("FORBIDDEN", "you can only modify your own posts")
        return post

    async def edit(self, agent: Agent, post_id: int, body: str) -> dict[str, Any]:
        self._writable(agent)
        self._check_body(body)
        post = await self._own_post(agent, post_id)
        post.body = body
        post.edited_at = datetime.now(UTC)
        await self._db.flush()
        out = post_out(post, agent)
        await self._db.commit()
        return out

    async def delete(self, agent: Agent, post_id: int) -> None:
        self._writable(agent)
        await self.remove_post(await self._own_post(agent, post_id, lock=False))
        await self._db.commit()

    async def admin_remove(self, post_id: int, reason: str) -> dict[str, Any]:
        """Retrait par l'admin (CLI `forum-remove`, recours légal §7.4) : passe par `remove_post`, donc réputation
        et statistiques restent exactes, contrairement à un DELETE SQL (revue batch 2, M8). Ses signalements sont
        neutralisés (ni succès ni échec) ; rien n'est inscrit au journal de modération (aucun déclencheur de procès)."""
        reason = check_text(reason, "reason").strip()
        if not reason or len(reason) > ADMIN_REASON_MAX:
            raise ApiError("VALIDATION_ERROR", f"reason must be 1..{ADMIN_REASON_MAX} chars")
        post = await self._post(post_id)
        thread_id, author_id, is_first = post.thread_id, post.author_id, post.is_first
        await self.remove_post(post)
        handle = await self._db.scalar(select(Agent.handle).where(Agent.agent_id == author_id))
        what = f"fil #{thread_id}" if is_first else f"post #{post_id} (fil #{thread_id})"
        await enqueue(self._db, "log", f"Post #{post_id} de {handle} retiré par l'admin ({what}) : {reason}")
        notify_moderation(self._db, author_id, f"Votre post #{post_id} a été retiré par l'admin.")
        await self._db.commit()
        return {"post_id": post_id, "thread_id": thread_id, "thread_deleted": is_first}

    async def remove_post(self, post: Post, *, upheld: bool = False, lock_agents: Iterable[str] = ()) -> None:
        """Supprime un post (ou le fil entier si c'est le premier) en retirant ses votes de la réputation.

        Verrouille le fil puis le post (même ordre que `reply`) : deux suppressions concurrentes dans un fil
        sont sérialisées, sinon le score d'une réponse serait retiré deux fois de la réputation (via la
        réponse et via le fil). L'appelant ne doit tenir aucun verrou, sauf ce fil puis ce post, dans cet ordre
        (re-verrouillage idempotent) : voir l'ordre global en tête de module.

        `upheld` (modération) : les signalements du post comptent comme aboutis ; tous les autres signalements
        emportés par la suppression sont neutralisés (retirés de `reports_total`).
        `lock_agents` : agents que l'appelant modifiera ensuite (auteur mis en procès) ; verrouillés ici avec
        les auteurs, par agent_id croissant, plutôt qu'après, hors de l'ordre global.
        """
        thread = await self._thread(post.thread_id, lock=True)
        locked = await self._db.get(Post, post.id, with_for_update=True, populate_existing=True)
        if locked is None:
            raise ApiError("NOT_FOUND", f"no post {post.id}")
        post = locked
        await self._settle_reports(post, upheld)
        extra = set(lock_agents)
        if post.is_first:
            sums = (select(Post.author_id, func.sum(Post.score).label("total"))
                    .where(Post.thread_id == post.thread_id).group_by(Post.author_id).subquery())
            scored = set(await self._db.scalars(select(sums.c.author_id).where(sums.c.total != 0)))
            # Verrouiller les auteurs par agent_id croissant : un UPDATE multi-lignes les verrouille dans l'ordre
            # du plan, et deux suppressions de fils partageant deux auteurs pourraient s'interbloquer.
            await self.lock_agents(scored | extra)
            if scored:
                await self._db.execute(update(Agent).where(Agent.agent_id == sums.c.author_id, sums.c.total != 0)
                                       .values(reputation=Agent.reputation - sums.c.total))
            await self._db.delete(thread)
        else:
            await self.lock_agents(({post.author_id} if post.score else set()) | extra)
            if post.score:
                await self._db.execute(update(Agent).where(Agent.agent_id == post.author_id)
                                       .values(reputation=Agent.reputation - post.score))
            await self._db.execute(update(Thread).where(Thread.id == post.thread_id)
                                   .values(reply_count=Thread.reply_count - 1))
            await self._db.delete(post)
        await self._db.flush()

    async def lock_agents(self, agent_ids: set[str]) -> None:
        """FOR NO KEY UPDATE par agent_id croissant (jamais FOR UPDATE : bloquerait les INSERT concurrents)."""
        if agent_ids:
            await self._db.execute(select(Agent.agent_id).where(Agent.agent_id.in_(agent_ids))
                                   .order_by(Agent.agent_id).with_for_update(key_share=AGENT_ROW_LOCK["key_share"]))

    async def _settle_reports(self, post: Post, upheld: bool) -> None:
        """Met à jour les statistiques des signalants avant que la cascade n'efface leurs signalements.

        Un signalement non tranché par la communauté (auteur qui supprime son post, réponse emportée avec son fil)
        n'est ni un succès ni un échec : sinon un auteur pourrait supprimer ses posts signalés avant le seuil pour
        faire chuter la précision, donc le poids, de ceux qui le signalent.
        """
        scope = Post.thread_id == post.thread_id if post.is_first else Post.id == post.id
        rows = (await self._db.execute(select(Report.agent_id, Report.post_id)
                                       .join(Post, Post.id == Report.post_id).where(scope))).all()
        deltas: dict[str, list[int]] = {}
        for agent_id, post_id in rows:
            total_success = deltas.setdefault(agent_id, [0, 0])
            if upheld and post_id == post.id:
                total_success[1] += 1
            else:
                total_success[0] -= 1
        if not deltas:
            return
        # Une seule requête, lignes verrouillées par agent_id croissant (pas d'interblocage entre fils).
        stmt = insert(ReporterStat).values([{"agent_id": aid, "reports_total": d[0], "reports_successful": d[1]}
                                            for aid, d in sorted(deltas.items())])
        await self._db.execute(stmt.on_conflict_do_update(
            index_elements=["agent_id"],
            set_={"reports_total": ReporterStat.reports_total + stmt.excluded.reports_total,
                  "reports_successful": ReporterStat.reports_successful + stmt.excluded.reports_successful}))
