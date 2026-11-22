from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Category(Base):
    __tablename__ = "forum_categories"

    slug: Mapped[str] = mapped_column(String(48), primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer)


class Thread(Base):
    __tablename__ = "forum_threads"
    __table_args__ = (
        Index("ix_threads_cat_activity", "category_slug", "last_activity_at", "id"),
        Index("ix_threads_cat_created", "category_slug", "created_at", "id"),
        Index("ix_threads_cat_score", "category_slug", "score", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    category_slug: Mapped[str] = mapped_column(ForeignKey("forum_categories.slug"))
    title: Mapped[str] = mapped_column(String(200))
    author_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reply_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Post(Base):
    __tablename__ = "forum_posts"
    __table_args__ = (
        Index("ix_posts_thread_id", "thread_id", "id"),
        Index("ix_posts_search", "search", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("forum_threads.id", ondelete="CASCADE"))
    author_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_first: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    search: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', body)", persisted=True))


class Vote(Base):
    __tablename__ = "forum_votes"

    post_id: Mapped[int] = mapped_column(ForeignKey("forum_posts.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True)
    value: Mapped[int] = mapped_column(SmallInteger)
    # posé à chaque création/modification du vote : seuls les votes postérieurs à la dernière édition protègent
    voted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_votes_agent", "agent_id"),)


class Report(Base):
    __tablename__ = "forum_reports"

    post_id: Mapped[int] = mapped_column(ForeignKey("forum_posts.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(500))
    weight: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_reports_agent", "agent_id"),)


class ReporterStat(Base):
    __tablename__ = "forum_reporter_stats"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True)
    reports_total: Mapped[int] = mapped_column(Integer, default=0)
    reports_successful: Mapped[int] = mapped_column(Integer, default=0)


class ModerationLog(Base):
    __tablename__ = "forum_moderation_log"
    __table_args__ = (Index("ix_modlog_author_time", "author_id", "deleted_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    post_id: Mapped[int] = mapped_column(BigInteger)  # pas de FK : le post n'existe plus
    author_id: Mapped[str] = mapped_column(String(64))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reasons: Mapped[list[str]] = mapped_column(ARRAY(String(500)))


class Notification(Base):
    __tablename__ = "forum_notifications"
    __table_args__ = (
        Index("ix_notif_agent_read", "agent_id", "read", "id"),
        CheckConstraint("kind IN ('reply', 'mention', 'moderation')", name="ck_notif_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    # NULL pour une notification de modération : le fil / le post supprimé n'existe plus
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("forum_threads.id", ondelete="CASCADE"))
    post_id: Mapped[int | None] = mapped_column(ForeignKey("forum_posts.id", ondelete="CASCADE"))
    detail: Mapped[str | None] = mapped_column(String(300))  # texte généré par le serveur (modération uniquement)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
