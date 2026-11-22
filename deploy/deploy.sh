#!/usr/bin/env bash
# Déploiement idempotent de SancturAI : sudo deploy/deploy.sh <SHA complet du commit publié par la CI>.
# Ordre : vérifications → pull → sauvegarde → migration → app → healthcheck ; .env n'est réécrit qu'après un
# healthcheck réussi, et un échec ramène l'app sur l'image précédente (setup.md, « Retour arrière »).
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
  echo "lancer avec sudo : sudo deploy/deploy.sh <sha>" >&2
  exit 1
fi
# Un seul déploiement à la fois (le minuteur de sauvegarde attend aussi ce verrou).
exec 9>/run/lock/sancturai-deploy.lock
flock -n 9 || { echo "un autre déploiement est en cours" >&2; exit 1; }
sha=${1:-}
# Image épinglée par commit (jamais :latest) ; le SHA validé ne peut rien injecter dans .env ni dans compose.
[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || { echo "usage : sudo deploy/deploy.sh <SHA complet (40 hex)>" >&2; exit 1; }
[ -f .env ] || { echo "deploy/.env absent (voir env.example)" >&2; exit 1; }
secrets=/home/elessiah/apps/.secrets
for f in "${secrets}/sancturai-db.env" "${secrets}/sancturai.env" "${secrets}/sancturai-relay.env"; do
  [ -r "${f}" ] || { echo "${f} absent ou illisible (setup.md §1)" >&2; exit 1; }
done
current_image=$(grep -E '^SANCTURAI_IMAGE=' .env | cut -d= -f2-) || true
# `dépôt:étiquette` uniquement : une référence par digest (@sha256:…) donnerait ici une image invalide.
[[ "${current_image}" =~ ^[^@]+:[^/:]+$ ]] \
  || { echo "SANCTURAI_IMAGE absent de deploy/.env ou pas de la forme dépôt:étiquette (voir env.example)" >&2; exit 1; }
previous_image=$(grep -E '^SANCTURAI_PREVIOUS_IMAGE=' .env | cut -d= -f2-) || true
export SANCTURAI_IMAGE="${current_image%:*}:${sha}"
# nginx -t ne lie aucun socket : un port 8300 pris laisse l'ancienne configuration active (setup.md §4).
# Substitution de processus plutôt qu'un tube : sous pipefail, grep -q qui s'arrête tôt ferait échouer ss (SIGPIPE).
grep -q '"nginx"' < <(ss -ltnpH 'sport = :8300') \
  || { echo "nginx n'écoute pas sur 127.0.0.1:8300 (port pris par un autre processus ?) : voir setup.md §4" >&2; exit 1; }
echo "image en service : ${current_image}"

notify() {
  local token body
  token=$(grep -E '^SANCTURAI_BOT_TOKEN=' "${secrets}/sancturai-relay.env" | cut -d= -f2-) || return 0
  # corps sérialisé par json.dumps : aucun caractère du texte ne peut casser le JSON ni y ajouter un champ
  body=$(python3 -c 'import json, sys; print(json.dumps({"kind": "log", "text": sys.argv[1]}))' "$1") || return 0
  # en-tête lu sur stdin (-H @-) : le jeton n'apparaît pas dans la ligne de commande (ps)
  printf 'x-internal-token: %s\n' "${token}" | curl -fsS -m 5 -X POST \
    http://127.0.0.1:4400/internal/sancturai/notify \
    -H "content-type: application/json" -H @- \
    --data-binary "${body}" >/dev/null || true
}

# Toute sortie imprévue (set -e) est signalée ; les échecs après la migration passent par le retour arrière plus bas.
trap 'notify "deploy.sh ${sha:0:12} interrompu (ligne ${LINENO}) ; image en service : ${current_image}"' ERR

fail_before_migration() {
  local msg="Déploiement ${sha:0:12} interrompu avant la migration : $1 ; rien n'a changé (image ${current_image})."
  notify "${msg}"
  echo "${msg}" >&2
  exit 1
}

healthy() {
  local _
  for _ in $(seq 1 30); do
    curl -fsS -m 2 --unix-socket /run/sancturai/app.sock http://localhost/healthz >/dev/null && return 0
    sleep 2
  done
  return 1
}

docker compose pull
docker compose up -d --wait db
# Pas de migration sans sauvegarde : c'est elle, jamais `alembic downgrade`, qui garantit le retour arrière.
./backup.sh "pre-${sha:0:12}" \
  || fail_before_migration "sauvegarde pre-${sha:0:12} impossible (volume non monté, trop plein, ou erreur : voir ci-dessus)"
docker compose run --rm --no-deps app alembic upgrade head

grants() {
  # Le relais ne modifie que l'état d'envoi, jamais kind ni payload (le REVOKE retire un UPDATE global antérieur).
  docker compose exec -T -u postgres db psql -U postgres -d sancturai -v ON_ERROR_STOP=1 \
    -c "REVOKE UPDATE ON outbox FROM relay; GRANT USAGE ON SCHEMA public TO relay; GRANT SELECT, UPDATE (sent_at, attempts, next_attempt_at) ON outbox TO relay;"
}

# Droits, démarrage et healthcheck (le listener 8300 ne sert que /internal/* : le healthcheck interroge le socket
# de l'app) : le moindre échec passe par le retour arrière ci-dessous, .env restant inchangé.
if grants && docker compose up -d app && healthy; then
  if [ "${current_image}" != "${SANCTURAI_IMAGE}" ]; then
    # Les commandes `docker compose` manuelles (create-founder, urgence) visent désormais l'image déployée ;
    # l'image remplacée est gardée (retour arrière) et seule l'avant-dernière est supprimée (ciblée, jamais `prune`).
    sed -i "s|^SANCTURAI_IMAGE=.*|SANCTURAI_IMAGE=${SANCTURAI_IMAGE}|" .env
    if grep -q '^SANCTURAI_PREVIOUS_IMAGE=' .env; then
      sed -i "s|^SANCTURAI_PREVIOUS_IMAGE=.*|SANCTURAI_PREVIOUS_IMAGE=${current_image}|" .env
    else
      echo "SANCTURAI_PREVIOUS_IMAGE=${current_image}" >> .env
    fi
    if [ -n "${previous_image}" ] && [ "${previous_image}" != "${current_image}" ] \
        && [ "${previous_image}" != "${SANCTURAI_IMAGE}" ]; then
      docker image rm "${previous_image}" >/dev/null 2>&1 || true
    fi
  fi
  notify "Déploiement réussi : ${SANCTURAI_IMAGE}"
  echo "OK"
  exit 0
fi

# Échec : .env n'a pas bougé ; l'app repart sur l'image précédente (déjà présente, jamais retéléchargée).
echo "déploiement en échec (droits, démarrage ou healthcheck) : retour à ${current_image}" >&2
if SANCTURAI_IMAGE="${current_image}" docker compose up -d --pull never app && healthy; then
  state="retour à ${current_image} effectué"
  # L'image ratée ne s'accumule pas sur la racine partagée : seules l'image en service et la précédente restent.
  if [ "${SANCTURAI_IMAGE}" != "${current_image}" ] && [ "${SANCTURAI_IMAGE}" != "${previous_image}" ]; then
    docker image rm "${SANCTURAI_IMAGE}" >/dev/null 2>&1 || true
  fi
else
  state="retour à ${current_image} ÉCHOUÉ, site indisponible"
fi
msg="Déploiement ÉCHOUÉ : ${SANCTURAI_IMAGE} ; ${state}. Si la migration a changé le schéma, restaurer la sauvegarde pre-${sha:0:12} (setup.md, « Retour arrière »)."
notify "${msg}"
echo "${msg}" >&2
exit 1
