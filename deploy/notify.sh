# Notification à l'admin (bot Discord), partagée par backup.sh et backup-offsite.sh (sourcée, jamais exécutée) :
# même appel que deploy.sh, POST /internal/sancturai/notify du bot avec kind "log". Échec silencieux : une
# notification perdue ne fait jamais échouer une sauvegarde.
notify() {
  local token body
  token=$(grep -E '^SANCTURAI_BOT_TOKEN=' /home/elessiah/apps/.secrets/sancturai-relay.env 2>/dev/null \
    | cut -d= -f2-) || return 0
  [[ -n "${token}" ]] || return 0
  # corps sérialisé par json.dumps : aucun caractère du texte ne peut casser le JSON ni y ajouter un champ
  body=$(python3 -c 'import json, sys; print(json.dumps({"kind": "log", "text": sys.argv[1]}))' "$1") || return 0
  # en-tête lu sur stdin (-H @-) : le jeton n'apparaît pas dans la ligne de commande (ps)
  printf 'x-internal-token: %s\n' "${token}" | curl -fsS -m 5 -X POST \
    http://127.0.0.1:4400/internal/sancturai/notify \
    -H "content-type: application/json" -H @- \
    --data-binary "${body}" >/dev/null 2>&1 || true
}
