from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .candidates import AssignmentCandidate
from .config_loader import KnownCourseConfig
from .models import Assignment, Base, ManualAssignmentSeries, PlatformCourse
from .settings import Settings, load_settings
from .status import TODO_STATUSES, normalize_status


DEFAULT_OWNER_KEY = "default"
MANUAL_ASSIGNMENT_PLATFORM = "手动"
MANUAL_ASSIGNMENT_COURSE = "手动添加"
MANUAL_ASSIGNMENT_SOURCE_PREFIX = "manual:"
MANUAL_ASSIGNMENT_SERIES_PREFIX = "manual:series:"
MANUAL_RECURRENCE_NONE = "none"
MANUAL_RECURRENCE_DAILY = "daily"
MANUAL_RECURRENCE_WEEKLY = "weekly"
MANUAL_RECURRENCE_MONTHLY = "monthly"
MANUAL_RECURRENCE_VALUES = {
    MANUAL_RECURRENCE_NONE,
    MANUAL_RECURRENCE_DAILY,
    MANUAL_RECURRENCE_WEEKLY,
    MANUAL_RECURRENCE_MONTHLY,
}
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


def add_manual_assignment(
    session: Session,
    *,
    owner_key: str = DEFAULT_OWNER_KEY,
    title: str,
    due_at: datetime,
    completed: bool = False,
    recurrence: str = MANUAL_RECURRENCE_NONE,
    now: datetime | None = None,
) -> Assignment:
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("作业名不能为空")
    recurrence = normalize_manual_recurrence(recurrence)
    timestamp = now or datetime.now()
    if recurrence == MANUAL_RECURRENCE_NONE:
        assignment = create_or_update_manual_assignment(
            session,
            owner_key=owner_key,
            title=title,
            due_at=due_at,
            completed=completed,
            source_key=f"{MANUAL_ASSIGNMENT_SOURCE_PREFIX}single:{owner_key}:{timestamp.isoformat(timespec='seconds')}",
            timestamp=timestamp,
        )
        session.commit()
        session.refresh(assignment)
        return assignment

    series = ManualAssignmentSeries(
        owner_key=owner_key,
        title=title,
        recurrence=recurrence,
        next_due_at=due_at,
        active=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(series)
    session.flush()
    source_key = manual_series_source_key(series.id)
    assignment = create_or_update_manual_assignment(
        session,
        owner_key=owner_key,
        title=title,
        due_at=due_at,
        completed=completed,
        source_key=source_key,
        timestamp=timestamp,
    )
    if completed:
        series.next_due_at = next_manual_due_at(due_at, recurrence)
        series.updated_at = timestamp
        create_or_update_manual_assignment(
            session,
            owner_key=owner_key,
            title=title,
            due_at=series.next_due_at,
            completed=False,
            source_key=source_key,
            timestamp=timestamp,
        )
    session.commit()
    session.refresh(assignment)
    return assignment


def create_or_update_manual_assignment(
    session: Session,
    *,
    owner_key: str,
    title: str,
    due_at: datetime,
    completed: bool,
    source_key: str,
    timestamp: datetime,
) -> Assignment:
    status_raw = "已完成" if completed else "进行中"
    candidate = AssignmentCandidate(
        platform=MANUAL_ASSIGNMENT_PLATFORM,
        course=MANUAL_ASSIGNMENT_COURSE,
        title=title,
        status_raw=status_raw,
        due_at=due_at,
        source_key=source_key,
        raw_snapshot=f"manual completed={str(completed).lower()}",
    )
    existing = session.scalar(
        select(Assignment).where(
            Assignment.owner_key == owner_key,
            Assignment.platform == MANUAL_ASSIGNMENT_PLATFORM,
            Assignment.course == MANUAL_ASSIGNMENT_COURSE,
            Assignment.title == candidate.title,
            Assignment.due_at == candidate.due_at,
        )
    )
    if existing is None:
        assignment = Assignment(
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
        session.add(assignment)
        return assignment

    existing.status_raw = candidate.status_raw
    existing.status_normalized = candidate.status_normalized
    existing.is_todo = candidate.is_todo
    existing.last_seen_at = timestamp
    existing.updated_at = timestamp
    existing.raw_snapshot = candidate.raw_snapshot
    existing.source_key = candidate.source_key
    return existing


def set_manual_assignment_completed(
    session: Session,
    *,
    owner_key: str = DEFAULT_OWNER_KEY,
    assignment_id: int,
    completed: bool,
    now: datetime | None = None,
) -> Assignment | None:
    assignment = session.scalar(
        select(Assignment).where(
            Assignment.id == assignment_id,
            Assignment.owner_key == owner_key,
            Assignment.source_key.like(f"{MANUAL_ASSIGNMENT_SOURCE_PREFIX}%"),
        )
    )
    if assignment is None:
        return None
    timestamp = now or datetime.now()
    status_raw = "已完成" if completed else "进行中"
    assignment.status_raw = status_raw
    assignment.status_normalized = normalize_status(status_raw)
    assignment.is_todo = assignment.status_normalized in TODO_STATUSES
    assignment.last_seen_at = timestamp
    assignment.updated_at = assignment.last_seen_at
    series = manual_series_for_assignment(session, assignment)
    if series is not None and series.active:
        if completed:
            series.next_due_at = next_manual_due_at(assignment.due_at, series.recurrence)
            create_or_update_manual_assignment(
                session,
                owner_key=owner_key,
                title=series.title,
                due_at=series.next_due_at,
                completed=False,
                source_key=manual_series_source_key(series.id),
                timestamp=timestamp,
            )
        else:
            series.next_due_at = assignment.due_at
        series.updated_at = timestamp
    session.commit()
    session.refresh(assignment)
    return assignment


def is_manual_assignment_dict(item: dict[str, object]) -> bool:
    return str(item.get("source_key") or "").startswith(MANUAL_ASSIGNMENT_SOURCE_PREFIX)


def manual_series_for_assignment(session: Session, assignment: Assignment) -> ManualAssignmentSeries | None:
    series_id = manual_series_id_from_source_key(assignment.source_key)
    if series_id is None:
        return None
    return session.get(ManualAssignmentSeries, series_id)


def manual_series_id_from_source_key(source_key: str) -> int | None:
    if not source_key.startswith(MANUAL_ASSIGNMENT_SERIES_PREFIX):
        return None
    raw_id = source_key.removeprefix(MANUAL_ASSIGNMENT_SERIES_PREFIX).split(":", 1)[0]
    try:
        return int(raw_id)
    except ValueError:
        return None


def manual_series_source_key(series_id: int) -> str:
    return f"{MANUAL_ASSIGNMENT_SERIES_PREFIX}{series_id}"


def normalize_manual_recurrence(value: str) -> str:
    normalized = (value or MANUAL_RECURRENCE_NONE).strip().lower()
    if normalized not in MANUAL_RECURRENCE_VALUES:
        raise ValueError("重复周期不正确")
    return normalized


def next_manual_due_at(due_at: datetime, recurrence: str) -> datetime:
    recurrence = normalize_manual_recurrence(recurrence)
    if recurrence == MANUAL_RECURRENCE_DAILY:
        return due_at + timedelta(days=1)
    if recurrence == MANUAL_RECURRENCE_WEEKLY:
        return due_at + timedelta(weeks=1)
    if recurrence == MANUAL_RECURRENCE_MONTHLY:
        return add_month(due_at)
    return due_at


def add_month(value: datetime) -> datetime:
    month = value.month + 1
    year = value.year
    if month > 12:
        month = 1
        year += 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


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
