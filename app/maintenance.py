import asyncio
import logging

from app.forum.notifications import prune_read_notifications
from app.outbox.service import enqueue
from app.state import AppState

log = logging.getLogger("sancturai")


async def run_maintenance_once(state: AppState) -> None:
    alert = state.disk.should_alert()  # ne lève pas (sonde fail-closed) : calculée avant take()
    errors = state.errors.take()
    try:
        async with state.sessionmaker() as db:
            await prune_read_notifications(db)
    except Exception:
        log.exception("notification prune failed")  # n'empêche pas le flush des erreurs ci-dessous
        state.errors.record()  # échec persistant visible par l'admin (compté au tick suivant)
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


async def maintenance_loop(state: AppState, interval: float = 60.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await run_maintenance_once(state)
        except Exception:  # la boucle ne doit jamais mourir
            log.exception("maintenance failed")
