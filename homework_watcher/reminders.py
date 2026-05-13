from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .datetime_utils import human_datetime, now_local
from .db import HomeworkDB
from .models import Assignment
from .notifier import Notifier


@dataclass(frozen=True)
class ReminderEvent:
    assignment: Assignment
    rule_key: str
    title: str
    message: str


THRESHOLDS = [
    ("due_24h", timedelta(hours=24), "截止前 24 小时"),
    ("due_6h", timedelta(hours=6), "截止前 6 小时"),
    ("due_1h", timedelta(hours=1), "截止前 1 小时"),
]


def remind_new_assignment(db: HomeworkDB, notifier: Notifier, assignment: Assignment) -> None:
    if assignment.id is None or db.reminder_sent(assignment.id, "new"):
        return
    notifier.notify(
        "新作业",
        f"{assignment.title}，截止 {human_datetime(assignment.due_at)}",
        subtitle=subtitle_for(assignment),
    )
    db.mark_reminded(assignment.id, "new")


def run_due_reminders(
    db: HomeworkDB,
    notifier: Notifier,
    *,
    now: datetime | None = None,
) -> list[ReminderEvent]:
    now = now or now_local()
    events: list[ReminderEvent] = []
    for assignment in db.list_assignments(include_done=False):
        if assignment.id is None:
            continue
        event = overdue_event(db, assignment, now) or threshold_event(db, assignment, now)
        if event is None:
            continue
        notifier.notify(event.title, event.message, subtitle=subtitle_for(assignment))
        db.mark_reminded(assignment.id, event.rule_key, at=now)
        events.append(event)
    return events


def overdue_event(db: HomeworkDB, assignment: Assignment, now: datetime) -> ReminderEvent | None:
    if now < assignment.due_at:
        return None
    rule_key = f"overdue:{now.date().isoformat()}"
    if db.reminder_sent(assignment.id, rule_key):
        return None
    return ReminderEvent(
        assignment=assignment,
        rule_key=rule_key,
        title="作业已逾期",
        message=f"{assignment.title} 已在 {human_datetime(assignment.due_at)} 截止，请尽快处理。",
    )


def threshold_event(db: HomeworkDB, assignment: Assignment, now: datetime) -> ReminderEvent | None:
    remaining = assignment.due_at - now
    if remaining <= timedelta(0):
        return None
    for rule_key, threshold, label in reversed(THRESHOLDS):
        if remaining <= threshold and not db.reminder_sent(assignment.id, rule_key):
            return ReminderEvent(
                assignment=assignment,
                rule_key=rule_key,
                title=f"作业{label}提醒",
                message=f"{assignment.title} 将于 {human_datetime(assignment.due_at)} 截止。",
            )
    return None


def subtitle_for(assignment: Assignment) -> str:
    parts = [part for part in [assignment.course, assignment.platform] if part]
    return " / ".join(parts)
