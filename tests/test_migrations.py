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
    # symétrie des downgrades : tout redescendre puis remonter doit redonner le même schéma
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        diff = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
        checks = set((await conn.execute(text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))).scalars())
        indexes = set((await conn.execute(text("SELECT indexname FROM pg_indexes"))).scalars())
    await engine.dispose()
    assert diff == [], f"migration manquante : {diff}"
    # compare_metadata ignore les CHECK : on vérifie leur présence à part
    expected = {c.name for t in Base.metadata.tables.values() for c in t.constraints
                if isinstance(c, CheckConstraint)}
    assert expected <= checks, f"CHECK manquants : {expected - checks}"
    assert {"ck_vault_files_size_nonneg", "ck_outbox_attempts_nonneg", "ck_outbox_kind", "ck_notif_kind"} <= checks
    # revue batch 2, M4 : garde-fous en base sur les compteurs et valeurs du forum
    assert {"ck_votes_value", "ck_threads_reply_count_nonneg", "ck_reporter_stats_nonneg",
            "ck_notif_moderation_shape"} <= checks
    # revue batch 2, I4 (cascades ON DELETE sans index = parcours séquentiel par post supprimé) ; M5 (purge)
    assert {"ix_notif_thread", "ix_notif_post", "ix_notif_prune", "ix_notif_prune_unread"} <= indexes
    assert {"ck_admin_threads_short_id", "ck_admin_msg_sender"} <= checks
    # spec visibilité §7 : procès, signalements d'agent, votes de procès
    assert {"ck_trials_status", "ck_trials_trigger", "ck_trials_verdict", "ck_trials_closed_by",
            "ck_trials_closed_shape", "ck_trial_votes_verdict", "ck_reporter_stats_agent_range"} <= checks
    assert {"uq_trials_open_accused", "ix_trials_status_deadline", "uq_agent_reports_pending",
            "ix_agent_reports_target_pending", "ix_trial_votes_voter"} <= indexes
