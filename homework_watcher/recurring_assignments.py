from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .datetime_utils import now_local
from .db import HomeworkDB
from .models import Assignment


DEFAULT_RECURRING_HORIZON_DAYS = 28
RECURRING_PLATFORM = "固定作业"


@dataclass(frozen=True)
class RecurringAssignmentRule:
    slug: str
    title: str
    course: str
    platform: str
    weekday: int
    due_time: time


DEFAULT_RECURRING_RULES = [
    RecurringAssignmentRule(
        slug="quantitative-analysis-weekly",
        title="定量化学分析作业",
        course="定量化学分析",
        platform="飞书私信助教",
        weekday=1,
        due_time=time(23, 59),
    ),
    RecurringAssignmentRule(
        slug="organic-chemistry-weekly",
        title="有机化学作业",
        course="有机化学",
        platform="线下",
        weekday=6,
        due_time=time(23, 59),
    ),
]


def materialize_recurring_assignments(
    db: HomeworkDB,
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_RECURRING_HORIZON_DAYS,
    rules: list[RecurringAssignmentRule] | None = None,
) -> list[Assignment]:
    current = now or now_local()
    generated: list[Assignment] = []
    for rule in rules or DEFAULT_RECURRING_RULES:
        for due_at in iter_due_times(rule, now=current, horizon_days=horizon_days):
            existing = db.find_by_title_course_due(title=rule.title, course=rule.course, due_at=due_at)
            if existing is not None:
                continue
            assignment, created = db.add_assignment(
                title=rule.title,
                course=rule.course,
                platform=rule.platform,
                due_at=due_at,
                status="未提交",
                source_text=f"recurring:{rule.slug}:{due_at.date().isoformat()}",
            )
            if created:
                generated.append(assignment)
    return generated


def iter_due_times(
    rule: RecurringAssignmentRule,
    *,
    now: datetime,
    horizon_days: int,
) -> list[datetime]:
    start_date = now.date()
    end_at = now + timedelta(days=max(0, horizon_days))
    days_until = (rule.weekday - start_date.weekday()) % 7
    due_date = start_date + timedelta(days=days_until)
    due_at = datetime.combine(due_date, rule.due_time)
    if due_at < now:
        due_at += timedelta(days=7)

    due_times: list[datetime] = []
    while due_at <= end_at:
        due_times.append(due_at)
        due_at += timedelta(days=7)
    return due_times
