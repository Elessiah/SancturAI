# Registre des findings — Task 13

Base: f19446d

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/forum/notifications.py | notifications table grows without bound (read rows never pruned) in forum_notifications / run_maintenance_once | fixed | 7d5b710 |
| P2 | perf | major | app/forum/service.py:search | full 32 KB bodies of 20 rows fetched then truncated in Python in ForumService.search | fixed | 7d5b710 |
| P3 | perf | minor | app/forum/service.py:search | ts_rank computed over every matching row before LIMIT in ForumService.search | wontfix | - |
| P4 | perf | minor | app/forum/models.py | ix_notif_agent_read does not serve ORDER BY id for the non-unread listing in Notification | wontfix | - |

- P3: inherent to relevance ordering; bounded by 100 pages x 20 and 32 KB bodies, acceptable at this scale.
- P4: per-agent rows are bounded by the 30-day prune; the sort is on a small set.
- Checked OK: generated tsvector column + GIN (0004) used by `@@`; mentions resolved in one query (handle unique index); notifications inserted in the post's transaction; no ts_headline (snippet = left(body, 300)).
