# Registre des findings — Task 8

Base: 38e8569

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/service.py:134 | redundant `(active or 0)` and literal "3" duplicating MAX_ACTIVE_INVITES in create_invite | fixed | 04c5d9f |
| P2 | perf | minor | app/identity/api.py:108 | hardcoded 3 duplicating MAX_CHALLENGES_PER_INVITE in invite_out | fixed | 04c5d9f |
| P3 | perf | minor | app/identity/service.py:157 | repeated now() calls (two timestamps) in suspend | fixed | 04c5d9f |
| P4 | perf | minor | app/identity/service.py:21 | stray triple blank lines around constants block in service module | fixed | 04c5d9f |
