import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import make_engine
from app.error_aggregator import ErrorAggregator


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    argon_sem: asyncio.Semaphore
    errors: ErrorAggregator = field(default_factory=ErrorAggregator)

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        engine = make_engine(settings.database_url)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            argon_sem=asyncio.Semaphore(settings.argon2_concurrency),
        )

    def reset_memory(self) -> None:
        """Réinitialise l'état en mémoire (utilisé par les tests). Étendu par les tâches suivantes."""
        self.errors.take()


def get_state(request: Request) -> AppState:
    return request.app.state.s  # type: ignore[no-any-return]


async def get_db(state: AppState = Depends(get_state)) -> AsyncIterator[AsyncSession]:
    async with state.sessionmaker() as session:
        yield session
