from __future__ import annotations

import re
from datetime import datetime, timedelta


DATE_PATTERNS = [
    re.compile(
        r"(?P<y>20\d{2})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*"
        r"(?P<d>\d{1,2})\s*(?:日)?"
        r"(?:\s*(?:T|at|@|/|上午|下午|晚上|夜间)?\s*"
        r"(?P<h>\d{1,2})(?:[:：点]\s*(?P<minute>\d{1,2}))?)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*(?:日|号)?"
        r"(?:\s*(?:上午|下午|晚上|夜间)?\s*"
        r"(?P<h>\d{1,2})(?:[:：点]\s*(?P<minute>\d{1,2}))?)?"
    ),
]


def now_local() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None, microsecond=0)


def parse_datetime(value: str, *, now: datetime | None = None) -> datetime:
    parsed = find_datetime(value, now=now)
    if parsed is None:
        raise ValueError(f"无法解析截止时间：{value}")
    return parsed


def find_datetime(text: str, *, now: datetime | None = None) -> datetime | None:
    now = now or now_local()
    normalized = text.strip()
    relative = _parse_relative_datetime(normalized, now)
    if relative is not None:
        return relative

    for pattern in DATE_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups.get("y") or now.year)
        month = int(groups["m"])
        day = int(groups["d"])
        hour = int(groups["h"]) if groups.get("h") else 23
        minute = int(groups["minute"]) if groups.get("minute") else (0 if groups.get("h") else 59)
        raw = match.group(0)
        if ("下午" in raw or "晚上" in raw or "夜间" in raw) and hour < 12:
            hour += 12
        if "上午" in raw and hour == 12:
            hour = 0
        parsed = datetime(year, month, day, hour, minute)
        if not groups.get("y") and parsed < now - timedelta(days=1):
            parsed = parsed.replace(year=parsed.year + 1)
        return parsed
    return None


def _parse_relative_datetime(text: str, now: datetime) -> datetime | None:
    if "今天" not in text and "明天" not in text and "后天" not in text:
        return None
    offset = 0
    if "明天" in text:
        offset = 1
    elif "后天" in text:
        offset = 2
    date = (now + timedelta(days=offset)).date()
    time_match = re.search(r"(?P<h>\d{1,2})(?:[:：点]\s*(?P<minute>\d{1,2}))?", text)
    if time_match:
        hour = int(time_match.group("h"))
        minute = int(time_match.group("minute") or 0)
        raw = time_match.group(0)
        if ("下午" in text or "晚上" in text or "夜间" in text) and hour < 12:
            hour += 12
        if "上午" in text and hour == 12:
            hour = 0
    else:
        hour = 23
        minute = 59
    return datetime(date.year, date.month, date.day, hour, minute)


def to_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def human_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")
