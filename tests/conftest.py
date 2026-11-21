from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.postgres import PostgresContainer

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.state import AppState


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def settings(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        database_url=pg_url,
        internal_token="test-internal-token",
        data_path=str(tmp_path_factory.mktemp("data")),
        allowed_hosts=["test", "localhost", "127.0.0.1:*"],
        disk_busy_ratio=1.0,  # indépendant du remplissage réel du disque de la machine
        maintenance_interval_seconds=0,  # pas de boucle de fond pendant les tests
    )


@pytest.fixture(scope="session")
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    state: AppState = application.state.s
    async with state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with LifespanManager(application):
        yield application


@pytest.fixture
def state(app: FastAPI) -> AppState:
    return app.state.s  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
async def _clean(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "app" not in request.fixturenames:
        return
    st: AppState = request.getfixturevalue("app").state.s
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    if tables:
        async with st.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    st.reset_memory()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def db(state: AppState) -> AsyncIterator[AsyncSession]:
    async with state.sessionmaker() as session:
        yield session
