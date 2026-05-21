from __future__ import annotations

import re
from datetime import datetime, time

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.config_loader import KnownCourseConfig
from homework_watcher.status import normalize_status


XIAOYA_PLATFORM_LABEL = "小雅"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?")
STATUS_RE = re.compile(r"进行中|未开始|已完成|已截止|未提交|待完成|未完成|已提交|已批阅")
NOISE_LINES = {
    "标题",
    "位置",
    "任务类型",
    "状态",
    "发布方式",
    "分配对象",
    "发布时间",
    "开始时间",
    "截止时间",
    "操作",
    "进入任务",
    "全部任务",
    "自主观看",
    "课堂练习",
    "作业",
    "测验",
    "问卷",
    "讨论",
    "仅关注待完成任务",
}


def parse_xiaoya_task_text(
    text: str, *, course: str, task_url: str, course_id: str = ""
) -> list[AssignmentCandidate]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[AssignmentCandidate] = []
    seen: set[tuple[str, datetime]] = set()

    for index, line in enumerate(lines):
        title = extract_title_from_line(line)
        if not title:
            continue
        window = " ".join(lines[index : index + 10])
        status_match = STATUS_RE.search(window)
        dates = DATE_RE.findall(window)
        if not status_match or not dates:
            continue
        due_at = parse_xiaoya_due_at(dates[-1])
        status_raw = status_match.group(0)
        if is_course_summary(title=title, course=course, status_raw=status_raw, due_at=due_at):
            continue
        key = (title, due_at)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            AssignmentCandidate(
                platform=XIAOYA_PLATFORM_LABEL,
                course=course,
                title=title,
                status_raw=status_raw,
                due_at=due_at,
                url=task_url,
                source_key=f"xiaoya:{course_id or course}:{title}:{due_at.isoformat(timespec='seconds')}",
                raw_snapshot=sanitize_snapshot(window[:500]),
            )
        )

    return candidates


def extract_title_from_line(line: str) -> str:
    text = line.strip()
    if not text or text in NOISE_LINES:
        return ""
    if text.startswith("共") and ("页" in text or "条" in text):
        return ""
    if DATE_RE.fullmatch(text):
        return ""
    if STATUS_RE.fullmatch(text):
        return ""
    if "作业" in text:
        match = re.match(r"(?P<title>.+?)\s*\\?\s*作业(?:\s|$)", text)
        if match:
            return normalize_title(match.group("title"))
    if STATUS_RE.search(text):
        before_status = STATUS_RE.split(text, maxsplit=1)[0]
        return normalize_title(before_status.replace("\\", " ").replace("作业", " "))
    if len(text) > 80:
        return ""
    return normalize_title(text)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip(" /\\|")


def parse_xiaoya_due_at(value: str) -> datetime:
    cleaned = re.sub(r"\s+", " ", value.strip())
    if len(cleaned) == 10:
        return datetime.combine(datetime.strptime(cleaned, "%Y-%m-%d").date(), time(23, 59, 59))
    if len(cleaned) == 16:
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M")
    return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")


def is_course_summary(*, title: str, course: str, status_raw: str, due_at: datetime) -> bool:
    if normalize_title(title) != normalize_title(course):
        return False
    if normalize_status(status_raw) == "unknown":
        return True
    return due_at.time() == time(0, 0, 0)


def sanitize_snapshot(value: str) -> str:
    sanitized = re.sub(r"(?i)(cookie|authorization|token|password)\s*[:=]\s*\S+", r"\1=<redacted>", value)
    return sanitized[:1000]


class XiaoyaScanner:
    platform_key = "xiaoya"

    def scan_known_course_text(self, course: KnownCourseConfig, text: str) -> list[AssignmentCandidate]:
        return parse_xiaoya_task_text(
            text,
            course=course.course,
            task_url=course.task_url,
            course_id=course.course_id,
        )
