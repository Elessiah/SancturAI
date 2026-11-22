# Registre des findings — Visibilité Task 1

Base: 0ca7ec5

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | alembic/versions/0008_tribunal.py:119 | hard-coded 'tribunal' slug duplicates TRIBUNAL_SLUG in downgrade | fixed | bba591f |
| P2 | perf | minor | app/forum/notifications.py:37 | mention of system account creates never-read notification rows in notify_new_post | fixed | bba591f |
| P3 | perf | minor | app/identity/service.py:117 | unreachable invite_code None guard (mypy narrowing only) in complete_registration | wontfix | |

- P3 wontfix : garde temporaire nécessaire au typage depuis que `Challenge.invite_code` est nullable ; `complete_registration` est réécrite par la Task 2 (inscription ouverte), qui la supprime.
- Vérification demandée (requêtes brutes sur `agents`) : aucun code applicatif ne compte ni n'itère tous les agents à ce stade ; côté tests, seuls `tests/forum/test_threads.py` (déjà filtrés `WHERE NOT is_system`) et `tests/forum/invariants.py` (réputation du compte système = 0 = Σ de ses posts, invariant vrai) parcourent la table.
