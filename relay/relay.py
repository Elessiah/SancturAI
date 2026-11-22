"""Relais SancturAI : outbox (Postgres, socket Unix) → API interne du bot Discord. Seul pont sortant."""
import json
import logging
import os
import re
import time
import unicodedata
from typing import Any

import httpx
import psycopg

log = logging.getLogger("sancturai-relay")
BATCH = 20
MAX_ATTEMPTS = 60  # = app.outbox.models.RELAY_MAX_ATTEMPTS ; avec le plafond 1 h : ≈ 50 h de panne bot tolérées
BACKOFF_BASE, BACKOFF_CAP = 5, 3600
HTTP_TIMEOUT = 10
LEASE_SECONDS = BATCH * HTTP_TIMEOUT + 30  # un relais mort libère ses événements après ce délai
# Frontière de privilèges : les lignes de l'outbox viennent de l'app (bac à sable) et le texte des agents.
# Bornes alignées sur app.outbox.service.MAX_TEXT et le contrat du bot ; tout écart => abandon sans envoi.
MAX_TEXT = {"log": 1800, "agent_message": 4000}
MAX_PAYLOAD_BYTES = 32_768  # mesuré côté Postgres : une ligne géante n'est jamais chargée sur l'hôte
AGENT_ID_MAX = 64
HANDLE_RE = re.compile(r"[a-z0-9_-]{3,32}")
SHORT_ID_RE = re.compile(r"[0-9a-f]{4}")
BATCH_BUDGET = LEASE_SECONDS - 30  # au-delà, on arrête le lot : les bails restants expirent et les lignes sont reprises

# Réservation (bail) : une seule requête courte met next_attempt_at dans le futur, puis chaque
# événement est réglé dans sa propre transaction autocommit. Aucun verrou de ligne n'est tenu pendant
# les appels HTTP (sinon : transaction ouverte jusqu'à BATCH x HTTP_TIMEOUT s, horizon xmin bloqué,
# et tous les envois rejoués si la connexion tombe en cours de lot).
CLAIM_PENDING = """
UPDATE outbox SET next_attempt_at = now() + make_interval(secs => %s)
WHERE id IN (
  SELECT id FROM outbox
  WHERE sent_at IS NULL AND attempts < %s AND next_attempt_at <= now()
  ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
RETURNING id, kind, CASE WHEN octet_length(payload::text) <= %s THEN payload::text END, attempts, next_attempt_at
"""
# Les MARK_* ne s'appliquent que si le bail posé par CLAIM_PENDING est toujours le nôtre (next_attempt_at inchangé) :
# un lot trop lent dont le bail a expiré (et qu'un autre relais a repris) ne peut plus écraser l'état.
MARK_SENT = "UPDATE outbox SET sent_at = now() WHERE id = %s AND next_attempt_at = %s"
MARK_FAILED = """
UPDATE outbox SET attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => %s)
WHERE id = %s AND next_attempt_at = %s
"""
MARK_ABANDONED = "UPDATE outbox SET attempts = %s WHERE id = %s AND next_attempt_at = %s"


def backoff_seconds(attempts: int) -> int:
    """Attente après un échec, `attempts` = nombre d'échecs déjà comptés (exponentielle plafonnée à 1 h)."""
    return BACKOFF_CAP if attempts >= 12 else min(BACKOFF_CAP, BACKOFF_BASE * 2**attempts)


def _bounded_str(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid field")
    return value


def _no_format_chars(value: str) -> bool:
    return not any(unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} for c in value)


def format_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Corps envoyé au bot : liste blanche stricte de champs typés et bornés (jamais le JSONB brut).

    Reprend le contrat du bot (texte non vide, short_id 4 hex minuscules, handle, agent_id sans caractères
    de contrôle/format). Lève ValueError si la ligne ne le respecte pas (le message ne contient aucune donnée).
    """
    if kind not in MAX_TEXT or not isinstance(payload, dict):
        raise ValueError("invalid event")
    text = _bounded_str(payload.get("text"), MAX_TEXT[kind])
    if not text.strip():
        raise ValueError("empty text")
    out: dict[str, Any] = {"kind": kind, "text": text}
    if kind == "agent_message":
        short_id = _bounded_str(payload.get("short_id"), 4)
        agent = payload.get("agent")
        if not isinstance(agent, dict) or not SHORT_ID_RE.fullmatch(short_id):
            raise ValueError("invalid agent message")
        handle = _bounded_str(agent.get("handle"), 32)
        agent_id = _bounded_str(agent.get("agent_id"), AGENT_ID_MAX)
        if not HANDLE_RE.fullmatch(handle) or not agent_id or not _no_format_chars(agent_id):
            raise ValueError("invalid agent")
        out["short_id"] = short_id
        out["agent"] = {"handle": handle, "agent_id": agent_id}
    return out


def _send(http: httpx.Client, bot_url: str, token: str, body: dict[str, Any]) -> int | None:
    try:
        # stream : seul l'en-tête de statut est lu (le corps de réponse est ignoré, jamais bufferisé)
        with http.stream("POST", bot_url, json=body, headers={"x-internal-token": token}) as response:
            return response.status_code
    except httpx.HTTPError:
        return None


def _settle(conn: psycopg.Connection, event_id: int, kind: str, text_json: str | None, attempts: int, lease: object,
            http: httpx.Client, bot_url: str, token: str) -> None:
    try:
        if text_json is None:
            raise ValueError("payload too large")
        body = format_payload(kind, json.loads(text_json))
    except (ValueError, RecursionError):
        # ligne hors contrat ou non décodable (app compromise ?) : abandon sans envoi ; seul l'id est journalisé
        conn.execute(MARK_ABANDONED, (MAX_ATTEMPTS, event_id, lease))
        log.warning("event %s violates the notify contract, abandoning", event_id)
        return
    status = _send(http, bot_url, token, body)
    if status == 200:
        conn.execute(MARK_SENT, (event_id, lease))
    elif status == 400:
        # charge utile invalide : permanent, inutile de réessayer (le texte n'est jamais journalisé)
        conn.execute(MARK_ABANDONED, (MAX_ATTEMPTS, event_id, lease))
        log.warning("event %s kind=%s rejected by bot (400), abandoning", event_id, kind)
    else:
        conn.execute(MARK_FAILED, (backoff_seconds(attempts), event_id, lease))


def process_batch(conn: psycopg.Connection, http: httpx.Client, bot_url: str, token: str) -> int:
    """Traite un lot (`conn` en autocommit) ; renvoie le nombre d'événements réservés."""
    rows = sorted(conn.execute(CLAIM_PENDING, (LEASE_SECONDS, MAX_ATTEMPTS, BATCH, MAX_PAYLOAD_BYTES)).fetchall())
    deadline = time.monotonic() + BATCH_BUDGET
    for event_id, kind, text_json, attempts, lease in rows:
        if time.monotonic() > deadline:
            log.warning("batch budget exhausted, remaining leases will expire")
            break
        try:
            _settle(conn, event_id, kind, text_json, attempts, lease, http, bot_url, token)
        except psycopg.OperationalError:
            raise  # base indisponible : main() reconnecte
        except Exception:
            # filet : aucune ligne ne doit pouvoir tuer la boucle ni laisser son lot sous bail (journal sans donnée de ligne)
            log.error("event %s failed unexpectedly, backing off", event_id)
            try:
                conn.execute(MARK_FAILED, (backoff_seconds(attempts), event_id, lease))
            except psycopg.OperationalError:
                raise
            except Exception:
                log.error("event %s could not be marked failed", event_id)
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    dsn = os.environ["SANCTURAI_RELAY_DSN"]
    bot_url = os.environ.get("SANCTURAI_BOT_URL", "http://127.0.0.1:4400/internal/sancturai/notify")
    token = os.environ.get("SANCTURAI_BOT_TOKEN", "")
    if not token.strip():  # sinon chaque envoi prend un 401 et brûle des tentatives
        raise SystemExit("SANCTURAI_BOT_TOKEN is required")
    interval = float(os.environ.get("SANCTURAI_RELAY_INTERVAL", "5"))
    # trust_env=False : ni HTTP(S)_PROXY/ALL_PROXY ni .netrc, le jeton et le texte ne vont qu'au bot local
    with httpx.Client(timeout=HTTP_TIMEOUT, trust_env=False) as http:
        while True:
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    while True:
                        if process_batch(conn, http, bot_url, token) < BATCH:  # lot plein : arriéré, on enchaîne
                            time.sleep(interval)
            except psycopg.OperationalError:
                log.warning("database unavailable, retrying in 30s")
                time.sleep(30)


if __name__ == "__main__":
    main()
