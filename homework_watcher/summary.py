from __future__ import annotations

from datetime import datetime, timedelta

from .datetime_utils import human_datetime, now_local
from .models import Assignment
from .statuses import assignment_is_pending


def build_daily_summary(assignments: list[Assignment], *, now: datetime | None = None) -> str:
    now = now or now_local()
    active = [item for item in assignments if assignment_is_pending(item)]
    today = now.date()
    tomorrow = today + timedelta(days=1)

    overdue = [item for item in active if item.due_at < now]
    due_today = [item for item in active if item.due_at.date() == today and item.due_at >= now]
    due_tomorrow = [item for item in active if item.due_at.date() == tomorrow]

    sections = [
        ("今日截止", due_today),
        ("明日截止", due_tomorrow),
        ("逾期未提交", overdue),
    ]
    lines = [f"作业汇总（{today.isoformat()}）"]
    for title, items in sections:
        lines.append("")
        lines.append(f"{title}：")
        if not items:
            lines.append("  无")
            continue
        for item in sorted(items, key=lambda assignment: assignment.due_at):
            meta = " / ".join(part for part in [item.course, item.platform] if part)
            prefix = f"  #{item.id} {human_datetime(item.due_at)}"
            suffix = f" [{meta}]" if meta else ""
            lines.append(f"{prefix} {item.title}{suffix}")
    return "\n".join(lines)
