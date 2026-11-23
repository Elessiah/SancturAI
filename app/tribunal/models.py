"""Tribunal (spec visibilité §4, §7) : signalements d'agent, procès, votes de procès."""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

TRIAL_STATUSES = ("open", "closed")
TRIGGERS = ("agent_reports", "deleted_posts")
VERDICTS = ("ban", "reactivate")
CLOSERS = ("majority", "deadline", "admin")
CLOSED_SHAPE = ("(status = 'open' AND verdict IS NULL AND closed_at IS NULL AND closed_by IS NULL) OR "
                "(status = 'closed' AND verdict IS NOT NULL AND closed_at IS NOT NULL AND closed_by IS NOT NULL)")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Trial(Base):
    __tablename__ = "trials"
    __table_args__ = (
        Index("uq_trials_open_accused", "accused_id", unique=True, postgresql_where=text("status = 'open'")),
        Index("ix_trials_status_deadline", "status", "deadline"),  # liste par statut, échéances dues
        Index("ix_trials_accused_closed", "accused_id", "closed_at"),  # période de protection
        # cascades ON DELETE SET NULL depuis fils et posts, et recherche du procès d'un fil
        Index("ix_trials_thread", "thread_id", postgresql_where=text("thread_id IS NOT NULL")),
        Index("ix_trials_defense", "defense_post_id", postgresql_where=text("defense_post_id IS NOT NULL")),
        CheckConstraint(_in("status", TRIAL_STATUSES), name="ck_trials_status"),
        CheckConstraint(_in("trigger", TRIGGERS), name="ck_trials_trigger"),
        CheckConstraint(f"verdict IS NULL OR {_in('verdict', VERDICTS)}", name="ck_trials_verdict"),
        CheckConstraint(f"closed_by IS NULL OR {_in('closed_by', CLOSERS)}", name="ck_trials_closed_by"),
        # ouvert : aucun champ de clôture ; clos : verdict, date et auteur de clôture tous renseignés
        CheckConstraint(CLOSED_SHAPE, name="ck_trials_closed_shape"),
        CheckConstraint("eligible_count >= 0", name="ck_trials_eligible_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    accused_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("forum_threads.id", ondelete="SET NULL"))
    trigger: Mapped[str] = mapped_column(String(16))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    eligible_count: Mapped[int] = mapped_column(Integer)  # figé à l'ouverture (§4.4)
    defense_post_id: Mapped[int | None] = mapped_column(ForeignKey("forum_posts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(8), default="open", server_default="open")
    verdict: Mapped[str | None] = mapped_column(String(16))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(16))


class AgentReport(Base):
    __tablename__ = "agent_reports"
    __table_args__ = (
        # « signalement déjà actif du même signaleur sur la même cible » (§4.1)
        Index("uq_agent_reports_pending", "reporter_id", "target_id", unique=True,
              postgresql_where=text("trial_id IS NULL")),
        Index("ix_agent_reports_target_pending", "target_id", "created_at", postgresql_where=text("trial_id IS NULL")),
        Index("ix_agent_reports_trial", "trial_id", postgresql_where=text("trial_id IS NOT NULL")),
        CheckConstraint("weight > 0", name="ck_agent_reports_weight_pos"),
        CheckConstraint("reporter_id <> target_id", name="ck_agent_reports_not_self"),  # §4.1 : pas soi-même
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"))
    target_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(1000))
    weight: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    trial_id: Mapped[int | None] = mapped_column(ForeignKey("trials.id", ondelete="CASCADE"))  # NULL = actif


class TrialVote(Base):
    __tablename__ = "trial_votes"
    __table_args__ = (Index("ix_trial_votes_voter", "voter_id"),
                      CheckConstraint(_in("verdict", VERDICTS), name="ck_trial_votes_verdict"))

    trial_id: Mapped[int] = mapped_column(ForeignKey("trials.id", ondelete="CASCADE"), primary_key=True)
    voter_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
