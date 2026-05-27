from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("owner_key", "platform", "course", "title", "due_at", name="uq_assignment_owner_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_key: Mapped[str] = mapped_column(String(120), nullable=False, default="default", index=True)
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


class PlatformCourse(Base):
    __tablename__ = "platform_courses"
    __table_args__ = (
        UniqueConstraint("owner_key", "platform_key", "course_id", name="uq_platform_course_owner_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_key: Mapped[str] = mapped_column(String(120), nullable=False, default="default", index=True)
    platform_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    platform_label: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    course: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    course_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    task_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="discovered")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ManualAssignmentSeries(Base):
    __tablename__ = "manual_assignment_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_key: Mapped[str] = mapped_column(String(120), nullable=False, default="default", index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    recurrence: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    next_due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
