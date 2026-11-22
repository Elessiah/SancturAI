# Registre des findings — Visibilité Task 8

Base: 14eae47

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/discovery.py:134, README.md:95 | public guide/README claim a frozen agent can only call me/logout/trial_read/trial_defense, omitting login (allowed, required to follow the trial) in _TEMPLATE | fixed | b27d0da |
| P2 | perf | major | docs/ops/mcp-registry.md:9 | launch check `curl -sI /charter` returns 405 (GET-only route, no HEAD) in launch order | fixed | b27d0da |
| P3 | perf | major | docs/ops/mcp-registry.md:7 | launch order tells admin to apply migrations after deploy.sh, which already runs alembic upgrade head (and needs sudo) in launch order | fixed | b27d0da |
| P4 | perf | minor | app/status.py:18 | failed compute not cached: every waiter queued on the lock retries the count query in turn during a DB outage in StatusCache.get | open | |
| P5 | perf | minor | app/discovery.py:250 | charter read lazily (blocking file I/O on the event loop at first request, missing file only detected then) in charter_text | open | |
| P6 | perf | minor | app/discovery.py:204 | ~10 KB guide re-formatted on every /, /llms.txt and MCP help call although it depends only on settings in help_text | open | |
| P7 | perf | minor | tests/test_mcp.py:62 | three blank lines between test functions in test_tools_listed | open | |

Revu sans finding (perf round 1) : StatusCache sous asyncio.Lock (un seul calcul à l'expiration et sous concurrence, TTL monotone 60 s vérifié, clear() au reset) ; count_members = une requête count, partagée avec le plafond d'inscription ; état disque via DiskGuard (sonde mise en cache) ; outils MCP status/charter délégués à current_status/charter_text (pas de doublon avec REST) ; robots.txt constant ; server.json et version à source unique testés ; textes guide/charte/README vérifiés contre le code (probation, seuils tribunal, refus agent_report, purge au bannissement, INVALID_INVITE sans repli, noms des 33 outils).
Perf round 1: 3 majors (P1-P3) fixed; minors left open for the next round.
