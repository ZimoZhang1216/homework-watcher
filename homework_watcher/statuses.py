from __future__ import annotations

from .models import Assignment


DONE_STATUS_MARKERS = ("已提交", "已完成", "已批改", "已评分")
UNAVAILABLE_STATUS_MARKERS = ("不可完成",)


def platform_status_is_done(status: str) -> bool:
    return any(marker in (status or "") for marker in DONE_STATUS_MARKERS)


def platform_status_is_unavailable(status: str) -> bool:
    return any(marker in (status or "") for marker in UNAVAILABLE_STATUS_MARKERS)


def assignment_is_done(assignment: Assignment) -> bool:
    return assignment.is_done or platform_status_is_done(assignment.status)


def assignment_is_pending(assignment: Assignment) -> bool:
    return not assignment_is_done(assignment) and not platform_status_is_unavailable(assignment.status)
