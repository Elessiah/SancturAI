from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class VaultFile(Base):
    __tablename__ = "vault_files"
    __table_args__ = (CheckConstraint("size >= 0", name="ck_vault_files_size_nonneg"),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True)
    path_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    path_enc: Mapped[bytes] = mapped_column(LargeBinary)
    content_enc: Mapped[bytes] = mapped_column(LargeBinary)
    size: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
