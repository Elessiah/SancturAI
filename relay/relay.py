"""Relais SancturAI : outbox (Postgres, socket Unix) → API interne du bot Discord. Seul pont sortant."""
import logging
import os
import time
from typing import Any

import httpx
import psycopg

log = logging.getLogger("sancturai-relay")
BATCH = 20
MAX_ATTEMPTS = 60  # = app.outbox.models.RELAY_MAX_ATTEMPTS ; avec le plafond 1 h : ≈ 50 h de panne bot tolérées
BACKOFF_BASE, BACKOFF_CAP = 5, 3600
HTTP_TIMEOUT = 10
LEASE_SECONDS = BATCH * HTTP_TIMEOUT + 30  # un relais mort libère ses événements après ce délai

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
RETURNING id, kind, payload, attempts
"""
MARK_SENT = "UPDATE outbox SET sent_at = now() WHERE id = %s"
MARK_FAILED = """
UPDATE outbox SET attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => %s)
WHERE id = %s
"""
MARK_ABANDONED = "UPDATE outbox SET attempts = %s WHERE id = %s"


def backoff_seconds(attempts: int) -> int:
    """Attente après un échec, `attempts` = nombre d'échecs déjà comptés (exponentielle plafonnée à 1 h)."""
    return BACKOFF_CAP if attempts >= 12 else min(BACKOFF_CAP, BACKOFF_BASE * 2**attempts)


def format_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "text": str(payload.get("text", ""))}
    if kind == "agent_message":
        out["short_id"] = payload.get("short_id")
        out["agent"] = payload.get("agent")
    return out


def process_batch(conn: psycopg.Connection, http: httpx.Client, bot_url: str, token: str) -> int:
    """Traite un lot (`conn` en autocommit) ; renvoie le nombre d'événements réservés."""
    rows = sorted(conn.execute(CLAIM_PENDING, (LEASE_SECONDS, MAX_ATTEMPTS, BATCH)).fetchall())
    for event_id, kind, payload, attempts in rows:
        status: int | None
        try:
            response = http.post(bot_url, json=format_payload(kind, payload),
                                 headers={"x-internal-token": token})
            status = response.status_code
        except httpx.HTTPError:
            status = None
        if status == 200:
            conn.execute(MARK_SENT, (event_id,))
        elif status == 400:
            # charge utile invalide : permanent, inutile de réessayer (le texte n'est jamais journalisé)
            conn.execute(MARK_ABANDONED, (MAX_ATTEMPTS, event_id))
            log.warning("event %s kind=%s rejected by bot (400), abandoning", event_id, kind)
        else:
            conn.execute(MARK_FAILED, (backoff_seconds(attempts), event_id))
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    dsn = os.environ["SANCTURAI_RELAY_DSN"]
    bot_url = os.environ.get("SANCTURAI_BOT_URL", "http://127.0.0.1:4400/internal/sancturai/notify")
    token = os.environ["SANCTURAI_BOT_TOKEN"]
    interval = float(os.environ.get("SANCTURAI_RELAY_INTERVAL", "5"))
    with httpx.Client(timeout=HTTP_TIMEOUT) as http:
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
