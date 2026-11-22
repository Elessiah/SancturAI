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
| S6 | secu | major | deploy/docker-compose.yml:db | db démarre en root avec CHOWN/DAC_OVERRIDE/FOWNER/SETGID/SETUID alors que §4.2 exige un utilisateur non-root (plan-mandated) in db.cap_add | fixed | 52f7e1f |
| S7 | secu | minor | deploy/deploy.sh:30 | relay a UPDATE sur toutes les colonnes d'outbox (payload, kind) au lieu de `UPDATE (sent_at, attempts, next_attempt_at)` (plan-mandated) in deploy.sh GRANT | fixed | 6104ad0 |
| S8 | secu | minor | docs/ops/setup.md:14 | `install -d -m 700 ~/apps/.secrets` re-chmode un dossier de secrets existant partagé avec d'autres services in setup.md §1 | fixed | 6104ad0 |
| S9 | secu | minor | deploy/deploy.sh:20 | corps JSON construit par interpolation de `$1` (guillemets/backslash de l'image cassent ou injectent des champs) in notify | fixed | 6104ad0 |
| S10 | secu | minor | deploy/systemd/sancturai-relay.service | ProtectHome=read-only laisse le relais lire les autres secrets et clés SSH d'elessiah (InaccessiblePaths) in sancturai-relay.service | fixed | 6104ad0 |
| S11 | secu | minor | deploy/Dockerfile + docker-compose.yml | images de base (python:3.13-slim, postgres:17-alpine) et SANCTURAI_IMAGE `:latest` non épinglées par digest in images | fixed | 6104ad0 |
| S12 | secu | minor | deploy/tmpfiles/sancturai.conf | tout processus www-data de l'hôte (autres sites PHP) joint le socket de l'app sans nginx : X-Real-IP forgeable (contournement du rate-limit) in /run/sancturai | wontfix |  |
| S13 | secu | major | deploy/nginx/sancturai.conf:location / | plafond de connexions indexé sur l'adresse complète : un client IPv6 change d'adresse dans son /64 et épuise quand même worker_connections (S4 contourné) in limit_conn sancturai_conn | fixed | 52f7e1f |
| S14 | secu | minor | deploy/docker-compose.yml:networks | IPv6 du pont laissé au défaut du démon (pas de `enable_ipv6: false`) : un démon avec IPv6 par défaut redonnerait à l'hôte une passerelle v6 sur le pont (S1 en IPv6) in networks.internal | fixed | 6104ad0 |
| S15 | secu | minor | docs/ops/setup.md:§7 | la vérification d'egress ne teste pas que la résolution DNS externe échoue (le DNS embarqué des réseaux internal dépend de la version de Docker : canal d'exfiltration) in setup.md §7 | fixed | 6104ad0 |
| S16 | secu | minor | docs/ops/setup.md:§2 | uid/gid 70 de l'hôte non réservés : un compte hôte uid 70 est authentifié `peer` superutilisateur postgres sur le socket monté, un groupe tiers gid 70 ouvrirait le socket (libres sur Debian 13, plage base-passwd) in setup.md §2 / SupplementaryGroups=70 | fixed | 6104ad0 |

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

Notes secu round 2 (1 nouveau majeur : pas tour final, mineurs S7-S12, S14, S15 laissés open) :
- S6 : décidé par l'orchestrateur (délégation utilisateur) : db en `user: "70:70"`, plus aucun `cap_add`, read_only conservé. Vérifié en local (Docker 29.9) : init (rôles créés), redémarrage, CapEff=0. Découvert au test : sans CAP_FSETID, le chmod 3775 de l'entrypoint efface le setgid du répertoire pg-socket, le socket est donc 70:70 0770 et un compte de l'hôte hors groupe 70 est refusé (le relais n'aurait plus joint Postgres). Correctif : pg-socket créé en 70:70 0770 (setup.md) et `SupplementaryGroups=70` dans sancturai-relay.service ; vérifié : uid 1000 refusé sans le groupe 70, connexion relay OK avec, avant et après redémarrage.
- S13 : plafond global du vhost (`limit_conn_zone $server_name`, 256) en plus du plafond par IP ; `nginx -t` OK sur nginx:1.26.
- Confirmé : inhibit_ipv4 n'affecte pas les sockets Unix (nginx → /run/sancturai/app.sock, relais → pg-socket : montages, pas le réseau ; connexion au socket testée) ; dans un conteneur du réseau internal, eth0 n'a aucune adresse IPv6 (seul lo) et la résolution DNS externe échoue (Docker 29.9) ; limit_conn 20/IP laisse de la marge à un agent MCP (1 flux SSE + quelques POST).

Notes secu round 3 (1 nouveau mineur S16, 0 majeur : tour final ; mineurs décidés par l'orchestrateur (délégation utilisateur)) :
- S16 : uid et gid 70 libres sur Debian 13 (`getent` dans debian:trixie ; plage 0-99 réservée à base-passwd, jamais allouée par un paquet). setup.md §2 crée le groupe `sancturai-pg` (gid 70) et un compte système sans shell (uid 70) ; groupadd/useradd refusent un id déjà pris et une vérification finale affiche CONFLIT (testé : cas libre, rejeu, gid pris, uid pris). Le relais utilise `SupplementaryGroups=sancturai-pg`.
- S7 : décidé par l'orchestrateur (délégation utilisateur) : `REVOKE UPDATE ON outbox FROM relay; … GRANT SELECT, UPDATE (sent_at, attempts, next_attempt_at)` (le REVOKE retire aussi un droit global antérieur) ; nouveau test relay/tests exécutant l'instruction de deploy.sh contre un vrai Postgres : envoi, échec, 400, hors contrat OK avec le rôle relay, UPDATE de payload/kind refusé.
- S8 : décidé par l'orchestrateur (délégation utilisateur) : créé seulement s'il manque, jamais re-chmodé, alerte si mode & 077 (testé : absent, 750, 500).
- S9 : décidé par l'orchestrateur (délégation utilisateur) : corps sérialisé par `python3 -c json.dumps` (testé avec stubs docker/curl).
- S10 : décidé par l'orchestrateur (délégation utilisateur) : `ProtectHome=tmpfs` + `BindReadOnlyPaths` (uv, relay/, ~/.local/share/uv optionnel) + `BindPaths` (~/.cache/uv, relay/.venv) : le relais ne voit ni ~/.ssh, ni ~/apps/.secrets, ni le code/.env des autres apps ; EnvironmentFile reste lu par systemd. Vérifié dans un conteneur Debian 13 avec systemd : relais démarré (« database unavailable » attendu sans base), /home ne contient que uv, son cache et relay/, .venv inscriptible, relay/ en lecture seule, groupe sancturai-pg présent. Déplacer le relais hors de /home non retenu (uv et son Python vivent dans le home d'elessiah).
- S11 : décidé par l'orchestrateur (délégation utilisateur) : `deploy.sh <sha>` (40 hex validés) épingle `ghcr…/sancturai:<sha>` (étiquette déjà publiée par la CI), réécrit `SANCTURAI_IMAGE` dans deploy/.env après `up -d app` (commandes compose manuelles sur l'image déployée) et supprime l'image précédente par son étiquette ; Postgres épinglé par digest de l'index multi-arch (= 17.12-alpine3.24, pull vérifié). python:3.13-slim (Dockerfile) reste une étiquette : l'image est reconstruite à chaque push et doit suivre les correctifs de sécurité.
- S12 wontfix : décidé par l'orchestrateur (délégation utilisateur) : risque résiduel documenté dans setup.md ; un secret nginx → app touche le code de l'app (autre tâche) et ajoute un secret dans un vhost lisible ; un processus www-data n'obtient que les droits d'un client internet (`/internal/*` exige le token, §8).
- S14 : décidé par l'orchestrateur (délégation utilisateur) : `enable_ipv6: false` (réseau créé en local : EnableIPv6=false, inhibit_ipv4 conservé).
- S15 : décidé par l'orchestrateur (délégation utilisateur) : vérification `getaddrinfo('example.com')` doit échouer ajoutée à setup.md §7.
- limit_conn 256 : conservé comme plafond voulu (une saturation ne coûte que SancturAI, ~512 connexions nginx au plus contre ~3000 disponibles) ; documenté dans le vhost et setup.md §4 avec la commande de vérification de worker_connections.
