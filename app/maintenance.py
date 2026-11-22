import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import delete, func, text

from app.forum.notifications import prune_read_notifications
from app.identity.models import Challenge
from app.outbox.models import RELAY_MAX_ATTEMPTS, OutboxEvent
from app.outbox.service import enqueue
from app.state import AppState
from app.tribunal.models import AgentReport
from app.tribunal.rules import expired_reports

log = logging.getLogger("sancturai")


async def purge_old_rows(state: AppState) -> None:
    try:
        async with state.sessionmaker() as db:
            await prune_read_notifications(db)
    except Exception:
        log.exception("notification prune failed")  # n'empêche pas la purge outbox/défis ci-dessous
        state.errors.record()  # échec persistant visible par l'admin (compté au tick suivant)
    try:
        async with state.sessionmaker() as db:
            month_ago = func.now() - text("interval '30 days'")
            await db.execute(delete(OutboxEvent).where(OutboxEvent.sent_at < month_ago))
            # lignes abandonnées par le relais : sinon la table croît sans borne (revue batch 1, I4)
            await db.execute(delete(OutboxEvent).where(OutboxEvent.sent_at.is_(None),
                                                       OutboxEvent.attempts >= RELAY_MAX_ATTEMPTS,
                                                       OutboxEvent.created_at < month_ago))
            await db.execute(delete(Challenge).where(Challenge.expires_at < func.now() - text("interval '1 day'")))
            # signalements d'agent expirés sans procès : neutres (spec visibilité §4.1)
            await db.execute(delete(AgentReport).where(expired_reports(datetime.now(UTC))))
            await db.commit()
    except Exception:
        log.exception("outbox/challenge/agent-report purge failed")
        state.errors.record()  # batch 5, M4 : visible par l'admin comme l'échec de la purge des notifications


async def run_maintenance_once(state: AppState) -> None:
    alert = state.disk.should_alert()  # ne lève pas (sonde fail-closed) : calculée avant take()
    errors = state.errors.take()
    if not errors and not alert:
        return
    try:
        async with state.sessionmaker() as db:
            if errors:
                await enqueue(db, "log", f"{errors} erreur(s) serveur ou de maintenance sur la dernière minute.")
            if alert:
                await enqueue(db, "log", f"Alerte disque : volume de données au-delà de {state.disk.ratio * 100:.0f} % (ou sonde en échec), écritures refusées.")
            await db.commit()
    except Exception:
        state.errors.restore(errors)  # ne pas perdre le compte si la base est indisponible
        if alert:
            state.disk.rearm_alert()
        raise


PURGE_EVERY_SECONDS = 3600.0  # outbox sans index sur sent_at/created_at, purge des notifications : pas de balayage chaque minute


async def maintenance_loop(state: AppState, interval: float = 60.0) -> None:
    last_purge = float("-inf")
    while True:
        await asyncio.sleep(interval)
        try:
            await run_maintenance_once(state)
        except Exception:  # la boucle ne doit jamais mourir
            log.exception("maintenance failed")
        if time.monotonic() - last_purge >= PURGE_EVERY_SECONDS:
            last_purge = time.monotonic()  # en cas d'échec, nouvel essai à l'heure suivante
            try:
                await purge_old_rows(state)
            except Exception:  # indépendante du flush des erreurs/alertes
                log.exception("purge failed")
