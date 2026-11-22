#!/bin/sh
# Exécuté une seule fois à l'initialisation du volume. Mots de passe depuis sancturai-db.env.
# Ils passent par des variables psql (:'var' = littéral SQL échappé), jamais interpolés dans le SQL.
set -eu
: "${SANCTURAI_DB_PASSWORD:?absent de sancturai-db.env}"
: "${SANCTURAI_RELAY_PASSWORD:?absent de sancturai-db.env}"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres \
  -v db_pw="$SANCTURAI_DB_PASSWORD" -v relay_pw="$SANCTURAI_RELAY_PASSWORD" <<'SQL'
CREATE ROLE sancturai LOGIN PASSWORD :'db_pw';
CREATE ROLE relay LOGIN PASSWORD :'relay_pw';
CREATE DATABASE sancturai OWNER sancturai;
REVOKE ALL ON DATABASE sancturai FROM PUBLIC;
GRANT CONNECT ON DATABASE sancturai TO relay;
SQL
