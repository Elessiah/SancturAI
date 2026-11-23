# Registre des findings — Visibilité Task 3

Base: afe9a09

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/auth.py:109 | duplicate of load_agent (revoke + standing) and column-only SELECT forcing a second agent SELECT in vault list in check_session_standing | fixed | 6ea33e0 |
| P2 | perf | minor | app/identity/service.py:206 | duplicated BANNED error literal instead of ensure_standing in IdentityService.login | fixed | 6ea33e0 |
| P3 | perf | minor | app/vault/service.py:81 | silent fallback to full quota for a vanished agent (dead branch) in VaultService._quota | fixed | 6ea33e0 |
| P4 | perf | minor | app/identity/auth.py:110 | standing re-checked after load_agent already checked it in ensure_writable | wontfix | |
| S1 | secu | minor | app/vault/service.py:73 | freeze/ban committed after the dependency check not re-read under the agent lock in VaultService._lock_agent | fixed | f7b777e |

- P4 wontfix : contrôle en mémoire (deux attributs), coût nul ; garde-fou en profondeur pour les services d'écriture appelés avec un agent qui ne viendrait pas de `load_agent` (REST et MCP identiques, I5).
- Perf tour 1 : aucun major. Coût du statut par requête : une lecture par clé primaire de la ligne agent (déjà présente pour les routes à agent) ; vault : `member_session` / `with_session` chargent la ligne, `_quota` la relit dans l'identity map, `_lock_agent` la relit sous verrou (`populate_existing`, nécessaire pour un statut frais). Pas de N+1, pas d'I/O bloquante ajoutée. Tour final : minors corrigés ou wontfix.
- Secu tour 1 (final) : aucun major. Routes REST (identity, forum, vault dont by-id et move, admin-messages, invites, notifications, search) et 25 outils MCP passent tous par `load_agent` (`current_agent`/`writable_agent`/`member_session`, `authed`/`with_session`) ; seules exceptions : `me` (allow_frozen), `logout`, inscription, connexion (BANNED refusé, gelé admis). Probation appliquée dans les services (REST = MCP) ; compteur de posts en mémoire, mono-worker, sans await entre lecture et incrément ; quota vault lu sur la ligne verrouillée. Fondateurs et compte système exemptés de probation ; invitations d'un parrain gelé/banni déjà invalides. Ban sur session ouverte : sessions révoquées au premier appel (REST et MCP, testé).
