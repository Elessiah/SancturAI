import asyncio
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import CheckConstraint, text
from sqlalchemy.ext.asyncio import create_async_engine

import app.db_models  # noqa: F401
from app.db import Base

ROOT = Path(__file__).resolve().parents[1]


async def test_migrations_match_models(pg_url: str) -> None:
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text("DROP DATABASE IF EXISTS migcheck"))
        await conn.execute(text("CREATE DATABASE migcheck"))
    await admin.dispose()

    url = pg_url.rsplit("/", 1)[0] + "/migcheck"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        diff = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
        checks = set((await conn.execute(text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))).scalars())
    await engine.dispose()
    assert diff == [], f"migration manquante : {diff}"
    # compare_metadata ignore les CHECK : on vérifie leur présence à part
    expected = {c.name for t in Base.metadata.tables.values() for c in t.constraints
                if isinstance(c, CheckConstraint)}
    assert expected <= checks, f"CHECK manquants : {expected - checks}"
