import re
import subprocess
from pathlib import Path

import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DOMAIN = "sancturai.bluegenji-esport.fr"


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


def test_hardening_and_limits() -> None:
    for name, svc in compose()["services"].items():
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cgroup_parent"] == "sancturai.slice", name
        assert svc["oom_score_adj"] == 1000, name
        assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name
        assert "mem_limit" in svc and "cpus" in svc and "pids_limit" in svc, name
    app = compose()["services"]["app"]
    assert app["read_only"] is True and app["user"] == "10001:33"
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
    # revue batch 1, M13 : 5 Mo de texte non ASCII échappé en JSON (\uXXXX) dépasse 8 Mo
    assert "client_max_body_size 16m;" in conf
    assert "error_page 413 = @sancturai_413;" in conf and '"error":"QUOTA_EXCEEDED"' in conf
    sl = (DEPLOY / "systemd" / "sancturai.slice").read_text(encoding="utf-8")
    for line in ("CPUWeight=20", "CPUQuota=150%", "MemoryHigh=1.2G", "MemoryMax=1.5G", "IOWeight=20"):
        assert line in sl


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
    assert "client_max_body_size 16m;" in tls[0] and "location /internal/" in tls[0]


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


def test_nginx_json_errors() -> None:
    conf = nginx()
    assert "message\":\"request body larger than 16 MiB\"" in conf
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
    assert "PASSWORD :'db_pw'" in sh and "PASSWORD :'relay_pw'" in sh
    heredoc = sh.split("<<'SQL'", 1)[1]
    assert "${" not in heredoc  # heredoc littéral : aucun mot de passe interpolé dans le SQL


def test_deploy_script_grants_and_healthcheck() -> None:
    sh = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    assert sh.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in sh
    assert "GRANT USAGE ON SCHEMA public TO relay; GRANT SELECT, UPDATE ON outbox TO relay;" in sh
    assert sh.index("alembic upgrade head") < sh.index("GRANT USAGE") < sh.index("docker compose up -d app")
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
    assert "cache-from: type=gha" in ci and "cache-to: type=gha,mode=max" in ci
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".git", ".venv", "tests", "deploy", "relay", "*.env", ".env"):
        assert entry in ignore, entry


def test_relay_unit() -> None:
    unit = (DEPLOY / "systemd" / "sancturai-relay.service").read_text(encoding="utf-8")
    for line in ("Slice=sancturai.slice", "User=elessiah", "NoNewPrivileges=yes", "ProtectSystem=strict",
                 "EnvironmentFile=/home/elessiah/apps/.secrets/sancturai-relay.env", "MemoryMax=128M"):
        assert line in unit, line
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


def test_scripts_are_executable_in_git() -> None:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-s", "deploy/deploy.sh", "deploy/postgres/init/01-roles.sh"],  # noqa: S607
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(out) == 2 and all(line.startswith("100755") for line in out), out
