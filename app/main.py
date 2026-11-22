import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin_messages.api import router as admin_router
from app.admin_messages.service import MIN_INTERNAL_TOKEN
from app.config import Settings, get_settings
from app.discovery import router as discovery_router
from app.errors import install_error_handlers
from app.forum.api import router as forum_router
from app.identity.api import router as identity_router
from app.maintenance import maintenance_loop
from app.middleware import InternalGuardMiddleware, NoStoreMiddleware
from app.state import AppState
from app.vault.api import router as vault_router

log = logging.getLogger("sancturai")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if len(settings.internal_token) < MIN_INTERNAL_TOKEN:  # jamais la valeur du token
        log.warning("internal_token is shorter than %d characters: /internal is closed (every request gets 404)",
                    MIN_INTERNAL_TOKEN)
    state = AppState.build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = (asyncio.create_task(maintenance_loop(state, state.settings.maintenance_interval_seconds))
                if state.settings.maintenance_interval_seconds > 0 else None)
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)  # attend la fin avant de fermer le moteur
            await state.engine.dispose()

    app = FastAPI(title="SancturAI", version="0.1.0", lifespan=lifespan)
    app.state.s = state
    install_error_handlers(app, on_internal=state.errors.record)
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(InternalGuardMiddleware, state=state)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(discovery_router)
    app.include_router(identity_router)
    app.include_router(forum_router)
    app.include_router(vault_router)
    app.include_router(admin_router)
    return app
