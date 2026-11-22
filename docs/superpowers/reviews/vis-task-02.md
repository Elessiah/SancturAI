# Registre des findings — Visibilité Task 2

Base: 9fe3c1c

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/identity/service.py:126 | redundant SELECT of sponsor handle already loaded by _valid_invite in _register_with_invite | fixed | 03caba3 |
| P2 | perf | minor | app/identity/api.py:72 | phrase response dict duplicated (REST, MCP, invite path) in register_complete | fixed | 03caba3 |
| P3 | perf | minor | app/identity/service.py:51 | full COUNT on agents without partial index at each register/start in count_members | wontfix | |
| P4 | perf | minor | app/identity/models.py:54 | dead indexed column never written (old invite-bound challenge) in Challenge.invite_code | wontfix | |
| P5 | perf | minor | app/mcp_server.py:66 | stale comment says UrlsafeId backs REST invite_code in UrlsafeId | fixed | 03caba3 |
| P6 | perf | minor | app/identity/service.py:160 | misleading default route="challenge" (founders) in create_agent | fixed | 03caba3 |
| S1 | secu | major | app/identity/service.py:131 | max_agents cap bypassed (TOCTOU count/insert, in-flight challenges unchecked at complete) in _ensure_open | fixed | 0eec58f |
| S2 | secu | minor | app/identity/service.py:100 | distinct INVALID_INVITE message leaks sponsor sanction status in _valid_invite | fixed | 0eec58f |

- P3 wontfix : table bornée par `max_agents` (+ bannis), endpoint rate-limité par IP ; un seq scan de ~1000 lignes est sub-milliseconde, un index partiel n'apporterait rien de mesurable (count(*) Postgres lit quand même la visibility map).
- P4 wontfix : la spec visibilité §7 garde `challenges.invite_code` (nullable) ; la supprimer est un changement de schéma hors périmètre de la Task 2.
- Perf tour 1 : aucun major (pas de N+1, Argon2 hors boucle d'événements via `to_thread`, ordre des contrôles correct) ; tour final, minors corrigés ou wontfix.
- Secu tour 1 : S1 major (3 inscriptions concurrentes sur 1 place → 3 comptes, reproduit) corrigé par recontrôle sous `pg_advisory_xact_lock` après Argon2 + contrôle dans `complete_registration` ; limites IP, consommation atomique du code (FOR UPDATE), parrain gelé/banni, absence de fuite de phrase vérifiés sans défaut. Pas tour final.
- Secu tour 2 : aucun nouveau finding. Correctif S1 vérifié : clé `REGISTRATION_LOCK_KEY` unique dans `app/`, `pg_advisory_xact_lock` pris après Argon2 (non tenu pendant la dérivation), en dernier après les verrous de ligne invite/défi (pas de cycle : `create_invite` et la modération ne prennent pas ce verrou, la FK `sponsor_id` ne pose qu'un KEY SHARE compatible), libéré au commit ou à la fermeture de session (rollback) sur toute erreur ; contention bornée par `argon2_concurrency`. Limites IP sur les deux voies (REST et MCP, avant toute dérivation), ordre des contrôles sans oracle (plafond avant code, code avant pseudo), phrase absente de l'outbox. Tour final : aucun minor secu open.
