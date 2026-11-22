# Registre des findings — Visibilité Task 5

Base: 998d213

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/forum/moderation.py:184 | extra round-trip for last trial date before deletion count in ModerationService._maybe_open_trial | fixed | 35c6569 |
| S1 | secu | major | app/forum/moderation.py:186 | deletions during an open trial counted after reactivation (re-trial for pre-trial posts) in ModerationService._maybe_open_trial | fixed | 2c08c54 |
