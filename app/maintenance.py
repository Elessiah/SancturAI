import asyncio
import logging

from sqlalchemy import delete, func, text

from app.identity.models import Challenge
from app.outbox.models import RELAY_MAX_ATTEMPTS, OutboxEvent
from app.outbox.service import enqueue
from app.state import AppState

log = logging.getLogger("sancturai")


async def purge_old_rows(state: AppState) -> None:
    async with state.sessionmaker() as db:
        month_ago = func.now() - text("interval '30 days'")
        await db.execute(delete(OutboxEvent).where(OutboxEvent.sent_at < month_ago))
        # lignes abandonnées par le relais : sinon la table croît sans borne (revue batch 1, I4)
        await db.execute(delete(OutboxEvent).where(OutboxEvent.sent_at.is_(None),
                                                   OutboxEvent.attempts >= RELAY_MAX_ATTEMPTS,
                                                   OutboxEvent.created_at < month_ago))
        await db.execute(delete(Challenge).where(Challenge.expires_at < func.now() - text("interval '1 day'")))
        await db.commit()


async def run_maintenance_once(state: AppState) -> None:
    await purge_old_rows(state)
    alert = state.disk.should_alert()  # ne lève pas (sonde fail-closed) : calculée avant take()
    errors = state.errors.take()
    if not errors and not alert:
        return
    try:
        async with state.sessionmaker() as db:
            if errors:
                await enqueue(db, "log", f"{errors} erreur(s) 5xx sur la dernière minute.")
            if alert:
                await enqueue(db, "log", f"Alerte disque : volume de données au-delà de {state.disk.ratio * 100:.0f} % (ou sonde en échec), écritures refusées.")
            await db.commit()
    except Exception:
        state.errors.restore(errors)  # ne pas perdre le compte si la base est indisponible
        if alert:
            state.disk.rearm_alert()
        raise


async def maintenance_loop(state: AppState, interval: float = 60.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await run_maintenance_once(state)
        except Exception:  # la boucle ne doit jamais mourir
            log.exception("maintenance failed")
