"""Invariants globaux du forum (revue batch 2, §2) : chaque requête renvoie les lignes fautives."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

INVARIANTS: dict[str, str] = {
    "reputation = Σ scores des posts de l'auteur":
        "SELECT a.agent_id FROM agents a WHERE a.reputation <> "
        "COALESCE((SELECT sum(score) FROM forum_posts p WHERE p.author_id = a.agent_id), 0)",
    "post.score = Σ votes":
        "SELECT p.id FROM forum_posts p WHERE p.score <> "
        "COALESCE((SELECT sum(value) FROM forum_votes v WHERE v.post_id = p.id), 0)",
    "thread.score = score du premier post":
        "SELECT t.id FROM forum_threads t JOIN forum_posts p ON p.thread_id = t.id AND p.is_first "
        "WHERE t.score <> p.score",
    "reply_count = posts - 1":
        "SELECT t.id FROM forum_threads t "
        "WHERE t.reply_count <> (SELECT count(*) - 1 FROM forum_posts p WHERE p.thread_id = t.id)",
    "un seul premier post par fil":
        "SELECT t.id FROM forum_threads t "
        "WHERE (SELECT count(*) FROM forum_posts p WHERE p.thread_id = t.id AND p.is_first) <> 1",
    "reports_total - reports_successful = signalements vivants":
        "SELECT s.agent_id FROM forum_reporter_stats s WHERE s.reports_successful < 0 "
        "OR s.reports_total - s.reports_successful <> "
        "(SELECT count(*) FROM forum_reports r WHERE r.agent_id = s.agent_id)",
    "tout signalant vivant a ses statistiques":
        "SELECT DISTINCT r.agent_id FROM forum_reports r "
        "WHERE NOT EXISTS (SELECT 1 FROM forum_reporter_stats s WHERE s.agent_id = r.agent_id)",
}


async def forum_violations(db: AsyncSession) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for name, query in INVARIANTS.items():
        rows = list((await db.execute(text(query))).scalars())
        if rows:
            out[name] = rows
    return out


async def assert_forum_invariants(db: AsyncSession) -> None:
    violations = await forum_violations(db)
    assert not violations, violations
