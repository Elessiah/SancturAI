from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.errors import install_error_handlers
from app.state import AppState


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    state = AppState.build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await state.engine.dispose()

    app = FastAPI(title="SancturAI", version="0.1.0", lifespan=lifespan)
    app.state.s = state
    install_error_handlers(app, on_internal=state.errors.record)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
