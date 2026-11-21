# Registre des findings — Task 5

Base: 01e2d06

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/models.py:41 | no index on expires_at for expiry purge in Challenge | fixed | |
| P2 | perf | minor | app/identity/models.py:15,28 | unindexed FK used_by / sponsor_id in Invite / Agent | wontfix | |
| P3 | perf | minor | app/identity/models.py | Python-side defaults without server_default in Agent/Invite/Challenge | wontfix | |

- P2: no agent deletion in the spec; extra indexes would only add write cost on tiny tables.
- P3: all inserts go through the ORM; adding server defaults would be speculative.
