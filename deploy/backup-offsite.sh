#!/usr/bin/env bash
# Copie hors du Pi d'une sauvegarde quotidienne : sudo deploy/backup-offsite.sh <dump> (appelé par backup.sh daily).
# Alignée sur celle du site et du bot (blueGenjiBot/scripts/backup-onedrive.sh) : chiffrée par age pour les mêmes
# destinataires, envoyée par rclone sur le même stockage (dossier et préfixe à part, que les purges du bot ne touchent
# pas), effacée là-bas au-delà de RETENTION_DAYS jours. Le dump en clair ne quitte jamais root : age le chiffre ici,
# et rclone, lancé sous le compte qui porte sa configuration (RCLONE_USER), n'en voit que le chiffré.
# Le chiffré est posé sur le volume de données (pas le dossier temporaire : racine partagée ou RAM), dans un dossier de root où le
# compte de rclone ne peut rien créer ni remplacer (pas de lien piégé sous les écritures de root) ; backup.sh lui a
# vérifié la place. Configuration : /home/elessiah/apps/.secrets/sancturai-backup.env (voir backup-offsite.env.example),
# lue ligne à ligne, jamais exécutée. Ces scripts vivent dans le dépôt du compte admin : ce compte reste de confiance.
# Place : la copie chiffrée n'est posée que si le volume reste sous le même seuil que backup.sh ; sinon, échec notifié
# (la sauvegarde locale, elle, est faite).
# Statut de chaque passage : /var/lib/sancturai/backup-status.json ; tout échec, et l'absence de configuration, sont
# notifiés à l'admin (bot Discord).
# Code 2 : rien n'est envoyé car la configuration est absente ; tout autre échec : 1.
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "lancer avec sudo : sudo deploy/backup-offsite.sh <dump>" >&2
  exit 1
fi
dump=${1:-}
[[ "${dump}" =~ ^/srv/sancturai-data/backups/[0-9]{8}T[0-9]{6}Z-daily\.dump$ && -s "${dump}" ]] \
  || { echo "usage : sudo deploy/backup-offsite.sh /srv/sancturai-data/backups/<horodatage>-daily.dump" >&2; exit 1; }
secrets=/home/elessiah/apps/.secrets
config=${secrets}/sancturai-backup.env
status_file=/var/lib/sancturai/backup-status.json
stage=/srv/sancturai-data/offsite
max_used_pct=85  # = backup.sh (garde disque de l'app)
# Marge des purges sous la borne, la même que le bot (PURGE_MARGIN_HOURS de backup-onedrive.sh).
purge_margin_hours=3

# shellcheck source=notify.sh
. ./notify.sh

# Même schéma que le statut du bot ({date, ok, remote, sizeBytes, parts, error}), pour qu'il puisse le relire.
# Écrit à part puis renommé ; json.dumps échappe toute sortie de rclone ou d'age.
write_status() {
  local tmp
  install -d -m 755 "$(dirname "${status_file}")"
  tmp=$(mktemp "${status_file}.tmp.XXXXXXXX")
  python3 - "$@" > "${tmp}" <<'PY'
import datetime, json, sys
ok, remote, size, error = sys.argv[1:5]
print(json.dumps({"date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "ok": ok == "true",
                  "remote": remote, "sizeBytes": int(size or 0), "parts": "postgres", "error": error or None}))
PY
  chmod 644 "${tmp}"
  mv -f -T "${tmp}" "${status_file}"
}

remote_label=""
fail() {
  echo "[backup-offsite] ÉCHEC : $1" >&2  # la cause part au journal d'abord
  write_status false "${remote_label}" 0 "$1" || echo "[backup-offsite] statut non écrit" >&2
  notify "Sauvegarde SancturAI hors du Pi en échec : $1 (la sauvegarde locale est faite)."
  exit 1
}
step="préparation"
# TimeoutStartSec du service (ou un arrêt) : l'interruption est un échec signalé, pas un silence. Toute autre commande
# en échec (set -e) aussi : ERR passe par fail, qui écrit le statut et notifie.
trap 'fail "interrompu pendant : ${step} (délai du service dépassé ou arrêt)"' TERM INT
trap 'fail "erreur inattendue ligne ${LINENO} (${step})"' ERR

if [[ ! -f "${config}" ]]; then
  # pas une erreur du service (la sauvegarde locale est faite), mais la charte promet cette copie : l'admin le sait
  msg="${config} absent : aucune copie hors du Pi (voir setup.md, « Copie hors du Pi »)"
  echo "AVERTISSEMENT : ${msg}" >&2
  write_status false "" 0 "${msg}" || echo "[backup-offsite] statut non écrit" >&2
  notify "Sauvegarde SancturAI : ${msg}. La sauvegarde locale est faite."
  exit 2
fi
conf() {  # dernière valeur KEY=… du fichier, sinon le défaut
  local value
  value=$(grep -E "^$1=" "${config}" | tail -n 1 | cut -d= -f2-) || true
  printf '%s' "${value:-$2}"
}
rclone_remote=$(conf RCLONE_REMOTE "")
remote_dir=$(conf REMOTE_DIR SancturAI/backups)
recipients=$(conf AGE_RECIPIENTS_FILE /home/elessiah/apps/blueGenjiBot/scripts/backup-recipients.txt)
retention_days=$(conf RETENTION_DAYS 30)
rclone_user=$(conf RCLONE_USER elessiah)
bwlimit=$(conf BWLIMIT "")
remote_label="${rclone_remote}:${remote_dir}"
# Valeurs passées à rclone et à runuser : formes fermées, aucune option ni chemin parent ne s'y glisse.
[[ "${rclone_remote}" =~ ^[A-Za-z0-9_-]+$ ]] || fail "RCLONE_REMOTE invalide ou absent"
[[ "${remote_dir}" =~ ^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$ ]] || fail "REMOTE_DIR invalide (${remote_dir})"
# Au moins 1 jour : 0 donnerait un seuil négatif à la purge, qui effacerait toutes les copies.
[[ "${retention_days}" =~ ^[1-9][0-9]*$ ]] || fail "RETENTION_DAYS invalide (${retention_days})"
[[ "${rclone_user}" =~ ^[a-z_][a-z0-9_-]*$ ]] && id -u "${rclone_user}" >/dev/null 2>&1 \
  || fail "RCLONE_USER invalide ou inconnu (${rclone_user})"
[[ -s "${recipients}" ]] || fail "destinataires age absents (${recipients})"
# Débit plafonné au besoin : le lien montant est partagé avec le site et le bot (Nice/IO ne bornent pas le réseau).
[[ -z "${bwlimit}" || "${bwlimit}" =~ ^[1-9][0-9]*[KMG]?$ ]] || fail "BWLIMIT invalide (${bwlimit}) : p. ex. 2M"
limit_args=(--transfers 1)  # jamais vide : "${limit_args[@]}" reste sûr sous set -u avec un bash ancien
[[ -z "${bwlimit}" ]] || limit_args+=(--bwlimit "${bwlimit}")

# rclone officiel dans /usr/local/bin (celui d'APT est trop ancien), comme le cron du bot.
as_rclone_user() { runuser -u "${rclone_user}" -- env PATH=/usr/local/bin:/usr/bin:/bin "$@"; }
for binary in age python3 curl; do
  command -v "${binary}" >/dev/null 2>&1 || fail "binaire manquant : ${binary}"
done
as_rclone_user rclone version >/dev/null 2>&1 || fail "rclone introuvable pour ${rclone_user}"

umask 077
# Dossier de root, lisible par tous (il ne contient que du chiffré), modifiable par root seul.
[[ ! -L "${stage}" ]] || fail "${stage} est un lien symbolique"
install -d -o root -g root -m 755 "${stage}"
# Un seul envoi à la fois (un déploiement pendant un envoi lent relance backup.sh, pas ce script, mais un lancement
# manuel le pourrait) : le reliquat n'est retiré que sous ce verrou, jamais pendant l'envoi d'un autre.
exec 8>"${stage}/.lock"
flock -n 8 || fail "un autre envoi hors du Pi est en cours"
find "${stage}" -maxdepth 1 -type f -name 'sancturai-*.age' -delete  # reliquat d'un passage interrompu
# La copie chiffrée fait la taille du dump (le vault est déjà chiffré, age n'y ajoute presque rien).
dump_bytes=$(stat -c %s "${dump}")
read -r total used < <(df --output=size,used -B1 "${stage}" | tail -n 1)
[[ $(( (used + dump_bytes) * 100 )) -lt $(( total * max_used_pct )) ]] \
  || fail "place insuffisante sur le volume pour la copie chiffrée (${dump_bytes} octets, seuil ${max_used_pct} %)"
name="sancturai-$(basename "${dump}").age"
trap 'rm -f "${stage}/${name}"' EXIT
step="chiffrement"
age --encrypt --recipients-file "${recipients}" --output "${stage}/${name}" "${dump}" \
  || fail "chiffrement age impossible"
chmod 644 "${stage}/${name}"
size=$(stat -c %s "${stage}/${name}")

step="envoi"
as_rclone_user rclone copy "${stage}/${name}" "${remote_label}" --retries 3 --low-level-retries 10 \
  "${limit_args[@]}" || fail "envoi rclone vers ${remote_label} impossible"

# Purge par ancienneté, limitée à nos copies : un échec n'invalide pas la copie déjà envoyée. Sur Nextcloud (WebDAV),
# corbeille et versions se coupent côté serveur, comme pour le bot (doc/backup-onedrive.md).
step="purge"
as_rclone_user rclone delete "${remote_label}" --min-age "$((retention_days * 24 - purge_margin_hours))h" \
  --include "sancturai-*.dump.age" \
  || echo "[backup-offsite] purge des anciennes copies incomplète" >&2

write_status true "${remote_label}" "${size}" "" || echo "[backup-offsite] statut non écrit" >&2
echo "${remote_label}/${name}"
