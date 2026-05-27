from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .candidates import AssignmentCandidate
from .config_loader import KnownCourseConfig
from .models import Assignment, Base, PlatformCourse
from .settings import Settings, load_settings
from .status import TODO_STATUSES


DEFAULT_OWNER_KEY = "default"
ASSIGNMENT_COPY_COLUMNS = [
    "id",
    "platform",
    "course",
    "title",
    "status_raw",
    "status_normalized",
    "due_at",
    "url",
    "source_key",
    "fingerprint",
    "is_todo",
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
    "raw_snapshot",
]


@dataclass(frozen=True)
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class CourseUpsertStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


def create_db_engine(settings: Settings | None = None) -> Engine:
    active_settings = settings or load_settings()
    ensure_parent_dir(active_settings.database_path)
    return create_engine(
        f"sqlite:///{active_settings.database_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )


def create_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(create_db_engine(settings), expire_on_commit=False, future=True)


def init_db(settings: Settings | None = None) -> None:
    engine = create_db_engine(settings)
    migrate_assignment_owner_key(engine)
    Base.metadata.create_all(engine)


def migrate_assignment_owner_key(engine: Engine) -> None:
    inspector = inspect(engine)
    if "assignments" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("assignments")}
    if "owner_key" in columns:
        return

    legacy_table = "assignments_legacy_owner_migration"
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {legacy_table}"))
        connection.execute(text(f"ALTER TABLE assignments RENAME TO {legacy_table}"))

    Base.metadata.create_all(engine)

    copy_columns = [column for column in ASSIGNMENT_COPY_COLUMNS if column in columns]
    insert_columns = ["owner_key", *copy_columns]
    select_columns = [f"'{DEFAULT_OWNER_KEY}'", *copy_columns]
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO assignments ({', '.join(insert_columns)}) "
                f"SELECT {', '.join(select_columns)} FROM {legacy_table}"
            )
        )
        connection.execute(text(f"DROP TABLE {legacy_table}"))


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def upsert_assignments(
    session: Session,
    candidates: Iterable[AssignmentCandidate],
    *,
    owner_key: str = DEFAULT_OWNER_KEY,
    now: datetime | None = None,
) -> UpsertStats:
    timestamp = now or datetime.now()
    inserted = 0
    updated = 0
    skipped = 0

    for candidate in candidates:
        if not candidate.platform.strip() or not candidate.course.strip() or not candidate.title.strip():
            skipped += 1
            continue

        existing = session.scalar(
            select(Assignment).where(
                Assignment.owner_key == owner_key,
                Assignment.platform == candidate.platform,
                Assignment.course == candidate.course,
                Assignment.title == candidate.title,
                Assignment.due_at == candidate.due_at,
            )
        )
        if existing is None:
            session.add(
                Assignment(
                    owner_key=owner_key,
                    platform=candidate.platform,
                    course=candidate.course,
                    title=candidate.title,
                    status_raw=candidate.status_raw,
                    status_normalized=candidate.status_normalized,
                    due_at=candidate.due_at,
                    url=candidate.url,
                    source_key=candidate.source_key,
                    fingerprint=candidate.fingerprint,
                    is_todo=candidate.is_todo,
                    first_seen_at=timestamp,
                    last_seen_at=timestamp,
                    created_at=timestamp,
                    updated_at=timestamp,
                    raw_snapshot=candidate.raw_snapshot,
                )
            )
            inserted += 1
            continue

        existing.status_raw = candidate.status_raw
        existing.status_normalized = candidate.status_normalized
        existing.url = candidate.url
        existing.source_key = candidate.source_key
        existing.fingerprint = candidate.fingerprint
        existing.is_todo = candidate.is_todo
        existing.last_seen_at = timestamp
        existing.updated_at = timestamp
        existing.raw_snapshot = candidate.raw_snapshot
        updated += 1

    session.commit()
    return UpsertStats(inserted=inserted, updated=updated, skipped=skipped)


def list_todos(session: Session, *, owner_key: str = DEFAULT_OWNER_KEY) -> list[Assignment]:
    return list(
        session.scalars(
            select(Assignment)
            .where(
                Assignment.owner_key == owner_key,
                Assignment.is_todo.is_(True),
                Assignment.status_normalized.in_(TODO_STATUSES),
            )
            .order_by(Assignment.due_at.asc(), Assignment.platform.asc(), Assignment.course.asc())
        )
    )


def list_assignments(session: Session, *, owner_key: str = DEFAULT_OWNER_KEY) -> list[Assignment]:
    return list(
        session.scalars(
            select(Assignment).where(Assignment.owner_key == owner_key).order_by(
                Assignment.platform.asc(), Assignment.course.asc(), Assignment.due_at.asc()
            )
        )
    )


def upsert_platform_courses(
    session: Session,
    courses: Iterable[KnownCourseConfig],
    *,
    owner_key: str = DEFAULT_OWNER_KEY,
    platform_key: str,
    platform_label: str,
    now: datetime | None = None,
) -> CourseUpsertStats:
    timestamp = now or datetime.now()
    inserted = 0
    updated = 0
    skipped = 0

    for course in courses:
        course_name = course.course.strip()
        course_id = course.course_id.strip()
        task_url = course.task_url.strip()
        if not course_name or not course_id or not task_url:
            skipped += 1
            continue
        existing = session.scalar(
            select(PlatformCourse).where(
                PlatformCourse.owner_key == owner_key,
                PlatformCourse.platform_key == platform_key,
                PlatformCourse.course_id == course_id,
            )
        )
        if existing is None:
            session.add(
                PlatformCourse(
                    owner_key=owner_key,
                    platform_key=platform_key,
                    platform_label=platform_label,
                    course=course_name,
                    course_id=course_id,
                    task_url=task_url,
                    source=course.source or "discovered",
                    active=True,
                    first_seen_at=timestamp,
                    last_seen_at=timestamp,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            inserted += 1
            continue

        existing.platform_label = platform_label
        existing.course = course_name
        existing.task_url = task_url
        existing.source = course.source or existing.source
        existing.active = True
        existing.last_seen_at = timestamp
        existing.updated_at = timestamp
        updated += 1

    session.commit()
    return CourseUpsertStats(inserted=inserted, updated=updated, skipped=skipped)


def list_platform_courses(
    session: Session,
    *,
    owner_key: str = DEFAULT_OWNER_KEY,
    platform_key: str,
    active_only: bool = True,
) -> list[PlatformCourse]:
    statement = select(PlatformCourse).where(
        PlatformCourse.owner_key == owner_key,
        PlatformCourse.platform_key == platform_key,
    )
    if active_only:
        statement = statement.where(PlatformCourse.active.is_(True))
    return list(session.scalars(statement.order_by(PlatformCourse.course.asc(), PlatformCourse.course_id.asc())))


def platform_course_to_known_course(course: PlatformCourse) -> KnownCourseConfig:
    return KnownCourseConfig(
        course=course.course,
        course_id=course.course_id,
        task_url=course.task_url,
        source=course.source or "cached",
    )


def platform_course_to_dict(course: PlatformCourse) -> dict[str, str | int | bool]:
    return {
        "id": course.id,
        "owner_key": course.owner_key,
        "platform_key": course.platform_key,
        "platform_label": course.platform_label,
        "course": course.course,
        "course_id": course.course_id,
        "task_url": course.task_url,
        "source": course.source,
        "active": course.active,
        "first_seen_at": course.first_seen_at.isoformat(timespec="seconds"),
        "last_seen_at": course.last_seen_at.isoformat(timespec="seconds"),
        "created_at": course.created_at.isoformat(timespec="seconds"),
        "updated_at": course.updated_at.isoformat(timespec="seconds"),
    }


def assignment_to_dict(assignment: Assignment) -> dict[str, str | int | bool]:
    return {
        "id": assignment.id,
        "owner_key": assignment.owner_key,
        "platform": assignment.platform,
        "course": assignment.course,
        "title": assignment.title,
        "status_raw": assignment.status_raw,
        "status_normalized": assignment.status_normalized,
        "due_at": assignment.due_at.isoformat(timespec="seconds"),
        "url": assignment.url,
        "source_key": assignment.source_key,
        "fingerprint": assignment.fingerprint,
        "is_todo": assignment.is_todo,
        "first_seen_at": assignment.first_seen_at.isoformat(timespec="seconds"),
        "last_seen_at": assignment.last_seen_at.isoformat(timespec="seconds"),
        "created_at": assignment.created_at.isoformat(timespec="seconds"),
        "updated_at": assignment.updated_at.isoformat(timespec="seconds"),
        "raw_snapshot": assignment.raw_snapshot,
    }
