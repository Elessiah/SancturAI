# Registre des findings — Task 11

Base: 6b854c4

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/forum/models.py:Thread | sort indexes (category, key) lack `id` tiebreaker matching ORDER BY key DESC, id DESC (extra sort step) | fixed | 55bb6c1 |
| P2 | perf | minor | app/forum/service.py:read_thread | thread and its author fetched in two queries instead of one join | fixed | 55bb6c1 |
| P3 | perf | minor | app/forum/service.py:remove_post | one UPDATE per distinct author in a loop when deleting a thread | fixed | 55bb6c1 |
| P4 | perf | minor | app/forum/service.py:thread_out | author dict duplicated in post_out / thread_out | fixed | 55bb6c1 |
| P5 | perf | minor | app/forum/service.py:list_threads | category lookup query on every page even when threads exist | fixed | 55bb6c1 |
| P6 | perf | minor | app/forum/service.py:edit | refresh() after commit costs an extra SELECT | fixed | 55bb6c1 |
| P7 | perf | minor | app/forum/models.py:Thread | reply_count/score/is_first have Python defaults only, no server_default (raw inserts fail) | fixed | 55bb6c1 |
| P8 | perf | minor | alembic/versions/0004_forum.py | migration imports app.forum.seed (seed drift would rewrite history) (plan-mandated) | wontfix | - |
| S1 | secu | major | app/forum/service.py:remove_post | concurrent deletes of a reply and the thread's first post not serialized (thread not locked): reply score reverted twice, reputation inflated/deflated | fixed | d593d36 |
| S2 | secu | minor | app/forum/service.py:create_thread | title accepts \n \r \t (multi-line titles can spoof thread listings) | fixed | - |
| S3 | secu | minor | app/forum/service.py:remove_post | thread delete updates several authors' reputation rows in plan order: two concurrent thread deletions sharing two authors can deadlock (500) | fixed | - |

P8 wontfix: brief mandates "autogénérée + seed"; seed list is stable. Offset pagination and per-page count(*) are bounded (page le 10 000, 20/50 rows) and acceptable at Pi scale.
Perf round 1: no new major (final round).
Secu round 1: 1 new major (S1), not final round.
Secu round 2: no new major (final round); S2, S3 fixed.
