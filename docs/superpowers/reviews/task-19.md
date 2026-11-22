# Registre des findings — Task 19 (branche feat/deploy)

Base: 3aa2664

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | .github/workflows/ci.yml:70 | build arm64 sous QEMU sans cache de couches (cache-from/to) in build-push-action | fixed | |
| P2 | perf | major | deploy/deploy.sh:30 | images précédentes jamais supprimées (disque du Pi) in deploy.sh | fixed | |
| P3 | perf | minor | deploy/docker-compose.yml:app | CMD uvicorn et `user: 10001:33` dupliqués Dockerfile/compose in app.command | open | |
| P4 | perf | minor | deploy/docker-compose.yml + systemd/sancturai.slice | somme des limites (512m+1g+relay 128M = 1,64G) > MemoryMax=1.5G in sancturai.slice (plan-mandated) | open | |
| P5 | perf | minor | deploy/deploy.sh:8 | `.env` relu par grep alors que compose le lit déjà, échec cryptique si absent in deploy.sh | open | |
| P6 | perf | minor | deploy/nginx/sancturai.conf:upstream | pas de keepalive sur l'upstream sancturai_app in nginx | open | |
| P7 | perf | minor | deploy/nginx/sancturai.conf:44 | commentaire client_max_body_size verbeux avec référence de revue, blocs @sancturai_* répétitifs in nginx | open | |
