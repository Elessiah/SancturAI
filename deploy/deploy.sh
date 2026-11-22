#!/usr/bin/env bash
# Déploiement idempotent de SancturAI : sudo deploy/deploy.sh <SHA complet du commit publié par la CI>.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
  echo "lancer avec sudo : sudo deploy/deploy.sh <sha>" >&2
  exit 1
fi
sha=${1:-}
# Image épinglée par commit (jamais :latest) ; le SHA validé ne peut rien injecter dans .env ni dans compose.
[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || { echo "usage : sudo deploy/deploy.sh <SHA complet (40 hex)>" >&2; exit 1; }
[ -f .env ] || { echo "deploy/.env absent (voir env.example)" >&2; exit 1; }
current_image=$(grep -E '^SANCTURAI_IMAGE=' .env | cut -d= -f2-) || true
[[ "${current_image}" == *:* ]] || { echo "SANCTURAI_IMAGE absent de deploy/.env (voir env.example)" >&2; exit 1; }
export SANCTURAI_IMAGE="${current_image%:*}:${sha}"

notify() {
  local token body
  token=$(grep -E '^SANCTURAI_BOT_TOKEN=' /home/elessiah/apps/.secrets/sancturai-relay.env | cut -d= -f2-) || return 0
  # corps sérialisé par json.dumps : aucun caractère du texte ne peut casser le JSON ni y ajouter un champ
  body=$(python3 -c 'import json, sys; print(json.dumps({"kind": "log", "text": sys.argv[1]}))' "$1") || return 0
  # en-tête lu sur stdin (-H @-) : le jeton n'apparaît pas dans la ligne de commande (ps)
  printf 'x-internal-token: %s\n' "${token}" | curl -fsS -m 5 -X POST \
    http://127.0.0.1:4400/internal/sancturai/notify \
    -H "content-type: application/json" -H @- \
    --data-binary "${body}" >/dev/null || true
}

docker compose pull
docker compose up -d --wait db
docker compose run --rm --no-deps app alembic upgrade head
# Le relais ne modifie que l'état d'envoi, jamais kind ni payload (le REVOKE retire un UPDATE global antérieur).
docker compose exec -T -u postgres db psql -U postgres -d sancturai -v ON_ERROR_STOP=1 \
  -c "REVOKE UPDATE ON outbox FROM relay; GRANT USAGE ON SCHEMA public TO relay; GRANT SELECT, UPDATE (sent_at, attempts, next_attempt_at) ON outbox TO relay;"
docker compose up -d app
# Les commandes `docker compose` manuelles (create-founder, urgence) visent désormais l'image déployée.
sed -i "s|^SANCTURAI_IMAGE=.*|SANCTURAI_IMAGE=${SANCTURAI_IMAGE}|" .env

# Le listener 127.0.0.1:8300 ne sert que /internal/* : le healthcheck interroge le socket de l'app.
for _ in $(seq 1 30); do
  if curl -fsS -m 2 --unix-socket /run/sancturai/app.sock http://localhost/healthz >/dev/null; then
    # image précédente (sinon chaque déploiement laisse ~200 Mo sur le disque du Pi) ; ciblée, jamais `prune`
    if [ "${current_image}" != "${SANCTURAI_IMAGE}" ]; then
      docker image rm "${current_image}" >/dev/null 2>&1 || true
    fi
    notify "Déploiement réussi : ${SANCTURAI_IMAGE}"
    echo "OK"
    exit 0
  fi
  sleep 2
done
notify "Déploiement ÉCHOUÉ (healthcheck) : ${SANCTURAI_IMAGE}"
echo "healthcheck failed" >&2
exit 1
