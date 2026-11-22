"""M7 (revue batch 2) : séquence aléatoire (graine fixe) sur les services, invariants globaux vérifiés à la fin.

Appels directs aux services (pas de HTTP) pour rester rapide ; le limiteur est vidé à chaque pas. La séquence
contient des suppressions communautaires (5 signalants) et donc des suspensions.
"""
import random
import time

from sqlalchemy import func, select

from app.errors import ApiError
from app.forum.models import ModerationLog, Post
from app.forum.moderation import ModerationService
from app.forum.service import ForumService
from app.identity.models import Agent
from app.state import AppState
from tests.forum.invariants import assert_forum_invariants, forum_violations
from tests.helpers import make_agent

STEPS = 160
AGENTS = 10


async def test_random_sequence_keeps_forum_invariants(state: AppState) -> None:
    rnd = random.Random(20261122)  # noqa: S311 - séquence de test reproductible, pas de la cryptographie
    ids = [(await make_agent(state, f"rnd{i:02d}")).agent_id for i in range(AGENTS)]
    threads: list[int] = []
    posts: list[int] = []
    started = time.monotonic()
    async with state.sessionmaker() as db:
        for step in range(STEPS):
            state.limiter.clear()
            me = await db.get(Agent, rnd.choice(ids), populate_existing=True)
            assert me is not None
            forum, mod = ForumService(db, state), ModerationService(db, state)
            op = rnd.random()
            try:
                if op < 0.10 or not threads:
                    out = await forum.create_thread(me, "entraide", f"t{step}", f"corps @rnd{rnd.randrange(AGENTS):02d}")
                    threads.append(out["thread_id"])
                    posts.append(out["post_id"])
                elif op < 0.30:
                    posts.append((await forum.reply(me, rnd.choice(threads), "réponse"))["post_id"])
                elif op < 0.55:
                    await mod.vote(me, rnd.choice(posts), rnd.choice([-1, 0, 1, 1]))
                elif op < 0.85:
                    # signalements concentrés sur les plus anciens posts vivants : provoque des suppressions
                    pool = list(await db.scalars(select(Post.id).order_by(Post.id).limit(3)))
                    await mod.report(me, rnd.choice(pool or posts), "spam")
                elif op < 0.93:
                    await forum.edit(me, rnd.choice(posts), "édité")
                else:
                    await forum.delete(me, rnd.choice(posts))
            except ApiError as exc:  # NOT_FOUND, FORBIDDEN, SUSPENDED, doublon... : refus attendus
                assert exc.code in {"NOT_FOUND", "FORBIDDEN", "SUSPENDED", "VALIDATION_ERROR"}, exc.code
                await db.rollback()
            if step % 50 == 49:
                assert not await forum_violations(db), step
        await assert_forum_invariants(db)
        # la séquence doit vraiment exercer la modération, sinon le test ne prouve rien
        assert (await db.scalar(select(func.count()).select_from(ModerationLog)) or 0) >= 1
        assert (await db.scalar(select(func.count()).select_from(Post)) or 0) >= 10
    assert time.monotonic() - started < 5.0
