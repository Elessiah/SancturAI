#!/usr/bin/env bash
# Sauvegarde de la base : sudo deploy/backup.sh <étiquette>  (daily : minuteur systemd ; pre-<sha> : deploy.sh).
# pg_dump au format custom (compressé) dans le volume de 20 Go, jamais sur la racine partagée. La base ne contient
# que des chiffrés et des empreintes : aucune phrase ni clé n'est écrite. Les 7 sauvegardes les plus récentes sont
# gardées. Restauration : setup.md, « Sauvegardes et restauration ».
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
  echo "lancer avec sudo : sudo deploy/backup.sh <étiquette>" >&2
  exit 1
fi
label=${1:-}
[[ "${label}" =~ ^[a-z0-9-]{1,64}$ ]] || { echo "usage : sudo deploy/backup.sh <étiquette [a-z0-9-]>" >&2; exit 1; }
data=/srv/sancturai-data
dir=${data}/backups
# Même seuil que la garde disque de l'app (disk_busy_ratio = 0.85) : au-delà, pas de sauvegarde.
max_used_pct=85

# Le minuteur attend la fin d'un déploiement en cours (deploy.sh tient ce verrou et appelle ce script lui-même).
if [ "${label}" = daily ]; then
  exec 9>/run/lock/sancturai-deploy.lock
  flock -w 1800 9 || { echo "déploiement en cours depuis 30 min : sauvegarde reportée" >&2; exit 1; }
fi

mountpoint -q "${data}" || { echo "AVERTISSEMENT : ${data} non monté, sauvegarde ignorée" >&2; exit 2; }
used=$(df --output=pcent "${data}" | tail -n 1 | tr -dc '0-9')
if [ "${used}" -ge "${max_used_pct}" ]; then
  echo "AVERTISSEMENT : volume ${data} plein à ${used} % (seuil ${max_used_pct} %), sauvegarde ignorée" >&2
  exit 2
fi

umask 077
install -d -m 700 /srv/sancturai-data/backups
name="$(date -u +%Y%m%dT%H%M%SZ)-${label}.dump"
tmp="${dir}/.${name}.tmp"
trap 'rm -f "${tmp}"' EXIT
# -T : pas de pseudo-terminal (flux binaire intact) ; mot de passe inutile, `peer` sur le socket du conteneur.
docker compose exec -T -u postgres db pg_dump -U postgres -Fc -Z 6 sancturai > "${tmp}"
[ -s "${tmp}" ] || { echo "sauvegarde vide" >&2; exit 1; }
mv "${tmp}" "${dir}/${name}"
trap - EXIT
# Rotation : les 7 plus récentes (noms horodatés UTC, donc triables), toutes étiquettes confondues.
find "${dir}" -maxdepth 1 -type f -name '*.dump' -printf '%f\n' | sort -r | tail -n +8 \
  | while read -r old; do rm -f -- "${dir}/${old}"; done
echo "${dir}/${name}"
