from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

from .datetime_utils import from_iso, now_local, to_iso
from .models import Assignment
from .statuses import assignment_is_pending


class HomeworkDB:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                course TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                completed_at TEXT,
                source_text TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                assignment_id INTEGER NOT NULL,
                rule_key TEXT NOT NULL,
                reminded_at TEXT NOT NULL,
                PRIMARY KEY (assignment_id, rule_key),
                FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE
            );
            """
        )
        self.ensure_columns()
        self.conn.commit()

    def ensure_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(assignments)").fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN status TEXT NOT NULL DEFAULT ''")
        if "url" not in columns:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN url TEXT NOT NULL DEFAULT ''")

    def add_assignment(
        self,
        *,
        title: str,
        due_at: datetime,
        course: str = "",
        platform: str = "",
        status: str = "",
        url: str = "",
        source_text: str | None = None,
    ) -> tuple[Assignment, bool]:
        title = clean_text(title)
        course = clean_text(course)
        platform = clean_text(platform)
        status = clean_text(status)
        url = clean_text(url)
        fingerprint = make_fingerprint(title, course, platform, due_at)
        timestamp = to_iso(now_local())
        try:
            cursor = self.conn.execute(
                """
                INSERT INTO assignments
                    (title, course, platform, due_at, status, url, source_text, fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    course,
                    platform,
                    to_iso(due_at),
                    status,
                    url,
                    source_text,
                    fingerprint,
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
            return self.get_assignment(cursor.lastrowid), True
        except sqlite3.IntegrityError:
            existing = self.get_by_fingerprint(fingerprint)
            if existing is None:
                raise
            self.conn.execute(
                """
                UPDATE assignments
                SET status = CASE WHEN ? != '' THEN ? ELSE status END,
                    url = CASE WHEN ? != '' THEN ? ELSE url END,
                    source_text = COALESCE(?, source_text),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, status, url, url, source_text, timestamp, existing.id),
            )
            self.conn.commit()
            return self.get_assignment(existing.id), False

    def get_assignment(self, assignment_id: int) -> Assignment:
        row = self.conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
        if row is None:
            raise KeyError(f"assignment not found: {assignment_id}")
        return row_to_assignment(row)

    def get_by_fingerprint(self, fingerprint: str) -> Assignment | None:
        row = self.conn.execute("SELECT * FROM assignments WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row_to_assignment(row) if row else None

    def find_by_title_course_due(self, *, title: str, course: str, due_at: datetime) -> Assignment | None:
        row = self.conn.execute(
            """
            SELECT * FROM assignments
            WHERE title = ? AND course = ? AND due_at = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (clean_text(title), clean_text(course), to_iso(due_at)),
        ).fetchone()
        return row_to_assignment(row) if row else None

    def list_assignments(self, *, include_done: bool = False) -> list[Assignment]:
        rows = self.conn.execute(
            "SELECT * FROM assignments ORDER BY due_at ASC, id ASC"
        ).fetchall()
        assignments = [row_to_assignment(row) for row in rows]
        if include_done:
            return assignments
        return [assignment for assignment in assignments if assignment_is_pending(assignment)]

    def mark_done(self, assignment_id: int) -> Assignment:
        timestamp = to_iso(now_local())
        cursor = self.conn.execute(
            "UPDATE assignments SET completed_at = COALESCE(completed_at, ?), updated_at = ? WHERE id = ?",
            (timestamp, timestamp, assignment_id),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"assignment not found: {assignment_id}")
        return self.get_assignment(assignment_id)

    def reminder_sent(self, assignment_id: int, rule_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM reminders WHERE assignment_id = ? AND rule_key = ?",
            (assignment_id, rule_key),
        ).fetchone()
        return row is not None

    def mark_reminded(self, assignment_id: int, rule_key: str, *, at: datetime | None = None) -> None:
        timestamp = to_iso(at or now_local())
        self.conn.execute(
            """
            INSERT OR IGNORE INTO reminders (assignment_id, rule_key, reminded_at)
            VALUES (?, ?, ?)
            """,
            (assignment_id, rule_key, timestamp),
        )
        self.conn.commit()


def clean_text(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def make_fingerprint(title: str, course: str, platform: str, due_at: datetime) -> str:
    raw = "\n".join(
        [
            clean_text(title).casefold(),
            clean_text(course).casefold(),
            clean_text(platform).casefold(),
            to_iso(due_at),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def row_to_assignment(row: sqlite3.Row) -> Assignment:
    return Assignment(
        id=row["id"],
        title=row["title"],
        course=row["course"],
        platform=row["platform"],
        due_at=from_iso(row["due_at"]),
        status=row["status"],
        url=row["url"],
        completed_at=from_iso(row["completed_at"]),
        source_text=row["source_text"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        fingerprint=row["fingerprint"],
    )
