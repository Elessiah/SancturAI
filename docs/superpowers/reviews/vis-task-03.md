# Registre des findings — Visibilité Task 3

Base: afe9a09

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/auth.py:109 | duplicate of load_agent (revoke + standing) and column-only SELECT forcing a second agent SELECT in vault list in check_session_standing | fixed | 6ea33e0 |
| P2 | perf | minor | app/identity/service.py:206 | duplicated BANNED error literal instead of ensure_standing in IdentityService.login | fixed | 6ea33e0 |
| P3 | perf | minor | app/vault/service.py:81 | silent fallback to full quota for a vanished agent (dead branch) in VaultService._quota | fixed | 6ea33e0 |
| P4 | perf | minor | app/identity/auth.py:110 | standing re-checked after load_agent already checked it in ensure_writable | wontfix | |

- P4 wontfix : contrôle en mémoire (deux attributs), coût nul ; garde-fou en profondeur pour les services d'écriture appelés avec un agent qui ne viendrait pas de `load_agent` (REST et MCP identiques, I5).
- Perf tour 1 : aucun major. Coût du statut par requête : une lecture par clé primaire de la ligne agent (déjà présente pour les routes à agent) ; vault : `member_session` / `with_session` chargent la ligne, `_quota` la relit dans l'identity map, `_lock_agent` la relit sous verrou (`populate_existing`, nécessaire pour un statut frais). Pas de N+1, pas d'I/O bloquante ajoutée. Tour final : minors corrigés ou wontfix.
