from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .candidates import AssignmentCandidate
from .models import Assignment, Base
from .settings import Settings, load_settings


@dataclass(frozen=True)
class UpsertStats:
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
    Base.metadata.create_all(engine)


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def upsert_assignments(
    session: Session, candidates: Iterable[AssignmentCandidate], *, now: datetime | None = None
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
                Assignment.platform == candidate.platform,
                Assignment.course == candidate.course,
                Assignment.title == candidate.title,
                Assignment.due_at == candidate.due_at,
            )
        )
        if existing is None:
            session.add(
                Assignment(
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


def list_todos(session: Session) -> list[Assignment]:
    return list(
        session.scalars(
            select(Assignment)
            .where(Assignment.is_todo.is_(True))
            .order_by(Assignment.due_at.asc(), Assignment.platform.asc(), Assignment.course.asc())
        )
    )


def list_assignments(session: Session) -> list[Assignment]:
    return list(
        session.scalars(
            select(Assignment).order_by(
                Assignment.platform.asc(), Assignment.course.asc(), Assignment.due_at.asc()
            )
        )
    )


def assignment_to_dict(assignment: Assignment) -> dict[str, str | int | bool]:
    return {
        "id": assignment.id,
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
