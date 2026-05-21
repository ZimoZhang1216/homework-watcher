from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("platform", "course", "title", "due_at", name="uq_assignment_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    course: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status_raw: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    status_normalized: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_key: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_todo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")
