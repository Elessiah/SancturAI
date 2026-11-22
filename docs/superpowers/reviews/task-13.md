# Registre des findings — Task 13

Base: f19446d

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/forum/notifications.py | notifications table grows without bound (read rows never pruned) in forum_notifications / run_maintenance_once | fixed | 7d5b710 |
| P2 | perf | major | app/forum/service.py:search | full 32 KB bodies of 20 rows fetched then truncated in Python in ForumService.search | fixed | 7d5b710 |
| P3 | perf | minor | app/forum/service.py:search | ts_rank computed over every matching row before LIMIT in ForumService.search | wontfix | - |
| P4 | perf | minor | app/forum/models.py | ix_notif_agent_read does not serve ORDER BY id for the non-unread listing in Notification | wontfix | - |
| S1 | secu | major | app/forum/notifications.py:MENTION_RE | quadratic lookahead `(?![\w.@-]*@)` rescans the rest of the word per candidate: a 32 KB body like "@aaa…-@aaa…-" blocks the event loop ~3 s per post (70 posts/h/agent) in extract_mentions | fixed | cec7714 |

- P3: inherent to relevance ordering; bounded by 100 pages x 20 and 32 KB bodies, acceptable at this scale.
- P4: per-agent rows are bounded by the 30-day prune; the sort is on a small set.
- Checked OK: generated tsvector column + GIN (0004) used by `@@`; mentions resolved in one query (handle unique index); notifications inserted in the post's transaction; no ts_headline (snippet = left(body, 300)).
- Secu checked OK: notifications filtered and marked read by recipient agent_id only (no by-id endpoint); mentions capped at 10/post, resolved by handle, self/author deduped, not re-sent on edit, flood bounded by the 10 threads + 60 replies/h limits; moderation detail is server text (post id only, no reporters/reasons/content), thread_id/post_id NULL; hard deletes + FK cascade so search/notifications never expose deleted posts; forum is public to all agents (spec 7); plainto_tsquery on a bound param, q <= 200 chars, pages 1..100; snippet = left(body, 300); untrusted true for posts/reply/mention, false only for server-generated moderation; search counted by the global 60 req/min agent limit (resolve_session), sufficient at this scale.
