from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
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
