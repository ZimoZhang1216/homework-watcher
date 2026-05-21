from __future__ import annotations

import re


STATUS_IN_PROGRESS = "in_progress"
STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"
STATUS_UNKNOWN = "unknown"

TODO_STATUSES = {STATUS_IN_PROGRESS, STATUS_PENDING}


def clean_status_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value))


def normalize_status(value: str | None) -> str:
    text = clean_status_text(value)
    if not text:
        return STATUS_UNKNOWN
    if any(marker in text for marker in ("已完成", "已提交", "已批阅")):
        return STATUS_COMPLETED
    if "已截止" in text or "逾期" in text:
        return STATUS_EXPIRED
    if any(marker in text for marker in ("进行中", "未提交", "待完成", "未完成")):
        return STATUS_IN_PROGRESS
    if "未开始" in text:
        return STATUS_PENDING
    return STATUS_UNKNOWN


def is_todo_status(status_normalized: str) -> bool:
    return status_normalized in TODO_STATUSES
