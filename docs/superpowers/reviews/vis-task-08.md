# Registre des findings — Visibilité Task 8

Base: 14eae47

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/discovery.py:134, README.md:95 | public guide/README claim a frozen agent can only call me/logout/trial_read/trial_defense, omitting login (allowed, required to follow the trial) in _TEMPLATE | fixed | b27d0da |
| P2 | perf | major | docs/ops/mcp-registry.md:9 | launch check `curl -sI /charter` returns 405 (GET-only route, no HEAD) in launch order | fixed | b27d0da |
| P3 | perf | major | docs/ops/mcp-registry.md:7 | launch order tells admin to apply migrations after deploy.sh, which already runs alembic upgrade head (and needs sudo) in launch order | fixed | b27d0da |
| P4 | perf | minor | app/status.py:18 | failed compute not cached: every waiter queued on the lock retries the count query in turn during a DB outage in StatusCache.get | fixed | 5429dba |
| P5 | perf | minor | app/discovery.py:250 | charter read lazily (blocking file I/O on the event loop at first request, missing file only detected then) in charter_text | fixed | 5429dba |
| P6 | perf | minor | app/discovery.py:204 | ~10 KB guide re-formatted on every /, /llms.txt and MCP help call although it depends only on settings in help_text | fixed | 5429dba |
| P7 | perf | minor | tests/test_mcp.py:62 | three blank lines between test functions in test_tools_listed | fixed | 5429dba |
| P8 | perf | major | app/forum/service.py:195, app/discovery.py:141, README.md:101 | guide/README say replies in a trial thread return TRIAL_CLOSED once the deadline passed, but only status was checked (replies accepted until the periodic close, up to 60 s) in _check_tribunal_reply | fixed | bbb2995 |
| P9 | perf | major | app/discovery.py:113 | guide says an agent report settles as success on ban / failure on reactivation, but trials closed by the admin (ban or unfreeze) leave reports neutral, and admin unfreeze grants no protection in _TEMPLATE | fixed | bbb2995 |
| P10 | perf | minor | docs/ops/mcp-registry.md:11 | literal newline instead of backslash-n inside the `curl -w` format splits the inline code span in launch order | fixed | 5429dba |
| P11 | perf | major | app/identity/api.py:57, app/mcp_server.py:317 | frozen accused cannot learn its trial id (notifications and trial_list return FROZEN, me exposes only frozen), so cannot defend (spec §4.4) in me — décidé par l'orchestrateur (délégation utilisateur) | fixed | 5429dba |

Revu sans finding (perf round 1) : StatusCache sous asyncio.Lock (un seul calcul à l'expiration et sous concurrence, TTL monotone 60 s vérifié, clear() au reset) ; count_members = une requête count, partagée avec le plafond d'inscription ; état disque via DiskGuard (sonde mise en cache) ; outils MCP status/charter délégués à current_status/charter_text (pas de doublon avec REST) ; robots.txt constant ; server.json et version à source unique testés ; textes guide/charte/README vérifiés contre le code (probation, seuils tribunal, refus agent_report, purge au bannissement, INVALID_INVITE sans repli, noms des 33 outils).
Perf round 1: 3 majors (P1-P3) fixed; minors left open for the next round.

Revu sans finding (perf round 2) : routes REST et 33 outils MCP cités dans le guide existent avec les bons verbes ; codes de refus du signalement d'agent, de la probation, de l'invitation et du plafond d'inscription conformes au code ; purge au bannissement (vault, posts, invitations non utilisées, votes, signalements en cours) ; tally sans votants gelés ; ordre de lancement et deploy.sh (sauvegarde puis alembic) ; server.json.
Hors focale (à arbitrer par l'orchestrateur, spec §4.4) : un accusé gelé ne peut pas apprendre l'id de son procès (forum_notifications, qui porte l'avis « procès #N », et trial_list renvoient FROZEN ; me n'expose que frozen: true) ; seul un parcours des ids via trial_read le retrouve.
Perf round 2: 2 majors (P8, P9) fixed; minors P4-P7, P10 left open for the next round.

Revu sans finding (perf round 3) : affirmations du guide, de CHARTER.md, du README et de docs/ops/mcp-registry.md revérifiées contre le code (migrations 0008-0010, CLI ban/unfreeze, notifications Discord inscription/ouverture/clôture, count_members hors bannis et système, quotas de probation 5 Mo / 1 000 fichiers) ; trial_id de me : une requête indexée (uq_trials_open_accused) seulement pour un agent gelé.
Perf round 3 (final) : aucun nouveau major ; P11 imposé et minors P4-P7, P10 fixed.
