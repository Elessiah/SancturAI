import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import make_engine
from app.disk import DiskGuard
from app.error_aggregator import ErrorAggregator
from app.identity.sessions import SessionStore
from app.ratelimit import RateLimiter
from app.status import StatusCache


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    argon_sem: asyncio.Semaphore
    disk: DiskGuard
    errors: ErrorAggregator = field(default_factory=ErrorAggregator)
    sessions: SessionStore = field(default_factory=lambda: SessionStore(86_400))
    limiter: RateLimiter = field(default_factory=RateLimiter)
    status: StatusCache = field(default_factory=StatusCache)

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        engine = make_engine(settings.database_url)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            argon_sem=asyncio.Semaphore(settings.argon2_concurrency),
            sessions=SessionStore(settings.session_ttl_seconds),
            disk=DiskGuard(settings.data_path, settings.disk_busy_ratio),
        )

    def reset_memory(self) -> None:
        """Réinitialise l'état en mémoire (utilisé par les tests). Étendu par les tâches suivantes."""
        self.errors.take()
        self.sessions.clear()
        self.limiter.clear()
        self.status.clear()


def get_state(request: Request) -> AppState:
    return request.app.state.s  # type: ignore[no-any-return]


async def get_db(state: AppState = Depends(get_state)) -> AsyncIterator[AsyncSession]:
    async with state.sessionmaker() as session:
        yield session
