import asyncio
import logging

from app.outbox.service import enqueue
from app.state import AppState

log = logging.getLogger("sancturai")


async def run_maintenance_once(state: AppState) -> None:
    errors = state.errors.take()
    alert = state.disk.should_alert()
    if not errors and not alert:
        return
    async with state.sessionmaker() as db:
        if errors:
            await enqueue(db, "log", f"{errors} erreur(s) 5xx sur la dernière minute.")
        if alert:
            await enqueue(db, "log", "Alerte disque : volume de données au-delà de 85 %, écritures refusées.")
        await db.commit()


async def maintenance_loop(state: AppState, interval: float = 60.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await run_maintenance_once(state)
        except Exception:  # la boucle ne doit jamais mourir
            log.exception("maintenance failed")
