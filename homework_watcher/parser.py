from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .datetime_utils import find_datetime


@dataclass(frozen=True)
class ParsedAssignment:
    title: str
    course: str
    platform: str
    due_at: datetime
    raw_text: str


FIELD_PATTERNS = {
    "title": [
        re.compile(r"^(?:作业标题|作业名称|标题|任务名称|任务|作业)\s*[:：]\s*(.+)$"),
    ],
    "course": [
        re.compile(r"^(?:课程名称|课程|科目|班课)\s*[:：]\s*(.+)$"),
    ],
    "platform": [
        re.compile(r"^(?:平台|来源)\s*[:：]\s*(.+)$"),
    ],
}


def parse_assignments(text: str, *, now: datetime | None = None) -> list[ParsedAssignment]:
    blocks = split_blocks(text)
    parsed: list[ParsedAssignment] = []
    for block in blocks:
        due_at = find_datetime(block, now=now)
        if due_at is None:
            continue
        title = extract_field(block, "title") or guess_title(block)
        if not title:
            title = "未命名作业"
        course = extract_field(block, "course")
        platform = extract_field(block, "platform") or guess_platform(block)
        parsed.append(
            ParsedAssignment(
                title=clean_candidate(title),
                course=clean_candidate(course),
                platform=clean_candidate(platform),
                due_at=due_at,
                raw_text=block.strip(),
            )
        )
    return parsed


def split_blocks(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    paragraph_blocks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", normalized) if chunk.strip()]
    if len(paragraph_blocks) > 1:
        return paragraph_blocks

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    due_like_lines = [line for line in lines if "截止" in line or "到期" in line or find_datetime(line)]
    if len(due_like_lines) > 1:
        return lines
    return [normalized]


def extract_field(text: str, field: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        for pattern in FIELD_PATTERNS[field]:
            match = pattern.search(stripped)
            if match:
                return trim_trailing_metadata(match.group(1))
    return ""


def guess_platform(text: str) -> str:
    if "长江雨课堂" in text or "雨课堂" in text:
        return "长江雨课堂"
    if "小雅" in text:
        return "小雅"
    return ""


def guess_title(text: str) -> str:
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if re.match(r"^(课程名称|课程|科目|班课|平台|来源|截止|截止时间|到期)\s*[:：]", candidate):
            continue
        candidate = re.sub(r"(?:截止|到期)(?:时间|日期)?\s*[:：]?\s*.*$", "", candidate).strip()
        candidate = re.sub(r"^(?:长江雨课堂|雨课堂|小雅)\s*[-—:：]\s*", "", candidate).strip()
        if candidate:
            return candidate
    return ""


def trim_trailing_metadata(value: str) -> str:
    value = re.sub(r"\s+(?:截止|到期)(?:时间|日期)?\s*[:：]?\s*.*$", "", value.strip())
    return value


def clean_candidate(value: str | None) -> str:
    return " ".join((value or "").strip().split())
