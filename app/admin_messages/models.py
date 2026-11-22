from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AdminThread(Base):
    __tablename__ = "admin_threads"
    __table_args__ = (CheckConstraint("short_id ~ '^[0-9a-f]{4}$'", name="ck_admin_threads_short_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    short_id: Mapped[str] = mapped_column(String(4), unique=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminMessage(Base):
    __tablename__ = "admin_messages"
    __table_args__ = (
        Index("ix_admin_msg_thread", "thread_id", "id"),
        CheckConstraint("sender IN ('agent', 'admin')", name="ck_admin_msg_sender"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("admin_threads.id", ondelete="CASCADE"))
    sender: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_by_agent: Mapped[bool] = mapped_column(Boolean, default=True)
