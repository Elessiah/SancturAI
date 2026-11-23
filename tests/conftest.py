import asyncio
import socket
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import uvicorn
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

import app.db_models  # noqa: F401
from app.config import Settings
from app.db import Base
from app.forum.seed import seed_categories
from app.main import create_app
from app.state import AppState
from app.tribunal.seed import seed_tribunal
from tests.helpers import reset_key_pool_cursor


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def settings(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        database_url=pg_url,
        internal_token="test-internal-token-0123456789abcdef",  # >= 32 caractères
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
        await seed_categories(conn)
        await seed_tribunal(conn)
    async with LifespanManager(application):
        yield application


@pytest.fixture
def state(app: FastAPI) -> AppState:
    return app.state.s  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
async def _clean(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    reset_key_pool_cursor()
    yield
    if "app" not in request.fixturenames:
        return
    st: AppState = request.getfixturevalue("app").state.s
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    if tables:
        async with st.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            await seed_categories(conn)
            await seed_tribunal(conn)
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


@pytest.fixture(scope="session")
async def live_url(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 30
    while not server.started:  # noqa: ASYNC110
        if task.done() or asyncio.get_running_loop().time() > deadline:  # échec de bind : échoue, ne bloque pas
            server.should_exit = True
            raise RuntimeError("live test server did not start")
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task
