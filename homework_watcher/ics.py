from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .datetime_utils import now_local
from .models import Assignment


def export_ics(assignments: list[Assignment], output: Path | str) -> Path:
    output_path = Path(output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//homework-watcher//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    stamp = format_ics_datetime(now_local())
    for assignment in assignments:
        due = assignment.due_at
        start = due - timedelta(minutes=30)
        uid = f"homework-{assignment.id or assignment.fingerprint}@homework-watcher"
        description_parts = [
            f"课程：{assignment.course}" if assignment.course else "",
            f"平台：{assignment.platform}" if assignment.platform else "",
            f"平台状态：{assignment.status}" if assignment.status else "",
            f"截止：{due.strftime('%Y-%m-%d %H:%M')}",
            f"链接：{assignment.url}" if assignment.url else "",
        ]
        description = "\\n".join(part for part in description_parts if part)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{escape_ics(uid)}",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{format_ics_datetime(start)}",
                f"DTEND:{format_ics_datetime(due)}",
                f"SUMMARY:{escape_ics('作业截止：' + assignment.title)}",
                f"DESCRIPTION:{escape_ics(description)}",
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
            ]
        )
    lines.append("END:VCALENDAR")
    output_path.write_text("\n".join(fold_line(line) for line in lines) + "\n", encoding="utf-8")
    return output_path


def format_ics_datetime(value) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_line(line: str) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks = []
    current = ""
    for char in line:
        if len((current + char).encode("utf-8")) > 73:
            chunks.append(current)
            current = " " + char
        else:
            current += char
    if current:
        chunks.append(current)
    return "\n".join(chunks)
