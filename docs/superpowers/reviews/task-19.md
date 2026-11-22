# Registre des findings — Task 19 (branche feat/deploy)

Base: 3aa2664

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | .github/workflows/ci.yml:70 | build arm64 sous QEMU sans cache de couches (cache-from/to) in build-push-action | fixed | bfe5805 |
| P2 | perf | major | deploy/deploy.sh:30 | images précédentes jamais supprimées (disque du Pi) in deploy.sh | fixed | bfe5805 |
| P3 | perf | minor | deploy/docker-compose.yml:app | CMD uvicorn et `user: 10001:33` dupliqués Dockerfile/compose in app.command | wontfix |  |
| P4 | perf | minor | deploy/docker-compose.yml + systemd/sancturai.slice | somme des limites (512m+1g+relay 128M = 1,64G) > MemoryMax=1.5G in sancturai.slice (plan-mandated) | fixed | 12d68c8 |
| P5 | perf | minor | deploy/deploy.sh:8 | `.env` relu par grep alors que compose le lit déjà, échec cryptique si absent in deploy.sh | fixed | 12d68c8 |
| P6 | perf | minor | deploy/nginx/sancturai.conf:upstream | pas de keepalive sur l'upstream sancturai_app in nginx | wontfix |  |
| P7 | perf | minor | deploy/nginx/sancturai.conf:44 | commentaire client_max_body_size verbeux avec référence de revue, blocs @sancturai_* répétitifs in nginx | fixed | 12d68c8 |
| S1 | secu | major | deploy/docker-compose.yml:networks | l'hôte garde une IP (passerelle) sur le pont `internal` : les conteneurs joignent tous ses services en 0.0.0.0 (vérifié Docker 29) in networks.internal | fixed | b26ca7f |
| S2 | secu | major | deploy/postgres/init/01-roles.sh:8 | mots de passe des rôles passés en argument psql (`-v db_pw=…`), lisibles par tout compte de l'hôte via ps in 01-roles.sh | fixed | b26ca7f |
| S3 | secu | major | deploy/systemd/sancturai-relay.service | relais non confiné réseau (pas d'IPAddressDeny/Allow, RestrictAddressFamilies, CapabilityBoundingSet, Protect*) alors qu'il ne doit parler qu'au bot (§4.1) in sancturai-relay.service | fixed | b26ca7f |
| S4 | secu | major | deploy/nginx/sancturai.conf:location / | aucun plafond de connexions par IP : flux SSE de 300 s, une IP épuise worker_connections partagé avec les autres sites in nginx location / | fixed | b26ca7f |
| S5 | secu | major | deploy/docker-compose.yml:db | conteneur db sans `read_only` (§4.2) in db | fixed | b26ca7f |
| S6 | secu | major | deploy/docker-compose.yml:db | db démarre en root avec CHOWN/DAC_OVERRIDE/FOWNER/SETGID/SETUID alors que §4.2 exige un utilisateur non-root (plan-mandated) in db.cap_add | open |  |
| S7 | secu | minor | deploy/deploy.sh:30 | relay a UPDATE sur toutes les colonnes d'outbox (payload, kind) au lieu de `UPDATE (sent_at, attempts, next_attempt_at)` (plan-mandated) in deploy.sh GRANT | open |  |
| S8 | secu | minor | docs/ops/setup.md:14 | `install -d -m 700 ~/apps/.secrets` re-chmode un dossier de secrets existant partagé avec d'autres services in setup.md §1 | open |  |
| S9 | secu | minor | deploy/deploy.sh:20 | corps JSON construit par interpolation de `$1` (guillemets/backslash de l'image cassent ou injectent des champs) in notify | open |  |
| S10 | secu | minor | deploy/systemd/sancturai-relay.service | ProtectHome=read-only laisse le relais lire les autres secrets et clés SSH d'elessiah (InaccessiblePaths) in sancturai-relay.service | open |  |
| S11 | secu | minor | deploy/Dockerfile + docker-compose.yml | images de base (python:3.13-slim, postgres:17-alpine) et SANCTURAI_IMAGE `:latest` non épinglées par digest in images | open |  |
| S12 | secu | minor | deploy/tmpfiles/sancturai.conf | tout processus www-data de l'hôte (autres sites PHP) joint le socket de l'app sans nginx : X-Real-IP forgeable (contournement du rate-limit) in /run/sancturai | open |  |

Notes round 2 (0 nouveau majeur, tour final) :
- P3 wontfix : le CMD du Dockerfile est la valeur par défaut sûre de l'image ; l'égalité avec la commande compose est garantie par test_dockerfile_matches_compose_and_ci.
- P4 : décidé par l'orchestrateur (délégation utilisateur) : slice MemoryMax=1.5G conservé, mem_limit app abaissé à 896m (512m + 896m + 128M = 1,5G), commentaire dans le compose + note setup.md, test mis à jour.
- P6 wontfix : connexion unix locale (coût négligeable) ; un keepalive upstream risque des 502 sur POST (course avec le keep-alive d'uvicorn).
- P7 : commentaire raccourci ; wontfix pour les blocs @sancturai_* (trois corps JSON distincts, factoriser n'apporte rien).

Notes secu round 1 :
- S1 : `com.docker.network.bridge.inhibit_ipv4: "true"` ; vérifié en local (Docker 29.9) : avant, un serveur de l'hôte sur 0.0.0.0 était joignable via 172.31.25x.1 ; après, hôte injoignable, app ↔ db OK, DNS externe en échec. Vérification ajoutée dans setup.md §7.
- S2 : `\getenv` dans le heredoc littéral (psql 17) ; init testée (mot de passe avec `'` et `"`), connexion sancturai OK.
- S5 : db `read_only` + tmpfs /tmp testé (init, redémarrage).
- S6 (décision utilisateur) : testé en local, `user: "70:70"` sans aucun `cap_add` + read_only fonctionne (setup.md crée déjà pg/ en 70:70 0700 et pg-socket en 70:<gid elessiah>).
