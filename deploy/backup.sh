#!/usr/bin/env bash
# Sauvegarde de la base : sudo deploy/backup.sh daily|pre-<12 hex>  (daily : minuteur systemd ; pre- : deploy.sh).
# pg_dump au format custom (compressé) dans le volume de 20 Go, jamais sur la racine partagée. La base ne contient
# que des chiffrés et des empreintes : aucune phrase ni clé n'est écrite. Rotation par étiquette : 7 quotidiennes,
# 3 d'avant migration (celle d'un retour arrière survit aux quotidiennes). Restauration : setup.md.
# Code 2 : sauvegarde ignorée (volume non monté ou trop plein) ; tout autre échec : 1.
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "lancer avec sudo : sudo deploy/backup.sh daily|pre-<sha12>" >&2
  exit 1
fi
label=${1:-}
[[ "${label}" =~ ^(daily|pre-[0-9a-f]{12})$ ]] || { echo "usage : sudo deploy/backup.sh daily|pre-<sha12>" >&2; exit 1; }
data=/srv/sancturai-data
dir=${data}/backups
# Même seuil que la garde disque de l'app (disk_busy_ratio = 0.85), sauvegarde comprise.
max_used_pct=85
if [[ "${label}" = daily ]]; then
  kind=daily keep=7
  # Le minuteur attend la fin d'un déploiement en cours (deploy.sh tient ce verrou et appelle ce script lui-même).
  exec 9>/run/lock/sancturai-deploy.lock
  flock -w 1800 9 || { echo "déploiement en cours depuis 30 min : sauvegarde reportée" >&2; exit 1; }
else
  kind=pre keep=3
fi

mountpoint -q "${data}" || { echo "AVERTISSEMENT : ${data} non monté, sauvegarde ignorée" >&2; exit 2; }
# Taille de la base (majorant du dump compressé) : pg/ est sur le même volume, un dump qui le remplirait
# arrêterait Postgres. On ne sauvegarde que si usage actuel + taille de la base reste sous le seuil.
db_bytes=$(docker compose exec -T -u postgres db psql -U postgres -Atc "SELECT pg_database_size('sancturai')")
[[ "${db_bytes}" =~ ^[0-9]+$ ]] || { echo "taille de la base illisible" >&2; exit 1; }
read -r total used < <(df --output=size,used -B1 "${data}" | tail -n 1)
if [[ $(( (used + db_bytes) * 100 )) -ge $(( total * max_used_pct )) ]]; then
  echo "AVERTISSEMENT : ${data} dépasserait ${max_used_pct} % avec la sauvegarde (${db_bytes} octets) : ignorée" >&2
  exit 2
fi

umask 077
install -d -m 700 /srv/sancturai-data/backups
name="$(date -u +%Y%m%dT%H%M%SZ)-${label}.dump"
tmp="${dir}/.${name}.tmp"
trap 'rm -f "${tmp}"' EXIT
# -T : pas de pseudo-terminal (flux binaire intact) ; mot de passe inutile, `peer` sur le socket du conteneur.
docker compose exec -T -u postgres db pg_dump -U postgres -Fc -Z 6 sancturai > "${tmp}"
[[ -s "${tmp}" ]] || { echo "sauvegarde vide" >&2; exit 1; }
mv "${tmp}" "${dir}/${name}"
trap - EXIT
# Rotation dans la famille de l'étiquette (noms horodatés UTC, donc triables).
find "${dir}" -maxdepth 1 -type f -name "*-${kind}*.dump" -printf '%f\n' | sort -r | tail -n +"$((keep + 1))" \
  | while read -r old; do rm -f -- "${dir}/${old}"; done
echo "${dir}/${name}"
