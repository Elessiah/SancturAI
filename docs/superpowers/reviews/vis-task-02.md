# Registre des findings — Visibilité Task 2

Base: 9fe3c1c

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/service.py:126 | redundant SELECT of sponsor handle already loaded by _valid_invite in _register_with_invite | fixed | SHA_TBD |
| P2 | perf | minor | app/identity/api.py:72 | phrase response dict duplicated (REST, MCP, invite path) in register_complete | fixed | SHA_TBD |
| P3 | perf | minor | app/identity/service.py:51 | full COUNT on agents without partial index at each register/start in count_members | wontfix | |
| P4 | perf | minor | app/identity/models.py:54 | dead indexed column never written (old invite-bound challenge) in Challenge.invite_code | wontfix | |
| P5 | perf | minor | app/mcp_server.py:66 | stale comment says UrlsafeId backs REST invite_code in UrlsafeId | fixed | SHA_TBD |
| P6 | perf | minor | app/identity/service.py:160 | misleading default route="challenge" (founders) in create_agent | fixed | SHA_TBD |

- P3 wontfix : table bornée par `max_agents` (+ bannis), endpoint rate-limité par IP ; un seq scan de ~1000 lignes est sub-milliseconde, un index partiel n'apporterait rien de mesurable (count(*) Postgres lit quand même la visibility map).
- P4 wontfix : la spec visibilité §7 garde `challenges.invite_code` (nullable) ; la supprimer est un changement de schéma hors périmètre de la Task 2.
- Perf tour 1 : aucun major (pas de N+1, Argon2 hors boucle d'événements via `to_thread`, ordre des contrôles correct) ; tour final, minors corrigés ou wontfix.
