from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

from .calendar_sync import SYNC_MARKER, applescript_quote
from .datetime_utils import human_datetime, now_local
from .models import Assignment


DEFAULT_REMINDERS_LIST_NAME = "Reminders"
WARNING_SYMBOL = "⚠️"
WARNING_WINDOW = timedelta(days=3)


def sync_reminders(
    assignments: list[Assignment],
    *,
    list_name: str = DEFAULT_REMINDERS_LIST_NAME,
    dry_run: bool = False,
) -> int:
    script = build_reminders_sync_script(assignments, list_name=list_name)
    if dry_run:
        print(script)
        return len(assignments)
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        raise RuntimeError("Reminders 同步只支持 macOS，并且需要 osascript。")
    ensure_reminders_running()
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"写入 Reminders 失败：{detail or result.returncode}")
    return len(assignments)


def list_reminder_lists() -> list[dict[str, str]]:
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        raise RuntimeError("Reminders 列表只支持 macOS，并且需要 osascript。")
    ensure_reminders_running()
    script = """
set rowsOut to {}
tell application "Reminders"
  repeat with l in lists
    set reminderCount to 0
    try
      set reminderCount to count of reminders of l
    end try
    set end of rowsOut to (name of l as text) & tab & (reminderCount as text)
  end repeat
end tell
set oldDelimiters to AppleScript's text item delimiters
set AppleScript's text item delimiters to linefeed
set outputText to rowsOut as text
set AppleScript's text item delimiters to oldDelimiters
return outputText
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"读取 Reminders 列表失败：{detail or result.returncode}")
    rows: list[dict[str, str]] = []
    for raw_row in result.stdout.strip().splitlines():
        parts = raw_row.split("\t")
        if len(parts) != 2:
            continue
        rows.append({"name": parts[0], "reminders": parts[1]})
    return rows


def ensure_reminders_running() -> None:
    if shutil.which("open") is None:
        return
    subprocess.run(["open", "-g", "-a", "Reminders"], check=False)
    time.sleep(2)


def build_reminders_sync_script(
    assignments: list[Assignment],
    *,
    list_name: str,
    now: datetime | None = None,
) -> str:
    now = now or now_local()
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
        'tell application "Reminders"',
        "  launch",
        f"  set targetListName to {applescript_quote(list_name)}",
        "  if not (exists list targetListName) then",
        "    make new list with properties {name:targetListName}",
        "  end if",
        "  set targetList to list targetListName",
        "  try",
        f"    delete (reminders of targetList whose body contains {applescript_quote(SYNC_MARKER)})",
        "  end try",
    ]
    for assignment in assignments:
        if assignment.id is None:
            continue
        due = assignment.due_at
        lines.extend(
            [
                "",
                f"  set reminderDue to my makeDate({due.year}, {due.month}, {due.day}, {due.hour}, {due.minute})",
                "  make new reminder at end of reminders of targetList with properties "
                + "{"
                + f"name:{applescript_quote(name_for_reminder(assignment, now=now))}, "
                + "due date:reminderDue, "
                + "remind me date:reminderDue, "
                + f"body:{applescript_quote(description_for_reminder(assignment))}"
                + "}",
            ]
        )
    lines.extend(["end tell", "return \"ok\""])
    return "\n".join(lines)


def name_for_reminder(assignment: Assignment, *, now: datetime | None = None) -> str:
    course = assignment.course.strip()
    title = assignment.title.strip()
    if course:
        name = f"{course}：{title}"
    else:
        name = f"作业：{title}"
    if due_within_warning_window(assignment, now=now):
        return f"{WARNING_SYMBOL} {name}"
    return name


def due_within_warning_window(assignment: Assignment, *, now: datetime | None = None) -> bool:
    now = now or now_local()
    remaining = assignment.due_at - now
    return timedelta(0) <= remaining <= WARNING_WINDOW


def description_for_reminder(assignment: Assignment) -> str:
    parts = [
        f"{SYNC_MARKER}{assignment.id}",
        f"课程：{assignment.course}" if assignment.course else "",
        f"平台：{assignment.platform}" if assignment.platform else "",
        f"平台状态：{assignment.status}" if assignment.status else "",
        f"截止：{human_datetime(assignment.due_at)}",
        f"链接：{assignment.url}" if assignment.url else "",
    ]
    return "\n".join(part for part in parts if part)
