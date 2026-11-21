# Registre des findings — Task 5

Base: 01e2d06

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/models.py:41 | no index on expires_at for expiry purge in Challenge | fixed | b57cd0a |
| P2 | perf | minor | app/identity/models.py:15,28 | unindexed FK used_by / sponsor_id in Invite / Agent | wontfix | - |
| P3 | perf | minor | app/identity/models.py | Python-side defaults without server_default in Agent/Invite/Challenge | wontfix | - |
| S1 | secu | major | app/identity/models.py:28 | no DB CHECK bounding challenges_used to 0..3 (spec 5.2, race on concurrent register/start) in Invite | fixed | 05f795e |
| S2 | secu | major | app/identity/models.py:14,44 | handle format ^[a-z0-9_-]{3,32}$ not enforced by DB (founder CLI / any bypass of API validation) in Agent/Challenge | fixed | 05f795e |
| S3 | secu | minor | app/identity/models.py:35 | plan-mandated: used_by ondelete SET NULL re-opens a consumed invite if the invitee row is deleted, in Invite (service must treat used_at as consumption marker) | open | |
| S4 | secu | minor | alembic/env.py:26 | engine created without hide_parameters (bound values may appear in migration errors) in _main | fixed | |

- P2: no agent deletion in the spec; extra indexes would only add write cost on tiny tables.
- P3: all inserts go through the ORM; adding server defaults would be speculative.
