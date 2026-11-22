#!/usr/bin/env bash
# Déploiement idempotent de SancturAI. Lancer avec sudo depuis ~/apps/SancturAI/deploy.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
  echo "lancer avec sudo : sudo ./deploy.sh" >&2
  exit 1
fi
[ -f .env ] || [ -n "${SANCTURAI_IMAGE:-}" ] || { echo "deploy/.env absent (voir env.example)" >&2; exit 1; }
: "${SANCTURAI_IMAGE:=$(grep -E '^SANCTURAI_IMAGE=' .env | cut -d= -f2-)}"
export SANCTURAI_IMAGE

notify() {
  local token
  token=$(grep -E '^SANCTURAI_BOT_TOKEN=' /home/elessiah/apps/.secrets/sancturai-relay.env | cut -d= -f2-) || return 0
  # en-tête lu sur stdin (-H @-) : le jeton n'apparaît pas dans la ligne de commande (ps)
  printf 'x-internal-token: %s\n' "${token}" | curl -fsS -m 5 -X POST \
    http://127.0.0.1:4400/internal/sancturai/notify \
    -H "content-type: application/json" -H @- \
    -d "{\"kind\":\"log\",\"text\":\"$1\"}" >/dev/null || true
}

# Image précédente : devenue sans étiquette après le pull, elle est supprimée en fin de déploiement
# (sinon chaque déploiement laisse ~200 Mo sur le disque du Pi). Ciblée : on ne touche pas aux autres images.
old_image_id=$(docker image inspect -f '{{.Id}}' "${SANCTURAI_IMAGE}" 2>/dev/null || true)
docker compose pull
docker compose up -d --wait db
docker compose run --rm --no-deps app alembic upgrade head
docker compose exec -T -u postgres db psql -U postgres -d sancturai -v ON_ERROR_STOP=1 \
  -c "GRANT USAGE ON SCHEMA public TO relay; GRANT SELECT, UPDATE ON outbox TO relay;"
docker compose up -d app

# Le listener 127.0.0.1:8300 ne sert que /internal/* : le healthcheck interroge le socket de l'app.
for _ in $(seq 1 30); do
  if curl -fsS -m 2 --unix-socket /run/sancturai/app.sock http://localhost/healthz >/dev/null; then
    new_image_id=$(docker image inspect -f '{{.Id}}' "${SANCTURAI_IMAGE}")
    if [ -n "${old_image_id}" ] && [ "${old_image_id}" != "${new_image_id}" ]; then
      docker image rm "${old_image_id}" >/dev/null 2>&1 || true
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
