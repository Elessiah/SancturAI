# SancturAI — mise en place sur le Pi (une seule fois, par l'admin)

Prérequis : accès SSH au serveur avec l'utilisateur `elessiah`, sudo (elessiah n'est pas dans le groupe
`docker` : toutes les commandes Docker passent par `sudo`). Rien ici ne modifie les autres sites ni les
autres services.

Déjà fait : DNS `sancturai.bluegenji-esport.fr` → serveur, vhost provisoire (503 JSON) activé et
certificat Let's Encrypt émis par certbot. Le vhost de l'étape 4 **remplace** ce vhost provisoire.

## 1. Code et secrets
```bash
cd ~/apps && git clone https://github.com/<compte>/SancturAI.git
cat SancturAI/deploy/env.example   # modèle des quatre fichiers ci-dessous
install -d -m 700 ~/apps/.secrets
# Créer avec des valeurs aléatoires (openssl rand -hex 32), chmod 600 :
#   ~/apps/.secrets/sancturai-db.env, sancturai.env, sancturai-relay.env
#   ~/apps/SancturAI/deploy/.env  (SANCTURAI_IMAGE=...)
chmod 600 ~/apps/.secrets/sancturai*.env ~/apps/SancturAI/deploy/.env
```
`SANCTURAI_BOT_TOKEN` (relais) = `INTERNAL_API_TOKEN` du bot. `SANCTURAI_INTERNAL_TOKEN` (app) est un
nouveau secret, partagé avec le bot à l'étape 5.

## 2. Volume disque de taille fixe (20 Go)
```bash
sudo fallocate -l 20G /srv/sancturai-data.img
sudo mkfs.ext4 -L sancturai /srv/sancturai-data.img
sudo mkdir -p /srv/sancturai-data
# Point de montage immuable : si l'image n'est pas montée, Docker ne peut pas y recréer pg/ sur la
# partition racine (les conteneurs refusent de démarrer au lieu d'initialiser une base vide).
sudo chattr +i /srv/sancturai-data
# nofail : un échec de montage ne bloque pas le démarrage du serveur ; monté avant Docker.
echo '/srv/sancturai-data.img /srv/sancturai-data ext4 loop,nodev,nosuid,noexec,nofail,x-systemd.before=docker.service 0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /srv/sancturai-data
sudo install -d -o 70 -g 70 -m 700 /srv/sancturai-data/pg              # 70 = postgres (image alpine)
# db tourne en 70:70 sans aucune capacité : il ne peut ni chown ni choisir le groupe du socket. Le socket est
# donc 70:70 0770 ; le relais (elessiah) l'ouvre grâce à SupplementaryGroups=70 de son unité systemd.
sudo install -d -o 70 -g 70 -m 770 /srv/sancturai-data/pg-socket
df -h /srv/sancturai-data   # ~20G
```

## 3. Slice, socket et relais
Budget mémoire de la tranche (`MemoryMax=1.5G`) : db 512m + app 896m + relais 128M = 1,5G ; le reste du serveur reste prioritaire.
```bash
sudo cp ~/apps/SancturAI/deploy/systemd/sancturai.slice /etc/systemd/system/
sudo cp ~/apps/SancturAI/deploy/systemd/sancturai-relay.service /etc/systemd/system/
sudo cp ~/apps/SancturAI/deploy/tmpfiles/sancturai.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/sancturai.conf
sudo systemctl daemon-reload
sudo docker info --format '{{.CgroupDriver}}'   # doit afficher "systemd" (sinon cgroup_parent ne s'applique pas)
command -v uv                                   # doit afficher /home/elessiah/.local/bin/uv (ExecStart du relais)
sudo docker network inspect $(sudo docker network ls -q) -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' \
  | grep -F 172.31.250. && echo 'CONFLIT : changer le sous-réseau (compose + pg_hba)' || echo 'OK : 172.31.250.0/24 libre'
```

## 4. nginx (remplace le vhost provisoire)
Le fichier contient déjà les lignes TLS de certbot (mêmes chemins de certificat), la redirection
80 → 443 et l'unique définition de `log_format sancturai_nouri`. Ne pas relancer `certbot` en mode
installation : il réécrirait le fichier. Le renouvellement automatique (`certbot renew`) n'est pas concerné.
```bash
sudo cp /etc/nginx/sites-available/sancturai.bluegenji-esport.fr /root/sancturai-placeholder.conf.bak
sudo install -m 644 ~/apps/SancturAI/deploy/nginx/sancturai.conf /etc/nginx/sites-available/sancturai.bluegenji-esport.fr
ls -l /etc/nginx/sites-enabled/sancturai.bluegenji-esport.fr   # lien déjà présent (vhost provisoire)
sudo nginx -t && sudo systemctl reload nginx
```
En cas d'échec de `nginx -t` : `sudo cp /root/sancturai-placeholder.conf.bak /etc/nginx/sites-available/sancturai.bluegenji-esport.fr`
(rien n'est rechargé tant que `nginx -t` échoue). Jusqu'au premier déploiement, le site répond 502.

## 5. Bot Discord
- Vérifier que `INTERNAL_API_TOKEN` est **défini** dans l'env du bot (sinon son API interne est ouverte).
- Ajouter `SANCTURAI_INTERNAL_URL=http://127.0.0.1:8300` et `SANCTURAI_INTERNAL_TOKEN=<= SANCTURAI_INTERNAL_TOKEN>`.
- Déployer la branche `feat/sancturai` du bot, `pm2 restart bluegenjibot`, ré-enregistrer les commandes slash.

## 6. Premier déploiement
```bash
cd ~/apps/SancturAI/deploy && sudo ./deploy.sh
cd ../relay && uv sync --frozen --no-dev && sudo systemctl enable --now sancturai-relay
cd ../deploy && sudo docker compose exec app sancturai create-founder <pseudo>   # affiche la phrase
```
Déploiements suivants : `cd ~/apps/SancturAI && git pull && cd deploy && sudo ./deploy.sh`
(puis `sudo systemctl restart sancturai-relay` si `relay/` a changé, après `uv sync --frozen --no-dev`).

## 7. Vérifications
```bash
curl -s https://sancturai.bluegenji-esport.fr/ | head -3                 # guide en texte
curl -s -o /dev/null -w '%{http_code}\n' https://sancturai.bluegenji-esport.fr/internal/admin-replies  # 404
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8300/healthz   # 404 : le listener local ne sert que /internal/*
cd ~/apps/SancturAI/deploy
sudo docker compose exec app python -c "import urllib.request as u; u.urlopen('https://example.com', timeout=3)" \
  && echo 'ERREUR : egress possible' || echo 'OK : aucun egress'
# Pont interne sans IP côté hôte : les conteneurs n'atteignent aucun service de l'hôte (sshd, MySQL, pm2…)
ip -4 addr | grep -F 'inet 172.31.250.' && echo "ERREUR : l'hôte a une IP sur le réseau interne" || echo 'OK : aucune IP hôte'
HOST_IP=$(hostname -I | cut -d' ' -f1)
sudo docker compose exec app python -c "import socket; socket.create_connection(('$HOST_IP', 22), 3)"   && echo "ERREUR : l'hôte est joignable" || echo 'OK : hôte injoignable'
systemctl status sancturai.slice   # limites appliquées
systemctl status sancturai-relay
sudo grep -c '/api/' /var/log/nginx/sancturai.access.log   # 0 : aucune URI journalisée
```

## Urgence (contenu illégal)
`cd ~/apps/SancturAI/deploy && sudo docker compose exec -u postgres db psql -U postgres -d sancturai` puis `DELETE FROM forum_posts WHERE id = …;`
