from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import timedelta

from .datetime_utils import human_datetime
from .models import Assignment


DEFAULT_CALENDAR_NAME = "作业提醒-iCloud"
SYNC_MARKER = "homework-watcher-id:"


def sync_calendar(
    assignments: list[Assignment],
    *,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    dry_run: bool = False,
) -> int:
    script = build_calendar_sync_script(assignments, calendar_name=calendar_name)
    if dry_run:
        print(script)
        return len(assignments)
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        raise RuntimeError("Calendar 同步只支持 macOS，并且需要 osascript。")
    ensure_calendar_running()
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"写入 Calendar 失败：{detail or result.returncode}")
    return len(assignments)


def list_calendars() -> list[dict[str, str]]:
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        raise RuntimeError("Calendar 列表只支持 macOS，并且需要 osascript。")
    ensure_calendar_running()
    script = """
tell application "Calendar"
  set rowsOut to {}
  set n to 0
  repeat with c in calendars
    set n to n + 1
    set eventCount to 0
    try
      set eventCount to count of events of c
    end try
    set writableText to "unknown"
    try
      set writableText to ((writable of c) as text)
    end try
    set end of rowsOut to (n as text) & tab & (name of c as text) & tab & writableText & tab & (eventCount as text)
  end repeat
  return rowsOut
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"读取 Calendar 列表失败：{detail or result.returncode}")
    rows: list[dict[str, str]] = []
    for raw_row in result.stdout.strip().split(", "):
        parts = raw_row.split("\t")
        if len(parts) != 4:
            continue
        rows.append({"index": parts[0], "name": parts[1], "writable": parts[2], "events": parts[3]})
    return rows


def ensure_calendar_running() -> None:
    if shutil.which("open") is None:
        return
    subprocess.run(["open", "-g", "-a", "Calendar"], check=False)
    time.sleep(2)


def build_calendar_sync_script(assignments: list[Assignment], *, calendar_name: str) -> str:
    lines = [
        "on makeDate(y, m, d, h, min)",
        "  set theDate to current date",
        "  set year of theDate to y",
        "  set month of theDate to m",
        "  set day of theDate to d",
        "  set time of theDate to (h * hours + min * minutes)",
        "  return theDate",
        "end makeDate",
        "",
        'tell application "Calendar"',
        "  launch",
        f"  set calendarName to {applescript_quote(calendar_name)}",
        "  if not (exists calendar calendarName) then",
        "    error \"找不到名为 \" & calendarName & \" 的日历。请先在 Calendar app 的 iCloud 下新建这个日历，或用 --calendar-name 指定已有 iCloud 日历。\"",
        "  end if",
        "  set targetCalendar to calendar calendarName",
        "  repeat with eventIndex from (count of events of targetCalendar) to 1 by -1",
        "    try",
        "      set existingEvent to event eventIndex of targetCalendar",
        f"      if (description of existingEvent as text) contains {applescript_quote(SYNC_MARKER)} then",
        "        delete existingEvent",
        "      end if",
        "    end try",
        "  end repeat",
    ]
    for assignment in assignments:
        if assignment.id is None:
            continue
        start = assignment.due_at - timedelta(minutes=30)
        end = assignment.due_at
        lines.extend(
            [
                "",
                f"  set eventStart to my makeDate({start.year}, {start.month}, {start.day}, {start.hour}, {start.minute})",
                f"  set eventEnd to my makeDate({end.year}, {end.month}, {end.day}, {end.hour}, {end.minute})",
                "  set newEvent to make new event at end of events of targetCalendar with properties "
                + "{"
                + f"summary:{applescript_quote('作业截止：' + assignment.title)}, "
                + "start date:eventStart, "
                + "end date:eventEnd, "
                + f"description:{applescript_quote(description_for_assignment(assignment))}"
                + "}",
                "  try",
                "    make new display alarm at end of display alarms of newEvent with properties {trigger interval:-1440}",
                "    make new display alarm at end of display alarms of newEvent with properties {trigger interval:-360}",
                "    make new display alarm at end of display alarms of newEvent with properties {trigger interval:-60}",
                "  end try",
            ]
        )
    lines.extend(["end tell", "return \"ok\""])
    return "\n".join(lines)


def description_for_assignment(assignment: Assignment) -> str:
    parts = [
        f"{SYNC_MARKER}{assignment.id}",
        f"课程：{assignment.course}" if assignment.course else "",
        f"平台：{assignment.platform}" if assignment.platform else "",
        f"平台状态：{assignment.status}" if assignment.status else "",
        f"截止：{human_datetime(assignment.due_at)}",
        f"链接：{assignment.url}" if assignment.url else "",
    ]
    return "\n".join(part for part in parts if part)


def applescript_quote(value: str) -> str:
    parts = value.splitlines() or [""]
    quoted = []
    for part in parts:
        escaped = part.replace("\\", "\\\\").replace('"', '\\"')
        quoted.append(f'"{escaped}"')
    return " & return & ".join(quoted)
