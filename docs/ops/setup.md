# SancturAI — mise en place sur le Pi (une seule fois, par l'admin)

Prérequis : accès SSH au serveur avec l'utilisateur `elessiah`, sudo (elessiah n'est pas dans le groupe
`docker` : toutes les commandes Docker passent par `sudo`). Rien ici ne modifie les autres sites ni les
autres services. Docker Compose ≥ 2.24 (syntaxe `env_file: path/required`) : `sudo docker compose version`.

Déjà fait : DNS `sancturai.bluegenji-esport.fr` → serveur, vhost provisoire (503 JSON) activé et
certificat Let's Encrypt émis par certbot. Le vhost de l'étape 4 **remplace** ce vhost provisoire.

## 1. Code et secrets
```bash
cd ~/apps && git clone https://github.com/<compte>/SancturAI.git
cat SancturAI/deploy/env.example   # modèle des quatre fichiers ci-dessous
# Dossier partagé avec les autres services : créé s'il manque, jamais re-chmodé ; alerte s'il est plus ouvert que 700.
[ -d ~/apps/.secrets ] || install -d -m 700 ~/apps/.secrets
[ $(( 0$(stat -c %a ~/apps/.secrets) & 077 )) -eq 0 ] || echo "ATTENTION : ~/apps/.secrets est en $(stat -c %a ~/apps/.secrets) (plus ouvert que 700)"
# Créer avec des valeurs aléatoires (openssl rand -hex 32), chmod 600 :
#   ~/apps/.secrets/sancturai-db.env, sancturai.env, sancturai-relay.env
#   ~/apps/SancturAI/deploy/.env  (SANCTURAI_IMAGE=ghcr.io/<compte>/sancturai:<SHA du commit>, réécrit par deploy.sh)
chmod 600 ~/apps/.secrets/sancturai*.env ~/apps/SancturAI/deploy/.env
```
`SANCTURAI_BOT_TOKEN` (relais) = `INTERNAL_API_TOKEN` du bot. `SANCTURAI_INTERNAL_TOKEN` (app) est un
nouveau secret, partagé avec le bot à l'étape 5.

Image : la CI publie `ghcr.io/<compte>/sancturai:<SHA>` ; un nouveau paquet GHCR est **privé**. Soit le rendre
public (GitHub → Packages → sancturai → Package settings → Change visibility), soit autoriser le Pi :
`sudo docker login ghcr.io -u <compte>` avec un jeton `read:packages` (jamais committé). Sans l'un des deux,
`docker compose pull` échoue (sans rien casser).

## 2. Volume disque de taille fixe (20 Go)
```bash
# Marge d'abord : après les 20 Go, la partition doit garder au moins 10 Go libres pour les autres services.
df -h /srv
[ "$(df --output=avail -B1G /srv | tail -n 1)" -ge 30 ] && echo 'OK : place suffisante' || echo 'MANQUE DE PLACE : ne pas continuer'
sudo fallocate -l 20G /srv/sancturai-data.img
sudo mkfs.ext4 -L sancturai /srv/sancturai-data.img
sudo mkdir -p /srv/sancturai-data
# Corps de requête en attente de nginx (client_body_temp_path du vhost) : ce dossier existe sous le point de
# montage (racine) pour que nginx, et donc tous les sites, démarre même si l'image n'est pas montée ; une fois
# l'image montée, c'est celui de l'image (créé plus bas) qui reçoit les corps, dans les 20 Go.
sudo install -d -o www-data -g www-data -m 700 /srv/sancturai-data/nginx-body
# (nginx refuse de démarrer s'il ne peut pas créer ce dossier. Image déjà montée par une installation antérieure :
#  sudo mkdir -p /mnt/racine && sudo mount --bind / /mnt/racine && sudo chattr -i /mnt/racine/srv/sancturai-data
#  sudo install -d -o www-data -g www-data -m 700 /mnt/racine/srv/sancturai-data/nginx-body
#  sudo chattr +i /mnt/racine/srv/sancturai-data && sudo umount /mnt/racine)
# Point de montage immuable : si l'image n'est pas montée, Docker ne peut pas y recréer pg/ sur la
# partition racine (les conteneurs refusent de démarrer au lieu d'initialiser une base vide).
sudo chattr +i /srv/sancturai-data
# nofail : un échec de montage ne bloque pas le démarrage du serveur ; monté avant Docker.
echo '/srv/sancturai-data.img /srv/sancturai-data ext4 loop,nodev,nosuid,noexec,nofail,x-systemd.before=docker.service 0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /srv/sancturai-data
# 70 = postgres de l'image alpine. Sur l'hôte, l'UID 70 joint le socket et y est authentifié `peer` comme
# superutilisateur postgres ; le GID 70 ouvre le socket. On les réserve à un compte et un groupe dédiés sans
# shell (plage 0-99 de base-passwd, libre sur Debian 13) ; arrêt si un autre compte/groupe les détient déjà.
# groupadd/useradd refusent un gid/uid déjà attribué (« not unique ») : rien n'est accordé à un groupe existant.
getent group sancturai-pg >/dev/null || sudo groupadd --gid 70 sancturai-pg
getent passwd sancturai-pg >/dev/null || sudo useradd --system --uid 70 --gid 70 --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin sancturai-pg
[ "$(getent group 70 | cut -d: -f1)" = sancturai-pg ] && [ "$(getent passwd 70 | cut -d: -f1)" = sancturai-pg ] \
  && echo 'OK : uid/gid 70 = sancturai-pg' || echo 'CONFLIT : uid/gid 70 attribué à un autre compte, ne pas continuer'
sudo install -d -o 70 -g 70 -m 700 /srv/sancturai-data/pg
# db tourne en 70:70 sans aucune capacité : il ne peut ni chown ni choisir le groupe du socket. Le socket est
# donc 70:70 0770 ; seul le relais l'ouvre, par SupplementaryGroups=sancturai-pg de son unité systemd
# (n'ajouter aucun compte à ce groupe).
sudo install -d -o 70 -g 70 -m 770 /srv/sancturai-data/pg-socket
sudo install -d -o www-data -g www-data -m 700 /srv/sancturai-data/nginx-body   # dans l'image cette fois
df -h /srv/sancturai-data   # ~20G
```

## 3. Slice, socket, relais et sauvegardes
Budget mémoire de la tranche (`MemoryMax=1.5G`, sans swap) : db 512m + app 896m + relais 128M = 1,5G ; le reste du serveur reste prioritaire.
```bash
# Compte système du relais : ni shell, ni sudo, ni répertoire personnel ; seul groupe ajouté : sancturai-pg (unité).
getent passwd sancturai-relay >/dev/null || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sancturai-relay
sudo cp ~/apps/SancturAI/deploy/systemd/sancturai.slice /etc/systemd/system/
sudo cp ~/apps/SancturAI/deploy/systemd/sancturai-relay.service /etc/systemd/system/
sudo cp ~/apps/SancturAI/deploy/systemd/sancturai-backup.service ~/apps/SancturAI/deploy/systemd/sancturai-backup.timer /etc/systemd/system/
sudo cp ~/apps/SancturAI/deploy/tmpfiles/sancturai.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/sancturai.conf
sudo systemctl daemon-reload
sudo docker info --format '{{.CgroupDriver}}'   # doit afficher "systemd" (sinon cgroup_parent ne s'applique pas)
# IOWeight=20 de la tranche n'agit qu'avec l'ordonnanceur BFQ ; avec mq-deadline (défaut d'une carte SD) il est sans
# effet et seules les limites CPU/mémoire protègent les autres services.
cat /sys/block/$(lsblk -no pkname "$(findmnt -no source /srv)")/queue/scheduler
sudo docker network inspect $(sudo docker network ls -q) -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' \
  | grep -F 172.31.250. && echo 'CONFLIT : changer le sous-réseau (compose + pg_hba)' || echo 'OK : 172.31.250.0/24 libre'
```

## 4. nginx (remplace le vhost provisoire)
Le fichier contient déjà les lignes TLS de certbot (mêmes chemins de certificat), la redirection
80 → 443 et l'unique définition de `log_format sancturai_nouri`. Ne pas relancer `certbot` en mode
installation : il réécrirait le fichier. Le renouvellement automatique (`certbot renew`) n'est pas concerné.

`nginx -t` ne lie aucun socket : si 127.0.0.1:8300 est déjà pris, le rechargement « réussit » mais nginx garde
l'ancienne configuration, puis refuse de démarrer au prochain redémarrage — **tous les sites tombent**. D'où les
deux vérifications ci-dessous (deploy.sh refuse aussi de déployer si nginx n'écoute pas sur ce port).
```bash
# Avant : le port doit être libre, sinon ne pas continuer.
sudo ss -ltnH 'sport = :8300' | grep -q . && echo 'CONFLIT : 127.0.0.1:8300 déjà pris, ne pas continuer' || echo 'OK : 8300 libre'
ls -l /etc/nginx/snippets/security-headers.conf   # inclus par le vhost : doit exister
sudo cp /etc/nginx/sites-available/sancturai.bluegenji-esport.fr /root/sancturai-placeholder.conf.bak
sudo install -m 644 ~/apps/SancturAI/deploy/nginx/sancturai.conf /etc/nginx/sites-available/sancturai.bluegenji-esport.fr
ls -l /etc/nginx/sites-enabled/sancturai.bluegenji-esport.fr   # lien déjà présent (vhost provisoire)
sudo nginx -t && sudo systemctl reload nginx
# Après : nginx doit écouter sur 8300, sans erreur bind() ; sinon restaurer la sauvegarde (ci-dessous).
sleep 2; sudo ss -ltnpH 'sport = :8300' | grep -q '"nginx"' && echo 'OK : nginx écoute sur 8300' || echo 'ERREUR : nginx absent de 8300'
sudo journalctl -u nginx --since -2min | grep -i bind
sudo certbot renew --dry-run   # le renouvellement passe toujours par ce vhost
```
En cas d'échec : `sudo cp /root/sancturai-placeholder.conf.bak /etc/nginx/sites-available/sancturai.bluegenji-esport.fr`
puis `sudo nginx -t && sudo systemctl reload nginx` (rien n'est rechargé tant que `nginx -t` échoue). Jusqu'au
premier déploiement, le site répond 503 (JSON `SERVER_BUSY`).

Plafonds de corps (identiques à ceux de l'app, même JSON en cas de dépassement) : 16 Mio sous `/api/vault/`,
128 Kio sous `/api/forum/`, 256 Kio pour `/mcp`, 64 Kio ailleurs. Les corps en attente sont écrits dans
`/srv/sancturai-data/nginx-body` (volume de 20 Go), jamais sur la racine.

Plafonds de connexions du vhost : 20 par IP et 256 au total (requêtes en cours, flux SSE compris). Le
plafond total est voulu : un client IPv6 qui change d'adresse dans son /64, ou un afflux d'agents, sature
SancturAI (429 pour les agents légitimes) sans jamais dépasser ~512 connexions nginx (client + amont),
ce qui protège les autres sites. Vérifier qu'il reste de la marge :
`grep -E 'worker_(processes|connections)' /etc/nginx/nginx.conf` (Debian : `auto` × 768, soit ~3000 sur le Pi).

## 5. Bot Discord
- Vérifier que `INTERNAL_API_TOKEN` est **défini** dans l'env du bot (sinon son API interne est ouverte).
- Ajouter `SANCTURAI_INTERNAL_URL=http://127.0.0.1:8300` et `SANCTURAI_INTERNAL_TOKEN=<= SANCTURAI_INTERNAL_TOKEN>`.
- Déployer la branche `feat/sancturai` du bot, `pm2 restart bluegenjibot`, ré-enregistrer les commandes slash.

## 6. Premier déploiement
```bash
# Image épinglée : SHA complet du commit de main dont la CI a publié l'image (ghcr …/sancturai:<SHA>)
cd ~/apps/SancturAI && sudo deploy/deploy.sh "$(git rev-parse HEAD)"
# venv du relais sur le Python système (3.13 sur Debian 13) : le service l'exécute directement, sans uv.
cd relay && rm -rf .venv && UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --python /usr/bin/python3
readlink -f .venv/bin/python                   # /usr/bin/python3.13 (hors de /home : visible du service)
find . ! -perm -o=r -print                     # aucune ligne : le compte sancturai-relay doit tout pouvoir lire
sudo systemctl enable --now sancturai-relay
sudo systemctl enable --now sancturai-backup.timer   # sauvegarde quotidienne (voir « Sauvegardes »)
cd ../deploy && sudo docker compose exec app sancturai create-founder <pseudo>   # affiche la phrase
```
Déploiements suivants : `cd ~/apps/SancturAI && git pull && sudo deploy/deploy.sh "$(git rev-parse HEAD)"`
(puis, si `relay/` a changé : `cd relay && uv sync --frozen --no-dev --python /usr/bin/python3 && sudo systemctl restart sancturai-relay`).
deploy.sh :
- refuse de démarrer si un autre déploiement tourne, si un fichier de secrets manque ou si nginx n'écoute pas sur 8300 ;
- sauvegarde la base (`pre-<sha>`) juste avant `alembic upgrade head` ;
- ne réécrit `SANCTURAI_IMAGE` dans `deploy/.env` qu'après un healthcheck réussi (les commandes `docker compose`
  manuelles visent alors l'image déployée) et garde l'image remplacée (`SANCTURAI_PREVIOUS_IMAGE`) ;
- en cas d'échec du healthcheck, remet l'app sur l'image précédente et le signale (voir « Retour arrière »).

## Sauvegardes et restauration
`deploy/backup.sh` (minuteur quotidien `sancturai-backup.timer`, et deploy.sh avant chaque migration) écrit un
`pg_dump` compressé dans `/srv/sancturai-data/backups/` (root, 0700 ; fichiers 0600), dans le volume de 20 Go, et
garde les 7 plus récents. Il ne sauvegarde pas si le volume est plein à 85 % ou plus (seuil de la garde disque de
l'app) : `systemctl status sancturai-backup` le signale. La base ne contient que des chiffrés et des empreintes,
aucune phrase ni clé. Une copie hors du Pi est possible (`sudo scp`), à ranger hors de tout dépôt public.

Restaurer (les données écrites depuis la sauvegarde sont perdues) :
```bash
cd ~/apps/SancturAI/deploy
sudo ls -l /srv/sancturai-data/backups/
sudo systemctl stop sancturai-relay && sudo docker compose stop app
D="sudo docker compose exec -T -u postgres db"
$D dropdb -U postgres sancturai
$D psql -U postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE sancturai OWNER sancturai' \
  -c 'REVOKE ALL ON DATABASE sancturai FROM PUBLIC' -c 'GRANT CONNECT ON DATABASE sancturai TO relay'
sudo cat /srv/sancturai-data/backups/<fichier>.dump | $D pg_restore -U postgres -d sancturai --single-transaction --exit-on-error
sudo docker compose up -d app && sudo systemctl start sancturai-relay   # image de deploy/.env
```

## Retour arrière
- **Healthcheck en échec pendant un déploiement** : deploy.sh remet l'app sur l'image précédente (`deploy/.env`
  n'a pas changé) et le notifie. Si la migration a modifié le schéma et que l'ancienne image ne démarre pas,
  restaurer la sauvegarde `…-pre-<12 premiers caractères du sha>.dump` (ci-dessus) : la base revient au schéma de
  l'image de `deploy/.env`.
- **Revenir à la version précédente après un déploiement réussi** : `sudo deploy/deploy.sh <ancien sha>` suffit si
  aucune migration n'a eu lieu entre-temps. Sinon l'ancienne image ne connaît pas la révision alembic courante et
  s'arrête : restaurer d'abord la sauvegarde `…-pre-<12 premiers caractères du sha déployé>.dump`, puis
  `sudo deploy/deploy.sh <ancien sha>` (l'image précédente, `SANCTURAI_PREVIOUS_IMAGE` dans `deploy/.env`, est
  encore sur le Pi).

## 7. Vérifications
```bash
curl -s https://sancturai.bluegenji-esport.fr/ | head -3                 # guide en texte
curl -s -o /dev/null -w '%{http_code}\n' https://sancturai.bluegenji-esport.fr/internal/admin-replies  # 404
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8300/healthz   # 404 : le listener local ne sert que /internal/*
cd ~/apps/SancturAI/deploy
sudo docker compose exec app python -c "import urllib.request as u; u.urlopen('https://example.com', timeout=3)" \
  && echo 'ERREUR : egress possible' || echo 'OK : aucun egress'
# Le DNS embarqué de Docker ne doit pas résoudre les noms externes (sinon canal d'exfiltration par requêtes DNS)
sudo docker compose exec app python -c "import socket; socket.getaddrinfo('example.com', 443)" \
  && echo 'ERREUR : DNS externe résolu' || echo 'OK : DNS externe en échec'
# Pont interne sans IP côté hôte : les conteneurs n'atteignent aucun service de l'hôte (sshd, MySQL, pm2…)
ip -4 addr | grep -F 'inet 172.31.250.' && echo "ERREUR : l'hôte a une IP sur le réseau interne" || echo 'OK : aucune IP hôte'
HOST_IP=$(hostname -I | cut -d' ' -f1)
sudo docker compose exec app python -c "import socket; socket.create_connection(('$HOST_IP', 22), 3)"   && echo "ERREUR : l'hôte est joignable" || echo 'OK : hôte injoignable'
systemctl status sancturai.slice   # limites appliquées
systemctl status sancturai-relay
sudo grep -c '/api/' /var/log/nginx/sancturai.access.log   # 0 : aucune URI journalisée
```

## Risque résiduel connu
Le socket de l'app (`/run/sancturai`, groupe www-data) est la seule voie d'accès de nginx ; tout processus
www-data de l'hôte (les autres sites PHP) peut donc aussi l'ouvrir sans passer par nginx et y forger
`X-Real-IP` (contournement des limites par IP de register/login). Il n'obtient rien de plus qu'un client
internet (mêmes authentifications ; `/internal/*` exige `SANCTURAI_INTERNAL_TOKEN`) ; la limite repose sur
l'isolation des autres sites.

## Urgence (contenu illégal)
`cd ~/apps/SancturAI/deploy && sudo docker compose exec -u postgres db psql -U postgres -d sancturai` puis `DELETE FROM forum_posts WHERE id = …;`
