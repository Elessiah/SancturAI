import re
import subprocess
from pathlib import Path

import yaml
from starlette.responses import JSONResponse

from app.config import Settings
from app.middleware import DEFAULT_BODY_BYTES, body_cap, too_large_error

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DOMAIN = "sancturai.bluegenji-esport.fr"
DATA = "/srv/sancturai-data"


def text(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def setup_doc() -> str:
    return text("docs", "ops", "setup.md")


def nginx_size(value: str) -> int:
    units = {"k": 1024, "m": 1024 * 1024}
    return int(value[:-1]) * units[value[-1]] if value[-1] in units else int(value)


def named_return(locs: list[str], name: str) -> tuple[int, str]:
    """Statut et corps (échappements nginx \\' retirés) du `return` de la location nommée."""
    loc = next(b for b in locs if b.lstrip().startswith(f"location {name} "))
    m = re.search(r"return (\d{3}) '((?:[^'\\]|\\.)*)';", loc)
    assert m is not None, name
    return int(m.group(1)), re.sub(r"\\(.)", r"\1", m.group(2))


def compose() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def nginx() -> str:
    return (DEPLOY / "nginx" / "sancturai.conf").read_text(encoding="utf-8")


def blocks(conf: str, opener: str) -> list[str]:
    """Corps des blocs `opener ... {` (accolades équilibrées), commentaires retirés."""
    text = re.sub(r"#[^\n]*", "", conf)
    out = []
    for m in re.finditer(rf"(?m)^\s*{opener}[^{{;]*\{{", text):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out.append(text[m.start():i])
    return out


def test_network_is_internal_and_no_ports() -> None:
    c = compose()
    assert c["networks"]["internal"]["internal"] is True
    for name, svc in c["services"].items():
        assert "ports" not in svc, f"{name} ne doit publier aucun port"
        assert svc["networks"] == ["internal"]
    # internal: true seul laisse à l'hôte une IP sur le pont : les conteneurs joindraient ses services
    assert c["networks"]["internal"]["driver_opts"] == {"com.docker.network.bridge.inhibit_ipv4": "true"}
    assert c["networks"]["internal"]["enable_ipv6"] is False  # pas de passerelle v6 de l'hôte non plus
    # image Postgres épinglée par digest ; l'app par SHA de commit (deploy.sh), jamais :latest
    assert re.fullmatch(r"postgres:17-alpine@sha256:[0-9a-f]{64}", c["services"]["db"]["image"])


def test_hardening_and_limits() -> None:
    for name, svc in compose()["services"].items():
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cgroup_parent"] == "sancturai.slice", name
        assert svc["oom_score_adj"] == 1000, name
        assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name
        assert "mem_limit" in svc and "cpus" in svc and "pids_limit" in svc, name
        assert svc["read_only"] is True and svc["tmpfs"] == ["/tmp:size=64m"], name  # noqa: S108 (§4.2)
    app = compose()["services"]["app"]
    assert app["user"] == "10001:33"
    # §4.2 : db non-root sans aucune capacité ; le relais joint le socket 70:70 0770 par son groupe 70
    db = compose()["services"]["db"]
    assert db["user"] == "70:70" and "cap_add" not in db
    relay = (DEPLOY / "systemd" / "sancturai-relay.service").read_text(encoding="utf-8").splitlines()
    assert "SupplementaryGroups=sancturai-pg" in relay
    doc = (ROOT / "docs" / "ops" / "setup.md").read_text(encoding="utf-8")
    # gid/uid 70 réservés à un groupe/compte dédié (uid 70 = superutilisateur postgres en `peer` sur le socket)
    assert "sudo groupadd --gid 70 sancturai-pg" in doc and "sudo useradd --system --uid 70 --gid 70" in doc
    assert doc.index("groupadd --gid 70") < doc.index("/srv/sancturai-data/pg\n")
    assert "install -d -o 70 -g 70 -m 700 /srv/sancturai-data/pg\n" in doc
    assert "install -d -o 70 -g 70 -m 770 /srv/sancturai-data/pg-socket" in doc
    # budget mémoire : db + app + relais (128M) tiennent dans MemoryMax=1.5G de la tranche
    limits = {n: svc["mem_limit"] for n, svc in compose()["services"].items()}
    assert limits == {"db": "512m", "app": "896m"}
    assert 512 + 896 + 128 <= 1536
    assert "--workers 1" in " ".join(app["command"])
    assert "--no-access-log" in " ".join(app["command"])  # chemins vault en clair dans les URL


def test_nginx_blocks_internal_and_slice_limits() -> None:
    conf = nginx()
    assert "location /internal/" in conf and "return 404" in conf
    assert "listen 127.0.0.1:8300" in conf
    assert "log_format sancturai_nouri" in conf
    assert "access_log /var/log/nginx/sancturai.access.log sancturai_nouri" in conf
    # `alert`, pas `crit` : nginx émet « connect() to unix:... failed (2: No such file) » en crit, avec
    # la ligne de requête (URI vault), dès que le socket de l'app est absent (redémarrage)
    assert "error_log /var/log/nginx/sancturai.error.log alert" in conf
    assert re.search(r"error_log\s+\S+\s+(debug|info|notice|warn|error|crit);", conf) is None
    assert "error_page 413 = @sancturai_413;" in conf
    # flux SSE longs : plafond de connexions par IP (worker_connections est partagé avec les autres sites)
    public = next(b for b in blocks(conf, "location") if b.lstrip().startswith("location / "))
    assert "limit_conn sancturai_conn 20;" in public and "limit_conn_status 429;" in public
    assert "limit_conn_zone $binary_remote_addr zone=sancturai_conn:10m;" in conf
    # un client IPv6 change d'adresse dans son /64 : plafond global du vhost en plus du plafond par IP
    assert "limit_conn_zone $server_name zone=sancturai_total:1m;" in conf
    assert "limit_conn sancturai_total 256;" in public
    sl = (DEPLOY / "systemd" / "sancturai.slice").read_text(encoding="utf-8")
    for line in ("CPUWeight=20", "CPUQuota=150%", "MemoryHigh=1.2G", "MemoryMax=1.5G", "IOWeight=20",
                 "MemorySwapMax=0"):  # revue batch 6, m6 : jamais de pagination sur le swap partagé
        assert line in sl.splitlines(), line
    for name, svc in compose()["services"].items():
        assert svc["memswap_limit"] == svc["mem_limit"], name


def test_nginx_never_logs_uri() -> None:
    conf = nginx()
    # le fichier remplace le vhost provisoire qui définit déjà ce log_format : une seule définition
    assert conf.count("log_format sancturai_nouri") == 1
    fmt = re.search(r"log_format sancturai_nouri([^;]*);", conf)
    assert fmt is not None
    for var in ("$request_uri", "$request ", "$request'", "$uri", "$args", "$query_string", "$arg_",
                "$http_referer"):
        assert var not in fmt.group(1), var
    # $request_uri n'est admis que dans la redirection 80 -> 443 (jamais $uri : injection CRLF)
    for line in conf.splitlines():
        if "$request_uri" in line:
            assert line.strip().startswith(f"return 301 https://{DOMAIN}$request_uri"), line
    assert "$uri" not in conf
    servers = blocks(conf, "server")
    assert len(servers) == 3  # 80 (redirection), 443 (public), 127.0.0.1:8300 (bot)
    for srv in servers:
        assert "access_log /var/log/nginx/sancturai.access.log sancturai_nouri;" in srv, srv[:80]
        assert "error_log /var/log/nginx/sancturai.error.log alert;" in srv, srv[:80]
        assert re.search(r"access_log\s+(?!/var/log/nginx/sancturai\.access\.log sancturai_nouri;)", srv) is None


def test_nginx_tls_and_redirect_match_certbot() -> None:
    conf = nginx()
    servers = blocks(conf, "server")
    http = [s for s in servers if re.search(r"listen 80;", s)]
    tls = [s for s in servers if "listen 443 ssl;" in s]
    assert len(http) == 1 and len(tls) == 1
    assert f"server_name {DOMAIN};" in http[0] and f"server_name {DOMAIN};" in tls[0]
    assert f"return 301 https://{DOMAIN}$request_uri;" in http[0]
    assert "proxy_pass" not in http[0]
    for line in (
        f"ssl_certificate /etc/letsencrypt/live/{DOMAIN}/fullchain.pem;",
        f"ssl_certificate_key /etc/letsencrypt/live/{DOMAIN}/privkey.pem;",
        "include /etc/letsencrypt/options-ssl-nginx.conf;",
        "ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;",
        "listen [::]:443 ssl;",
    ):
        assert line in tls[0], line
    # http2 est activé globalement (conf.d) ; ipv6only=on ne peut figurer qu'une fois sur le serveur
    assert "http2" not in conf and "ipv6only" not in conf
    assert "location /internal/" in tls[0]


def test_nginx_security_headers_wherever_add_header() -> None:
    """Règle d'héritage nginx : un add_header local masque ceux du niveau supérieur."""
    conf = nginx()
    for srv in blocks(conf, "server"):
        if "proxy_pass" in srv and "listen 443" in srv:
            assert "include snippets/security-headers.conf;" in srv
    for loc in blocks(conf, "location"):
        if "add_header" in loc:
            assert "include snippets/security-headers.conf;" in loc, loc[:60]


def test_nginx_local_listener_only_internal() -> None:
    local = [s for s in blocks(nginx(), "server") if "listen 127.0.0.1:8300;" in s]
    assert len(local) == 1
    srv = local[0]
    assert "listen [::" not in srv and re.search(r"listen (?!127\.0\.0\.1:8300;)", srv) is None
    locs = blocks(srv, "location")
    proxied = [loc for loc in locs if "proxy_pass" in loc]
    assert len(proxied) == 1 and proxied[0].lstrip().startswith("location /internal/")
    assert "proxy_set_header X-Real-IP $remote_addr;" in proxied[0]
    assert 'proxy_set_header X-Forwarded-For "";' in proxied[0]
    assert any(loc.lstrip().startswith("location / ") and "return 404" in loc for loc in locs)
    # revue batch 6, m5 : app en redémarrage → JSON §9.2 pour le bot aussi, pas la page 502 HTML
    assert "error_page 502 504 = @sancturai_busy;" in srv
    assert named_return(locs, "@sancturai_busy")[0] == 503
    assert "client_max_body_size 64k;" in srv


def test_nginx_json_errors() -> None:
    conf = nginx()
    assert "QUOTA_EXCEEDED" not in conf  # revue batch 6, m4 : même code que l'app pour un corps trop gros
    assert "limit_req_status 429;" in conf and "error_page 429 = @sancturai_429;" in conf
    assert '"error":"RATE_LIMITED"' in conf
    assert "error_page 502 504 = @sancturai_busy;" in conf and '"error":"SERVER_BUSY"' in conf
    assert "proxy_intercept_errors" not in conf  # les erreurs JSON de l'app passent telles quelles


def test_pg_hba_has_no_trust() -> None:
    hba = (DEPLOY / "postgres" / "pg_hba.conf").read_text(encoding="utf-8")
    assert "trust" not in hba
    rules = [line.split() for line in hba.splitlines() if line.strip() and not line.startswith("#")]
    relay = [r for r in rules if r[2] == "relay"]
    assert relay == [["local", "sancturai", "relay", "scram-sha-256"]]
    assert all(r[-1] in {"peer", "scram-sha-256"} for r in rules)


def test_db_socket_not_world_accessible() -> None:
    db = compose()["services"]["db"]
    assert "unix_socket_permissions=0770" in db["command"]
    assert "/srv/sancturai-data/pg-socket:/var/run/postgresql" in db["volumes"]


def test_roles_script_passes_passwords_as_psql_variables() -> None:
    sh = (DEPLOY / "postgres" / "init" / "01-roles.sh").read_text(encoding="utf-8")
    assert "PASSWORD %L', :'db_pw')" in sh and "PASSWORD %L', :'relay_pw')" in sh
    heredoc = sh.split("<<'SQL'", 1)[1]
    assert "${" not in heredoc  # heredoc littéral : aucun mot de passe interpolé dans le SQL
    # lus dans l'environnement par psql, jamais en argument (ligne de commande visible via ps sur l'hôte)
    assert r"\getenv db_pw SANCTURAI_DB_PASSWORD" in heredoc
    assert r"\getenv relay_pw SANCTURAI_RELAY_PASSWORD" in heredoc
    assert "_pw=" not in sh


def test_deploy_script_grants_and_healthcheck() -> None:
    sh = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    assert sh.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in sh
    # UPDATE limité à l'état d'envoi (relay/tests vérifie ces droits contre un vrai Postgres)
    assert ("REVOKE UPDATE ON outbox FROM relay; GRANT USAGE ON SCHEMA public TO relay; "
            "GRANT SELECT, UPDATE (sent_at, attempts, next_attempt_at) ON outbox TO relay;") in sh
    assert sh.index("alembic upgrade head") < sh.index("GRANT USAGE") < sh.index("docker compose up -d app")
    # image épinglée par SHA de commit validé ; corps JSON de notify sérialisé, jamais interpolé
    assert '[[ "${sha}" =~ ^[0-9a-f]{40}$ ]]' in sh and 'SANCTURAI_IMAGE="${current_image%:*}:${sha}"' in sh
    assert "json.dumps" in sh and '\\"text\\":' not in sh
    # le listener 8300 ne sert que /internal/* : le healthcheck passe par le socket de l'app
    assert "--unix-socket /run/sancturai/app.sock" in sh and "/healthz" in sh
    assert "8300/healthz" not in sh
    # l'image précédente est supprimée après un déploiement réussi (ciblée, jamais `image prune`)
    assert "docker image rm" in sh and "image prune" not in sh
    assert "x-internal-token: ${token}\" \\" not in sh  # jeton jamais en argument (visible dans ps)


def test_dockerfile_matches_compose_and_ci() -> None:
    df = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    cmd = re.search(r"^CMD (\[.*\])$", df, re.M)
    assert cmd is not None
    assert yaml.safe_load(cmd.group(1)) == compose()["services"]["app"]["command"]
    assert "USER 10001:33" in df and "--no-dev" in df
    uv = re.search(r"ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)", df)
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert uv is not None and f'version: "{uv.group(1)}"' in ci
    assert ci.count("persist-credentials: false") == ci.count("actions/checkout@") == 3  # S1
    assert "cache-from: type=gha" in ci and "cache-to: type=gha,mode=max" in ci
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".git", ".venv", "tests", "deploy", "relay", "*.env", ".env"):
        assert entry in ignore, entry


def test_relay_unit() -> None:
    unit = (DEPLOY / "systemd" / "sancturai-relay.service").read_text(encoding="utf-8")
    lines = unit.splitlines()
    for line in ("Slice=sancturai.slice", "NoNewPrivileges=yes", "ProtectSystem=strict",
                 "EnvironmentFile=/home/elessiah/apps/.secrets/sancturai-relay.env", "MemoryMax=128M"):
        assert line in lines, line
    # revue batch 6, I4 : compte système dédié (ni sudo ni shell), venv du relais exécuté directement : ni uv ni
    # cache partagé inscriptible ; tout ce qui est remonté l'est en lecture seule
    assert "User=sancturai-relay" in lines and "User=elessiah" not in unit
    assert "ExecStart=/home/elessiah/apps/SancturAI/relay/.venv/bin/python relay.py" in lines
    assert "uv" not in re.sub(r"#[^\n]*", "", unit).replace("UMask", "")
    assert "BindReadOnlyPaths=/home/elessiah/apps/SancturAI/relay" in lines
    assert not any(line.startswith(("BindPaths=", "ReadWritePaths=")) for line in lines)
    # §4.1 : le relais ne joint que la boucle locale (bot) et le socket Postgres
    for line in ("IPAddressDeny=any", "IPAddressAllow=localhost", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                 "CapabilityBoundingSet=", "SystemCallFilter=@system-service", "ProtectProc=invisible",
                 "UMask=0077", "MemoryDenyWriteExecute=yes"):
        assert line in lines, line
    # /home masqué (clés SSH, secrets et code des autres services) ; seul le relais est remonté
    assert "ProtectHome=tmpfs" in lines and "ProtectHome=read-only" not in unit
    assert not any(line.startswith("Bind") and ".secrets" in line for line in lines)
    doc = setup_doc()
    assert "useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sancturai-relay" in doc
    assert "uv sync --frozen --no-dev --python /usr/bin/python3" in doc
    tmp = (DEPLOY / "tmpfiles" / "sancturai.conf").read_text(encoding="utf-8")
    assert "d /run/sancturai 2770 10001 www-data -" in tmp


def test_env_example_keys_exist() -> None:
    env = (DEPLOY / "env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"(?m)^(SANCTURAI_[A-Z_]+)=", env))
    for key in ("SANCTURAI_IMAGE", "SANCTURAI_RELAY_DSN", "SANCTURAI_BOT_TOKEN", "SANCTURAI_BOT_URL",
                "SANCTURAI_DB_PASSWORD", "SANCTURAI_RELAY_PASSWORD", "SANCTURAI_INTERNAL_TOKEN"):
        assert key in keys, key
    app_section = env.split("sancturai.env (chmod 600)", 1)[1].split("\n\n", 1)[0]
    for key in re.findall(r"(?m)^SANCTURAI_([A-Z_]+)=", app_section):
        assert key.lower() in Settings.model_fields, key


def test_setup_doc_fits_existing_vhost() -> None:
    doc = (ROOT / "docs" / "ops" / "setup.md").read_text(encoding="utf-8")
    assert f"/etc/nginx/sites-available/{DOMAIN}" in doc
    assert "sudo nginx -t && sudo systemctl reload nginx" in doc
    assert "certbot --nginx" not in doc  # le certificat existe déjà ; certbot réécrirait le fichier
    assert "20G" in doc and "nofail" in doc
    # dossier de secrets partagé : jamais re-chmodé s'il existe
    assert "[ -d ~/apps/.secrets ] || install -d -m 700 ~/apps/.secrets" in doc
    assert "socket.getaddrinfo('example.com', 443)" in doc  # DNS externe en échec (§7)


def test_nginx_body_caps_match_app() -> None:
    """Revue batch 6, I1 : chaque plafond nginx = celui de BodyLimitMiddleware pour la route, et le corps renvoyé
    est octet pour octet celui de l'app (même code, statut, message et conseil : revue batch 6, m4)."""
    conf = nginx()
    tls = next(s for s in blocks(conf, "server") if "listen 443 ssl;" in s)
    locs = blocks(tls, "location")
    server_level = re.sub(r"(?s)\n\s*location .*", "", tls)  # directives avant le premier bloc location
    assert f"client_max_body_size {DEFAULT_BODY_BYTES // 1024}k;" in server_level
    # revue batch 6, N1 : aucun chemin de SancturAI dont dépendrait le démarrage de nginx (donc des autres sites) ;
    # corps ≤ 64 Kio en mémoire, lecture lente coupée vite, envois au vault (sur disque) bornés à 2/IP et 8 au total
    assert "client_body_temp_path" not in conf and "/srv/" not in re.sub(r"#[^\n]*", "", conf)
    assert "client_body_buffer_size 64k;" in server_level and "client_body_timeout 15s;" in server_level
    # R2-I1 : zones propres au vault, clé vide (non comptée) pour les lectures : seuls les envois au vault comptent
    assert "limit_conn_zone $sancturai_vault_upload_ip zone=sancturai_vault_conn:10m;" in conf
    assert "limit_conn_zone $sancturai_vault_upload_all zone=sancturai_vault_total:1m;" in conf
    for var, key in (("ip", "$binary_remote_addr"), ("all", "$server_name")):
        m = re.search(rf"map \$request_method \$sancturai_vault_upload_{var} \{{([^}}]*)\}}", conf)
        assert m is not None, var
        entries = dict(line.split() for line in m.group(1).strip().rstrip(";").split(";"))
        assert entries == {"GET": '""', "HEAD": '""', "OPTIONS": '""', "default": key}, entries

    def directives(block: str) -> set[str]:
        return {line.strip() for line in block.lstrip().splitlines()[1:] if line.strip() and line.strip() != "}"}

    def app_body(method: str, path: str) -> tuple[int, str]:
        err = too_large_error(body_cap(method, path), path)
        return err.status, JSONResponse(err.to_dict()).body.decode()

    assert named_return(locs, "@sancturai_413") == app_body("POST", "/api/login")
    public = next(b for b in locs if b.lstrip().startswith("location / "))
    routes = [("location ^~ /api/vault/ ", "PUT", "/api/vault/a.md"),
              ("location ^~ /api/forum/ ", "POST", "/api/forum/threads"),
              ("location ~ ^/mcp(/|$) ", "POST", "/mcp/")]
    assert {body_cap(m, p) for _, m, p in routes} | {DEFAULT_BODY_BYTES} == {
        16 * 1024 * 1024, 128 * 1024, 256 * 1024, 64 * 1024}
    for opener, method, path in routes:
        loc = next(b for b in locs if b.lstrip().startswith(opener))
        size = re.search(r"client_max_body_size (\w+);", loc)
        assert size is not None and nginx_size(size.group(1)) == body_cap(method, path), opener
        handler = re.search(r"error_page 413 = (@\w+);", loc)
        assert handler is not None and named_return(locs, handler.group(1)) == app_body(method, path), opener
        # même proxy (SSE, en-têtes) et mêmes limiteurs que `location /` (sauf le vault, plus strict) ; un error_page
        # local remplace TOUS ceux hérités du serveur : 429 et 502/504 sont redéclarés
        vault = opener.startswith("location ^~ /api/vault/")
        stricter = {"limit_conn sancturai_vault_conn 2;", "limit_conn sancturai_vault_total 8;"} if vault else set()
        assert directives(public) <= directives(loc), opener  # sancturai_conn 20 partout, vault compris
        assert directives(loc) - directives(public) == {
            size.group(0), handler.group(0), "error_page 429 = @sancturai_429;",
            "error_page 502 504 = @sancturai_busy;", *stricter}, opener
    for loc in locs:
        if re.match(r"\s*location @sancturai_\w*413 ", loc):
            assert "include snippets/security-headers.conf;" in loc and "add_header Cache-Control no-store always;" in loc


def deploy_sh() -> str:
    return (DEPLOY / "deploy.sh").read_text(encoding="utf-8")


def test_deploy_script_is_serialised_and_checks_prerequisites() -> None:
    sh = deploy_sh()
    first_change = sh.index("docker compose pull")
    # revue batch 6, m1 : deux déploiements simultanés ne s'entremêlent pas
    assert "exec 9>/run/lock/sancturai-deploy.lock" in sh and "flock -n 9" in sh
    assert sh.index("flock -n 9") < first_change
    # revue batch 6, I5 : nginx doit réellement écouter 127.0.0.1:8300 (nginx -t ne lie aucun socket)
    assert "ss -ltnpH 'sport = :8300'" in sh and sh.index("sport = :8300") < first_change
    # revue batch 6, m2 : référence `dépôt:étiquette` (un digest @sha256 donnerait une image invalide)
    assert "^[^@]+:[^/:]+$" in sh
    # revue batch 6, m12 : fichiers de secrets lisibles (sinon l'app retomberait sur les valeurs par défaut)
    assert "\nsecrets=/home/elessiah/apps/.secrets\n" in sh
    for name in ("sancturai-db.env", "sancturai.env", "sancturai-relay.env"):
        assert f'"${{secrets}}/{name}"' in sh, name
    assert '[ -r "${f}" ]' in sh and sh.index('[ -r "${f}" ]') < first_change


def test_deploy_script_backs_up_and_rolls_back() -> None:
    sh = deploy_sh()
    # revue batch 6, I2 : sauvegarde juste avant la migration ; m-N2 : sans sauvegarde, pas de migration
    assert sh.index('./backup.sh "pre-') < sh.index("alembic upgrade head")
    assert "dump_rc" not in sh and "|| fail_before_migration" in sh
    # m-N1 : grants ou `up -d app` en échec passent par le même retour arrière que le healthcheck ; ERR notifié
    assert "trap 'notify" in sh and " ERR" in sh
    assert re.search(r"if grants && docker compose up -d app && healthy; then", sh)
    # m-N5 : l'image d'un déploiement raté est supprimée une fois le retour arrière sain
    assert sh.count('docker image rm "${SANCTURAI_IMAGE}"') == 1
    # m-N6 : pas de faux négatif par SIGPIPE sous pipefail
    assert "< <(ss -ltnpH 'sport = :8300')" in sh
    # R2-m3 : la notification d'une sortie imprévue nomme l'image réellement en service
    assert 'serving="${SANCTURAI_IMAGE}"' in sh and "image en service : ${serving}" in sh
    # revue batch 6, I3 : .env réécrit seulement après un healthcheck réussi ; échec → retour à l'image précédente
    health = sh.index("--unix-socket /run/sancturai/app.sock")
    assert sh.index("sed -i") > health
    assert "SANCTURAI_PREVIOUS_IMAGE" in sh
    assert 'SANCTURAI_IMAGE="${current_image}" docker compose up -d --pull never app' in sh
    assert sh.index("--pull never") > health


def test_backup_script_and_timer() -> None:
    sh = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    assert sh.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in sh
    assert "umask 077" in sh and f"install -d -m 700 {DATA}/backups" in sh
    assert "pg_dump -U postgres -Fc" in sh and "mountpoint -q" in sh
    # m-N4 : rotation par étiquette : 7 quotidiennes, 3 d'avant migration (la sauvegarde d'un retour arrière survit)
    assert "keep=7" in sh and "keep=3" in sh and 'tail -n +"$((keep + 1))"' in sh
    assert "^(daily|pre-[0-9a-f]{12})$" in sh
    # même seuil que la garde disque de l'app, taille de la base comprise (m-N3) : une sauvegarde n'affame jamais
    # l'hôte ni Postgres (pg/ est sur le même volume)
    pct = re.search(r"max_used_pct=(\d+)", sh)
    assert pct is not None and int(pct.group(1)) == round(Settings.model_fields["disk_busy_ratio"].default * 100)
    assert "pg_database_size('sancturai')" in sh
    assert ".tmp" in sh and "mv " in sh  # écrite à part puis renommée : jamais de sauvegarde tronquée retenue
    svc = (DEPLOY / "systemd" / "sancturai-backup.service").read_text(encoding="utf-8").splitlines()
    for line in ("Type=oneshot", "ExecStart=/home/elessiah/apps/SancturAI/deploy/backup.sh daily",
                 "Nice=19", "IOSchedulingClass=idle"):
        assert line in svc, line
    timer = (DEPLOY / "systemd" / "sancturai-backup.timer").read_text(encoding="utf-8").splitlines()
    assert "Persistent=true" in timer and any(line.startswith("OnCalendar=") for line in timer)
    doc = setup_doc()
    assert "sancturai-backup.timer" in doc and "pg_restore" in doc and "--single-transaction" in doc


def test_scripts_are_executable_in_git() -> None:
    files = ["deploy/backup.sh", "deploy/deploy.sh", "deploy/postgres/init/01-roles.sh"]
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-s", *files],  # noqa: S607
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(out) == len(files) and all(line.startswith("100755") for line in out), out


def test_setup_doc_operational_checks() -> None:
    doc = setup_doc()
    # I5 : port libre avant l'activation, nginx à l'écoute après le rechargement
    # R2-m1 : seul un autre processus que nginx bloque (le bloc se rejoue pour mettre le vhost à jour)
    pre = "ss -ltnpH 'sport = :8300' | grep -v '\"nginx\"' | grep -q ."
    post = "ss -ltnpH 'sport = :8300' | grep '\"nginx\"' >/dev/null"
    assert doc.index(pre) < doc.index("sudo nginx -t && sudo systemctl reload nginx") < doc.index(post)
    assert "nginx-body" not in doc  # revue batch 6, N1
    # revue batch 6, N2 : une vérification arrête son bloc (sous-shell `set -e` + exit 1) avant l'action dangereuse
    blocks_ = re.findall(r"```bash\n(.*?)```", doc, re.S)

    def guarded(check: str, action: str) -> None:
        block = next(b for b in blocks_ if check in b)
        assert block.startswith("( set -e\n") and "\n)\n" in block, check
        block = block.split("\n)\n", 1)[0]  # le sous-shell seul : la vérification et l'action y figurent toutes deux
        line = next(ln for ln in block.splitlines() if check in ln)
        assert "exit 1" in line, line
        assert block.index(check) < block.index(action), check

    guarded("-ge 30 ]", "sudo fallocate -l 20G")
    guarded(pre, "sudo install -m 644")
    # R2-m2 : le bloc uid/gid 70 n'écrit rien sur la racine si le volume n'est pas monté
    guarded("mountpoint -q /srv/sancturai-data", "sudo install -d -o 70")
    guarded("[ -f /etc/nginx/snippets/security-headers.conf ]", "sudo install -m 644")
    guarded('[ "$(getent group 70 | cut -d: -f1)" = sancturai-pg ]', "sudo install -d -o 70")
    # après le rechargement : nginx absent de 8300 → vhost provisoire remis et arrêt du bloc
    after = doc[doc.index(post):]
    after = after[:after.index("\n)\n")]
    assert "sudo cp /root/sancturai-placeholder.conf.bak" in after and "exit 1" in after
    # m-N7 : le compte du relais doit pouvoir lire le venv (copie, pas de liens durs vers le cache uv d'elessiah)
    assert "UV_LINK_MODE=copy" in doc and "chmod -R o+rX ." in doc
    assert r"find . \( -type d ! -perm -o=rx \) -o \( -type f ! -perm -o=r \) -print" in doc
    # m9, m10, m11
    assert doc.index("df -h /srv") < doc.index("fallocate -l 20G")
    assert "docker login ghcr.io" in doc
    assert "certbot renew --dry-run" in doc and "/etc/nginx/snippets/security-headers.conf" in doc and "2 par IP et 8 au total" in doc
    assert "2.24" in doc and "502" not in doc
    # I3 : retour arrière documenté
    assert "Retour arrière" in doc


def test_ci_builds_image_on_pull_requests() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    # revue batch 6, m13 : un push rapide sur main n'annule pas la publication de l'image du précédent
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.ref != 'refs/heads/main' }}"
    build = ci["jobs"]["build"]
    assert build["if"] == "github.event_name == 'pull_request'"
    step = next(s for s in build["steps"] if s.get("uses", "").startswith("docker/build-push-action"))
    assert step["with"]["push"] is False and step["with"]["platforms"] == "linux/amd64"
