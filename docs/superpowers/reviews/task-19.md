# Registre des findings — Task 19 (branche feat/deploy)

Base: 3aa2664

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | .github/workflows/ci.yml:70 | build arm64 sous QEMU sans cache de couches (cache-from/to) in build-push-action | fixed | bfe5805 |
| P2 | perf | major | deploy/deploy.sh:30 | images précédentes jamais supprimées (disque du Pi) in deploy.sh | fixed | bfe5805 |
| P3 | perf | minor | deploy/docker-compose.yml:app | CMD uvicorn et `user: 10001:33` dupliqués Dockerfile/compose in app.command | wontfix |  |
| P4 | perf | minor | deploy/docker-compose.yml + systemd/sancturai.slice | somme des limites (512m+1g+relay 128M = 1,64G) > MemoryMax=1.5G in sancturai.slice (plan-mandated) | fixed | FIX |
| P5 | perf | minor | deploy/deploy.sh:8 | `.env` relu par grep alors que compose le lit déjà, échec cryptique si absent in deploy.sh | fixed | FIX |
| P6 | perf | minor | deploy/nginx/sancturai.conf:upstream | pas de keepalive sur l'upstream sancturai_app in nginx | wontfix |  |
| P7 | perf | minor | deploy/nginx/sancturai.conf:44 | commentaire client_max_body_size verbeux avec référence de revue, blocs @sancturai_* répétitifs in nginx | fixed | FIX |

Notes round 2 (0 nouveau majeur, tour final) :
- P3 wontfix : le CMD du Dockerfile est la valeur par défaut sûre de l'image ; l'égalité avec la commande compose est garantie par test_dockerfile_matches_compose_and_ci.
- P4 : décidé par l'orchestrateur (délégation utilisateur) : slice MemoryMax=1.5G conservé, mem_limit app abaissé à 896m (512m + 896m + 128M = 1,5G), commentaire dans le compose + note setup.md, test mis à jour.
- P6 wontfix : connexion unix locale (coût négligeable) ; un keepalive upstream risque des 502 sur POST (course avec le keep-alive d'uvicorn).
- P7 : commentaire raccourci ; wontfix pour les blocs @sancturai_* (trois corps JSON distincts, factoriser n'apporte rien).
