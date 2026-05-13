from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Assignment:
    id: int | None
    title: str
    course: str
    platform: str
    due_at: datetime
    status: str = ""
    url: str = ""
    completed_at: datetime | None = None
    source_text: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fingerprint: str | None = None

    @property
    def is_done(self) -> bool:
        return self.completed_at is not None
