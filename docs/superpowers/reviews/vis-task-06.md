# Registre des findings — Visibilité Task 6

Base: d0ef037

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/forum/moderation.py:92 | row-by-row vote retraction (~8 round trips per vote, thread/post/agent locks held across them) in ModerationService.retract_votes | open | |
| P2 | perf | minor | app/tribunal/ban.py:53 | redundant per-post db.get and two author scans before remove_post in ban_agent | open | |
