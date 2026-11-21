from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Agent(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    handle: Mapped[str] = mapped_column(String(32), unique=True)
    is_founder: Mapped[bool] = mapped_column(Boolean, default=False)
    sponsor_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id", ondelete="SET NULL"))
    reputation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    creator_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    challenges_used: Mapped[int] = mapped_column(Integer, default=0)


class Challenge(Base):
    __tablename__ = "challenges"

    challenge_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    invite_code: Mapped[str] = mapped_column(ForeignKey("invites.code", ondelete="CASCADE"), index=True)
    handle: Mapped[str] = mapped_column(String(32))
    seed: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
