# Registre des findings — Visibilité Task 6

Base: d0ef037

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/forum/moderation.py:92 | row-by-row vote retraction (~8 round trips per vote, thread/post/agent locks held across them) in ModerationService.retract_votes | fixed | a86143e |
| P2 | perf | minor | app/tribunal/ban.py:53 | redundant per-post db.get and two author scans before remove_post in ban_agent | fixed | 8800b56 |
| S1 | secu | major | app/tribunal/ban.py:39 | in-flight write of the banned agent (reply, thread, vote, report) commits after the purge scan and survives the ban in ban_agent | fixed | 0b35890 |
| S2 | secu | minor | app/tribunal/ban.py:48 | `banned-` prefix not reserved at registration (impersonation of a banned account; handle collision fails the ban with a raw IntegrityError) in ban_agent | fixed | 9cd82a7 |
| S3 | secu | minor | app/tribunal/opening.py:57 | trial thread title and dossier keep the freed handle (a new holder of the handle appears as the accused) in ban_agent | fixed | 9cd82a7 |
| S4 | secu | minor | app/tribunal/ban.py:57 | sessions.revoke_agent is a no-op from the CLI process: the banned agent's in-memory sessions and vault keys survive in the server until next use or TTL (24 h) in ban_agent | fixed | 9cd82a7 |

Perf round 2: no new findings (final perf round).
Secu round 2: no new major (final secu round). S1 lock reviewed: held per request transaction (released at the service commit; read routes until session close, bounded by nginx buffering and BODY_HOLD), one MCP tool call = one transaction, no route takes a row lock before load_agent, two-int key space disjoint from the bigint registration lock, re-entrant shared acquisition, Argon2 only on unauthenticated routes.
