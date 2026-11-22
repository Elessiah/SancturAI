#!/bin/sh
# Exécuté une seule fois à l'initialisation du volume. Mots de passe depuis sancturai-db.env.
# Lus par psql dans son environnement (\getenv) puis passés en :'var' (littéral SQL échappé) : jamais
# interpolés dans le SQL ni passés en argument (la ligne de commande est visible de tout compte de l'hôte via ps).
set -eu
: "${SANCTURAI_DB_PASSWORD:?absent de sancturai-db.env}"
: "${SANCTURAI_RELAY_PASSWORD:?absent de sancturai-db.env}"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
\getenv db_pw SANCTURAI_DB_PASSWORD
\getenv relay_pw SANCTURAI_RELAY_PASSWORD
CREATE ROLE sancturai LOGIN PASSWORD :'db_pw';
CREATE ROLE relay LOGIN PASSWORD :'relay_pw';
CREATE DATABASE sancturai OWNER sancturai;
REVOKE ALL ON DATABASE sancturai FROM PUBLIC;
GRANT CONNECT ON DATABASE sancturai TO relay;
SQL
