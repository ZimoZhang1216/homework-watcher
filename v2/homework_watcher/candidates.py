from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .status import is_todo_status, normalize_status


@dataclass(frozen=True)
class AssignmentCandidate:
    platform: str
    course: str
    title: str
    status_raw: str
    due_at: datetime
    url: str = ""
    source_key: str = ""
    raw_snapshot: str = ""

    @property
    def status_normalized(self) -> str:
        return normalize_status(self.status_raw)

    @property
    def is_todo(self) -> bool:
        return is_todo_status(self.status_normalized)

    @property
    def fingerprint(self) -> str:
        parts = [
            self.platform.strip(),
            self.course.strip(),
            self.title.strip(),
            self.due_at.isoformat(timespec="seconds"),
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def sanitized_title(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "course": self.course,
            "title": self.title,
            "status": self.status_raw,
            "due_at": self.due_at.isoformat(timespec="seconds"),
        }
