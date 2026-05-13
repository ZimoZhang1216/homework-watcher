from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .calendar_sync import DEFAULT_CALENDAR_NAME, SYNC_MARKER, description_for_assignment
from .datetime_utils import now_local
from .ics import escape_ics, fold_line, format_ics_datetime
from .models import Assignment


DEFAULT_ICLOUD_CALDAV_URL = "https://caldav.icloud.com"


@dataclass(frozen=True)
class CalDAVSyncResult:
    created: int
    deleted: int
    calendar_name: str


def sync_icloud_calendar(
    assignments: list[Assignment],
    *,
    username: str,
    app_password: str,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    url: str = DEFAULT_ICLOUD_CALDAV_URL,
    create_calendar: bool = False,
    dry_run: bool = False,
) -> CalDAVSyncResult:
    if dry_run:
        print("\n\n".join(build_caldav_event(assignment) for assignment in assignments if assignment.id is not None))
        return CalDAVSyncResult(created=sum(1 for assignment in assignments if assignment.id is not None), deleted=0, calendar_name=calendar_name)
    if not username:
        raise ValueError("缺少 iCloud 用户名。请设置 ICLOUD_USERNAME 或使用 --icloud-username。")
    if not app_password:
        raise ValueError("缺少 iCloud app-specific password。请设置 ICLOUD_APP_PASSWORD。")

    try:
        import caldav
    except ImportError as exc:
        raise RuntimeError("缺少 caldav 依赖。请重新安装项目：python -m pip install .") from exc

    try:
        client = caldav.DAVClient(url=url, username=username, password=app_password)
        principal = client.principal()
        calendar = find_calendar(principal, calendar_name)
        if calendar is None:
            if not create_calendar:
                raise RuntimeError(f"找不到 iCloud 日历：{calendar_name}。请先在 iCloud Calendar 创建，或加 --icloud-create-calendar。")
            calendar = principal.make_calendar(name=calendar_name)

        deleted = delete_existing_synced_events(calendar)
        created = 0
        for assignment in assignments:
            if assignment.id is None:
                continue
            calendar.save_event(build_caldav_event(assignment))
            created += 1
        return CalDAVSyncResult(created=created, deleted=deleted, calendar_name=calendar_name)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"iCloud Calendar CalDAV 同步失败：{exc}") from exc


def find_calendar(principal: Any, calendar_name: str) -> Any | None:
    for calendar in principal.calendars():
        if calendar_display_name(calendar) == calendar_name:
            return calendar
    return None


def calendar_display_name(calendar: Any) -> str:
    try:
        name = calendar.get_display_name()
    except Exception:
        name = getattr(calendar, "name", "")
    return str(name or "").strip()


def delete_existing_synced_events(calendar: Any) -> int:
    deleted = 0
    for event in calendar.events():
        data = event_data(event)
        if SYNC_MARKER not in data:
            continue
        event.delete()
        deleted += 1
    return deleted


def event_data(event: Any) -> str:
    data = getattr(event, "data", "") or ""
    if data:
        return str(data)
    try:
        return str(event.get_data())
    except Exception:
        return ""


def build_caldav_event(assignment: Assignment) -> str:
    due = assignment.due_at
    start = due - timedelta(minutes=30)
    uid = f"homework-{assignment.id or assignment.fingerprint}@homework-watcher"
    stamp = format_ics_datetime(now_local())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//homework-watcher//CN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{escape_ics(uid)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{format_ics_datetime(start)}",
        f"DTEND:{format_ics_datetime(due)}",
        f"SUMMARY:{escape_ics('作业截止：' + assignment.title)}",
        f"DESCRIPTION:{escape_ics(description_for_assignment(assignment))}",
    ]
    if assignment.url:
        lines.append(f"URL:{escape_ics(assignment.url)}")
    lines.extend(
        [
            "BEGIN:VALARM",
            "TRIGGER:-PT24H",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{escape_ics('作业 24 小时后截止：' + assignment.title)}",
            "END:VALARM",
            "BEGIN:VALARM",
            "TRIGGER:-PT6H",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{escape_ics('作业 6 小时后截止：' + assignment.title)}",
            "END:VALARM",
            "BEGIN:VALARM",
            "TRIGGER:-PT1H",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{escape_ics('作业 1 小时后截止：' + assignment.title)}",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    return "\n".join(fold_line(line) for line in lines) + "\n"
