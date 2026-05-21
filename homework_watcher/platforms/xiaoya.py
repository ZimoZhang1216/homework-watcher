from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .base import (
    DEFAULT_CANDIDATE_SELECTORS,
    CandidateBlock,
    LoginRequiredError,
    PageStructureChangedError,
    PlatformAssignment,
    PlaywrightPlatformAdapter,
    PlaywrightUnavailableError,
    ProgressCallback,
    compact_text,
    emit_progress,
    load_playwright,
    safe_body_text,
)
from ..datetime_utils import find_datetime


FULL_DATETIME_RE = re.compile(
    r"20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*(?:日)?\s+"
    r"\d{1,2}\s*[:：]\s*\d{1,2}(?:\s*[:：]\s*\d{1,2})?"
)
LOADING_TEXT_RE = re.compile(r"正在加载应用|加载应用|请稍候|loading", re.IGNORECASE)
TASK_WORD_RE = re.compile(r"作业|任务|实习|练习|测验|问卷|讨论|提交")
STATUS_RE = re.compile(
    r"进行中|未提交|待完成|未完成|已截止但未完成|已完成|已提交|已批改|已批阅|未开始|未开放|未到开始时间"
)
COURSE_PATH_RE = re.compile(r"(/app/jx-web/mycourse/[^\"'<\s]+|/mycourse/[^\"'<\s]+)")
COURSE_ID_RE = re.compile(
    r"(?:courseId|course_id|courseid|classroomId|classroom_id|clazzId|classId|courseCode|resourceId|id)"
    r"[\"'\s:=_-]+([1-9]\d{10,})",
    re.IGNORECASE,
)
DEBUG_DIR_ENV = "HW_XIAOYA_DEBUG_DIR"
STRUCTURE_CHEMISTRY_COURSE_ID = os.environ.get("HW_XIAOYA_STRUCTURE_COURSE_ID", "6902426124991620398")
KNOWN_COURSE_IDS = {"结构化学": STRUCTURE_CHEMISTRY_COURSE_ID} if STRUCTURE_CHEMISTRY_COURSE_ID else {}
CHROMIUM_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
SENSITIVE_KEY_RE = re.compile(
    r"cookie|authorization|token|password|passwd|secret|session|credential|key|ticket|csrf|jwt|access|refresh|signature|sid|openid",
    re.IGNORECASE,
)
JSON_TASK_HINT_RE = re.compile(
    r"task|title|name|status|state|due|deadline|end|close|finish|作业|任务|截止|状态|标题",
    re.IGNORECASE,
)
TITLE_KEYS = {
    "title",
    "name",
    "taskName",
    "taskTitle",
    "assignmentName",
    "assignmentTitle",
    "homeworkTitle",
    "homeworkName",
    "activityName",
    "workName",
    "resourceName",
    "questionnaireName",
}
STATUS_KEYS = {
    "status",
    "state",
    "submitStatus",
    "finishStatus",
    "completeStatus",
    "completionStatus",
    "taskStatus",
    "progress",
}
DUE_KEYS = {
    "due_at",
    "dueAt",
    "deadline",
    "deadlineTime",
    "submitEndTime",
    "endTime",
    "endDate",
    "closeTime",
    "finishTime",
    "expireTime",
    "limitTime",
    "截止时间",
}
URL_KEYS = {
    "url",
    "link",
    "href",
    "jumpUrl",
    "jump_url",
    "taskUrl",
    "task_url",
    "resourceUrl",
    "resource_url",
    "activityUrl",
    "activity_url",
}
URL_KEY_CANONICAL = {re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", key.casefold()) for key in URL_KEYS}
MAX_DEBUG_ARRAY_ITEMS = 80
MAX_DEBUG_STRING_LENGTH = 1_000


@dataclass(frozen=True)
class CourseEntry:
    name: str
    page_number: int
    task_url: str = ""


class XiaoyaDebugDumper:
    def __init__(self, root: Path | str | None = None):
        configured = root if root is not None else os.environ.get(DEBUG_DIR_ENV, "")
        self.root = Path(configured).expanduser() if configured else None
        self.counter = 0
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def dump_page(self, page, stage: str, *, course: str = "", course_id: str = "") -> None:
        if self.root is None:
            return
        prefix = self._next_prefix(stage, course=course, course_id=course_id)
        url = sanitize_url(getattr(page, "url", ""))
        try:
            body = redact_sensitive_text(safe_body_text(page))
        except Exception as exc:
            body = f"<failed to read body text: {exc}>"
        try:
            html = redact_sensitive_text(page.content())
        except Exception as exc:
            html = f"<failed to read page content: {exc}>"
        (self.root / f"{prefix}.url").write_text(url, encoding="utf-8")
        (self.root / f"{prefix}.txt").write_text(f"URL: {url}\n\n{body}", encoding="utf-8")
        (self.root / f"{prefix}.html").write_text(html, encoding="utf-8")
        try:
            page.screenshot(path=str(self.root / f"{prefix}.png"), full_page=True, timeout=5_000)
        except Exception as exc:
            (self.root / f"{prefix}.png.error.txt").write_text(str(exc), encoding="utf-8")

    def dump_json_candidate(self, stage: str, payload: dict, *, course: str = "", course_id: str = "") -> None:
        if self.root is None:
            return
        prefix = self._next_prefix(stage, course=course, course_id=course_id)
        (self.root / f"{prefix}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _next_prefix(self, stage: str, *, course: str = "", course_id: str = "") -> str:
        self.counter += 1
        parts = [f"{self.counter:03d}", safe_filename(stage)]
        if course:
            parts.append(safe_filename(course))
        if course_id:
            parts.append(safe_filename(course_id))
        return "-".join(part for part in parts if part)


class XiaoyaNetworkRecorder:
    def __init__(self, debug: XiaoyaDebugDumper, *, course: str, platform: str, page_url: str):
        self.debug = debug
        self.course = course
        self.platform = platform
        self.page_url = page_url
        self.candidates: list[dict] = []

    def attach(self, page) -> None:
        page.on("response", self._handle_response)

    def detach(self, page) -> None:
        try:
            page.remove_listener("response", self._handle_response)
        except Exception:
            pass

    def _handle_response(self, response) -> None:
        try:
            request = response.request
            resource_type = getattr(request, "resource_type", "")
            content_type = response.headers.get("content-type", "")
            if resource_type not in {"xhr", "fetch"} and "json" not in content_type.lower():
                return
            if "json" not in content_type.lower():
                return
            data = response.json()
        except Exception:
            return
        if not json_has_task_hints(data):
            return
        payload = {
            "url": sanitize_url(response.url),
            "method": getattr(response.request, "method", ""),
            "status": response.status,
            "content_type": response.headers.get("content-type", ""),
            "request_payload_shape": request_payload_shape(response.request),
            "response_json": sanitize_json(data),
        }
        self.candidates.append(payload)
        self.debug.dump_json_candidate("network-candidate", payload, course=self.course)

    def assignments(self, *, fallback_url: str) -> list[PlatformAssignment]:
        parsed: list[PlatformAssignment] = []
        for candidate in self.candidates:
            parsed.extend(
                parse_xiaoya_json_assignments(
                    candidate.get("response_json"),
                    course=self.course,
                    platform=self.platform,
                    fallback_url=fallback_url,
                )
            )
        return dedupe_assignments(parsed)


class XiaoyaAdapter(PlaywrightPlatformAdapter):
    slug = "xiaoya"
    platform_name = "小雅"
    start_url = os.environ.get(
        "HW_XIAOYA_URL",
        "https://nankai.ai-augmented.com/app/jx-web/mycourse",
    )
    scan_timeout_seconds = int(os.environ.get("HW_XIAOYA_SCAN_TIMEOUT_SECONDS", "600"))
    course_timeout_seconds = int(os.environ.get("HW_XIAOYA_COURSE_TIMEOUT_SECONDS", "45"))
    max_course_pages = int(os.environ.get("HW_XIAOYA_MAX_COURSE_PAGES", "30"))
    max_courses = int(os.environ.get("HW_XIAOYA_MAX_COURSES", "100"))
    max_task_pages = int(os.environ.get("HW_XIAOYA_MAX_TASK_PAGES", "20"))
    candidate_selectors = [
        "[class*='homework' i]",
        "[class*='assignment' i]",
        "[class*='task' i]",
        "[class*='work' i]",
        "[class*='activity' i]",
        "[class*='course' i]",
        *DEFAULT_CANDIDATE_SELECTORS,
    ]

    def _launch_context(self, playwright, *, headless: bool):
        context = super()._launch_context(playwright, headless=headless)
        context.set_default_timeout(5_000)
        prefer_student_course_tab(context)
        return context

    def fetch_assignments(
        self,
        *,
        headless: bool = True,
        progress: ProgressCallback = None,
    ) -> list[PlatformAssignment]:
        sync_playwright, playwright_error = load_playwright()
        ensure_profile_available(self.user_data_dir)
        debug = XiaoyaDebugDumper()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=headless)
            deadline = time.monotonic() + self.scan_timeout_seconds
            try:
                page = context.pages[0] if context.pages else context.new_page()
                open_xiaoya_page(page, self.url, deadline=deadline, progress=progress, label="打开课程列表")
                debug.dump_page(page, "course-list")
                if self.is_login_required(page):
                    raise LoginRequiredError(
                        f"{self.platform_name} 登录状态已失效。请在网站里重新打开小雅登录。"
                    )

                course_entries = collect_course_entries(
                    page,
                    progress=progress,
                    platform_name=self.platform_name,
                    deadline=deadline,
                    max_pages=self.max_course_pages,
                    max_courses=self.max_courses,
                )
                emit_progress(progress, f"{self.platform_name}：发现 {len(course_entries)} 门课程")
                if not course_entries:
                    fallback = self.parse_candidate_blocks(
                        [CandidateBlock(safe_body_text(page), page.url)],
                        fallback_url=page.url,
                    )
                    if fallback:
                        return fallback
                    raise PageStructureChangedError(
                        f"{self.platform_name} 未能识别课程列表。当前页面：{page.url}"
                    )

                assignments: list[PlatformAssignment] = []
                skipped: list[str] = []
                for index, course in enumerate(course_entries):
                    ensure_scan_time_left(deadline, f"扫描课程 {index + 1}/{len(course_entries)}")
                    course_deadline = min(deadline, time.monotonic() + self.course_timeout_seconds)
                    emit_progress(
                        progress,
                        f"{self.platform_name}：扫描课程 {index + 1}/{len(course_entries)} {course.name}",
                    )
                    try:
                        if not course.task_url:
                            debug.dump_page(page, "course-without-task-url", course=course.name)
                            raise PlaywrightUnavailableError("未能定位课程任务页 URL，已跳过以避免卡住")
                        items = scan_course_tasks(
                            page,
                            course,
                            start_url=self.url,
                            platform=self.platform_name,
                            deadline=course_deadline,
                            max_pages=self.max_task_pages,
                            progress=progress,
                            debug=debug,
                            course_index=index + 1,
                        )
                    except PlaywrightUnavailableError as exc:
                        debug.dump_page(page, "course-scan-error", course=course.name)
                        if time.monotonic() >= deadline:
                            raise
                        skipped.append(f"{course.name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course.name}，原因：{truncate(str(exc), 42)}")
                        continue
                    except Exception as exc:
                        debug.dump_page(page, "course-scan-error", course=course.name)
                        skipped.append(f"{course.name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course.name}，原因：{truncate(str(exc), 42)}")
                        continue
                    if items:
                        emit_progress(progress, f"{self.platform_name}：{course.name} 识别 {len(items)} 条任务")
                    assignments.extend(items)

                assignments.extend(
                    self._scan_missing_known_course_tasks(
                        page,
                        assignments,
                        deadline=deadline,
                        progress=progress,
                        debug=debug,
                        start_index=len(course_entries) + 1,
                    )
                )
                assignments = dedupe_assignments(assignments)
                if assignments:
                    suffix = f"，跳过 {len(skipped)} 门课程" if skipped else ""
                    emit_progress(progress, f"{self.platform_name}：完成，识别 {len(assignments)} 条任务{suffix}")
                    return assignments
                if skipped:
                    emit_progress(progress, f"{self.platform_name}：完成，跳过 {len(skipped)} 门课程，未发现待记录任务")
                else:
                    emit_progress(progress, f"{self.platform_name}：完成，未发现待记录任务")
                return []
            except (LoginRequiredError, PageStructureChangedError):
                raise
            except playwright_error as exc:
                raise PlaywrightUnavailableError(f"{self.platform_name} 扫描失败：{exc}") from exc
            finally:
                context.close()

    def _scan_missing_known_course_tasks(
        self,
        page,
        assignments: list[PlatformAssignment],
        *,
        deadline: float,
        progress: ProgressCallback,
        debug: XiaoyaDebugDumper,
        start_index: int,
    ) -> list[PlatformAssignment]:
        fallback_items: list[PlatformAssignment] = []
        for offset, (course_name, course_id) in enumerate(KNOWN_COURSE_IDS.items()):
            current = assignments + fallback_items
            if has_real_course_assignments(current, course_name):
                continue
            ensure_scan_time_left(deadline, f"补扫已知课程 {course_name}")
            course_deadline = min(deadline, time.monotonic() + self.course_timeout_seconds)
            course = CourseEntry(
                name=course_name,
                page_number=0,
                task_url=task_url_for_course_id(self.url, course_id),
            )
            emit_progress(progress, f"{self.platform_name}：补扫已知课程 {course_name}")
            try:
                items = scan_course_tasks(
                    page,
                    course,
                    start_url=self.url,
                    platform=self.platform_name,
                    deadline=course_deadline,
                    max_pages=self.max_task_pages,
                    progress=progress,
                    debug=debug,
                    course_index=start_index + offset,
                )
            except PlaywrightUnavailableError as exc:
                debug.dump_page(page, "known-course-scan-error", course=course_name, course_id=course_id)
                emit_progress(progress, f"{self.platform_name}：补扫 {course_name} 失败：{truncate(str(exc), 42)}")
                continue
            except Exception as exc:
                debug.dump_page(page, "known-course-scan-error", course=course_name, course_id=course_id)
                emit_progress(progress, f"{self.platform_name}：补扫 {course_name} 失败：{truncate(str(exc), 42)}")
                continue
            if items:
                emit_progress(progress, f"{self.platform_name}：{course_name} 补扫识别 {len(items)} 条任务")
                fallback_items.extend(items)
        return fallback_items

    def fetch_structure_debug_assignments(
        self,
        *,
        headless: bool = True,
        progress: ProgressCallback = None,
    ) -> list[PlatformAssignment]:
        """Open the known structure chemistry task page and parse it through all Xiaoya paths."""
        sync_playwright, playwright_error = load_playwright()
        ensure_profile_available(self.user_data_dir)
        debug = XiaoyaDebugDumper()
        course = CourseEntry(
            name="结构化学",
            page_number=1,
            task_url=task_url_for_course_id(self.url, STRUCTURE_CHEMISTRY_COURSE_ID),
        )
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=headless)
            deadline = time.monotonic() + min(self.scan_timeout_seconds, self.course_timeout_seconds)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                return scan_course_tasks(
                    page,
                    course,
                    start_url=self.url,
                    platform=self.platform_name,
                    deadline=deadline,
                    max_pages=self.max_task_pages,
                    progress=progress,
                    debug=debug,
                    course_index=1,
                )
            except playwright_error as exc:
                raise PlaywrightUnavailableError(f"{self.platform_name} 结构化学诊断失败：{exc}") from exc
            finally:
                context.close()


def fetch_assignments(*, headless: bool = True, progress: ProgressCallback = None):
    """Fetch assignments from Xiaoya without submitting anything."""
    return XiaoyaAdapter().fetch_assignments(headless=headless, progress=progress)


def debug_structure_assignments(
    *,
    headless: bool = True,
    progress: ProgressCallback = None,
) -> list[PlatformAssignment]:
    """Debug the known Structure Chemistry Xiaoya task page."""
    return XiaoyaAdapter().fetch_structure_debug_assignments(headless=headless, progress=progress)


def safe_filename(value: str) -> str:
    value = compact_text(str(value or "")).replace("\n", " ")
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", value).strip("-._")
    return (value or "page")[:80]


def sanitize_url(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    sanitized_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        sanitized_pairs.append((key, "<redacted>" if SENSITIVE_KEY_RE.search(key) else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized_pairs), parts.fragment))


def request_payload_shape(request) -> Any:
    try:
        post_data = getattr(request, "post_data", None)
        if callable(post_data):
            post_data = post_data()
    except Exception:
        return None
    if not post_data:
        return None
    try:
        parsed = json.loads(post_data)
    except Exception:
        try:
            pairs = parse_qsl(str(post_data), keep_blank_values=True)
        except Exception:
            return {"type": "text", "length": len(str(post_data))}
        if not pairs:
            return {"type": "text", "length": len(str(post_data))}
        return {"type": "form", "keys": [key for key, _ in pairs]}
    return json_shape(parsed)


def json_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "list", "length": len(value), "item": json_shape(value[0]) if value else None}
    return type(value).__name__


def sanitize_json(value: Any, *, key: str = "") -> Any:
    if key and SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            child_key = str(raw_key)
            if is_url_json_key(child_key) and isinstance(item, str):
                sanitized[child_key] = sanitize_url(item)
            else:
                sanitized[child_key] = sanitize_json(item, key=child_key)
        return sanitized
    if isinstance(value, list):
        items = [sanitize_json(item, key=key) for item in value[:MAX_DEBUG_ARRAY_ITEMS]]
        if len(value) > MAX_DEBUG_ARRAY_ITEMS:
            items.append({"_truncated": len(value) - MAX_DEBUG_ARRAY_ITEMS})
        return items
    if isinstance(value, str):
        if looks_like_sensitive_string(value):
            return "<redacted>"
        if len(value) > MAX_DEBUG_STRING_LENGTH:
            return value[:MAX_DEBUG_STRING_LENGTH] + f"...<truncated {len(value) - MAX_DEBUG_STRING_LENGTH} chars>"
    return value


def looks_like_sensitive_string(value: str) -> bool:
    if re.match(r"^eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", value):
        return True
    if len(value) > 160 and re.fullmatch(r"[A-Za-z0-9_.:/+=-]+", value):
        return True
    return False


def redact_sensitive_text(text: str) -> str:
    sensitive_name = (
        r"authorization|cookie|token|password|passwd|secret|session|credential|ticket|csrf|jwt|"
        r"access[_-]?token|refresh[_-]?token|hw_web_secret_key|hw_web_admin_token|smtp"
    )
    text = re.sub(
        rf"(?i)({sensitive_name})(\s*[:=]\s*)([^\s&\"'<>]+)",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(
        rf"(?i)((?:{sensitive_name})=)([^&\"'<>\\s]+)",
        r"\1<redacted>",
        text,
    )
    return text


def json_has_task_hints(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if JSON_TASK_HINT_RE.search(str(key)):
                return True
            if json_has_task_hints(item, depth=depth + 1):
                return True
    elif isinstance(value, list):
        return any(json_has_task_hints(item, depth=depth + 1) for item in value[:MAX_DEBUG_ARRAY_ITEMS])
    elif isinstance(value, str) and len(value) < 300:
        return bool(JSON_TASK_HINT_RE.search(value))
    return False


def parse_xiaoya_json_assignments(
    value: Any,
    *,
    course: str,
    platform: str,
    fallback_url: str,
) -> list[PlatformAssignment]:
    assignments: list[PlatformAssignment] = []
    for node in iter_json_dicts(value):
        item = assignment_from_json_node(node, course=course, platform=platform, fallback_url=fallback_url)
        if item is not None:
            assignments.append(item)
    return dedupe_assignments(assignments)


def iter_json_dicts(value: Any, *, depth: int = 0):
    if depth > 10:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_json_dicts(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_dicts(item, depth=depth + 1)


def assignment_from_json_node(
    node: dict,
    *,
    course: str,
    platform: str,
    fallback_url: str,
) -> PlatformAssignment | None:
    raw_title = first_key_value(node, TITLE_KEYS)
    due_value = first_key_value(node, DUE_KEYS)
    if raw_title is None or due_value is None:
        return None
    title = clean_xiaoya_title(str(raw_title))
    if not title or looks_like_xiaoya_metadata(title):
        return None
    due_at = datetime_from_json_value(due_value)
    if due_at is None:
        return None
    raw_status = first_key_value(node, STATUS_KEYS)
    status = guess_row_status([str(raw_status)]) if raw_status is not None else "未知"
    raw_url = first_key_value(node, URL_KEYS)
    url = fallback_url
    if isinstance(raw_url, str) and raw_url.strip():
        url = sanitize_url(urljoin(fallback_url, raw_url.strip()))
    return PlatformAssignment(
        title=title,
        course=course,
        platform=platform,
        due_at=due_at,
        status=status,
        url=url,
    )


def first_key_value(node: dict, keys: set[str]) -> Any:
    exact = {canonical_json_key(key): key for key in keys}
    for raw_key, value in node.items():
        if canonical_json_key(str(raw_key)) in exact and value not in (None, ""):
            return value
    return None


def canonical_json_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def is_url_json_key(value: str) -> bool:
    return canonical_json_key(value) in URL_KEY_CANONICAL


def datetime_from_json_value(value: Any):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return find_datetime(value)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        if 946684800 <= timestamp <= 4102444800:
            try:
                return datetime.fromtimestamp(timestamp)
            except (OSError, OverflowError, ValueError):
                return None
    return None


def ensure_profile_available(profile_dir: Path) -> None:
    active_pids = find_active_profile_processes(profile_dir)
    if active_pids:
        raise PlaywrightUnavailableError(
            "小雅远程登录浏览器仍在运行，请先释放远程登录会话再扫描。"
            f"占用进程：{', '.join(str(pid) for pid in active_pids[:6])}"
        )
    remove_stale_chromium_profile_locks(profile_dir)


def find_active_profile_processes(profile_dir: Path) -> list[int]:
    profile = str(profile_dir.expanduser())
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except Exception:
        return []
    current_pid = os.getpid()
    pids: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, args = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if browser_process_uses_profile(args, profile):
            pids.append(pid)
    return pids


def browser_process_uses_profile(args: str, profile: str) -> bool:
    lower_args = args.lower()
    if "chrome" not in lower_args and "chromium" not in lower_args:
        return False
    return f"--user-data-dir={profile}" in args or f"--user-data-dir {profile}" in args


def remove_stale_chromium_profile_locks(profile_dir: Path) -> None:
    for lock_name in CHROMIUM_PROFILE_LOCK_NAMES:
        lock_path = profile_dir / lock_name
        try:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink()
        except OSError:
            pass


def ensure_scan_time_left(deadline: float, step: str) -> None:
    if time.monotonic() >= deadline:
        raise PlaywrightUnavailableError(f"小雅扫描超时，停在：{step}")


def remaining_timeout_ms(deadline: float, default_ms: int) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise PlaywrightUnavailableError("小雅扫描超时")
    return max(800, min(default_ms, remaining))


def truncate(value: str, length: int) -> str:
    value = compact_text(value)
    if len(value) <= length:
        return value
    return value[: max(0, length - 1)] + "…"


def prefer_student_course_tab(context) -> None:
    context.add_init_script(
        """
        (() => {
          try {
            if (!location.hostname.endsWith("ai-augmented.com")) return;
            sessionStorage.setItem("course_home_tabs_current", "study");
          } catch (_) {}
        })();
        """
    )


def open_xiaoya_page(page, url: str, *, deadline: float, progress: ProgressCallback, label: str) -> None:
    emit_progress(progress, f"小雅：{label}")
    page.goto(url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, 20_000))
    wait_for_xiaoya_shell(page, timeout_ms=8_000, settle_ms=600)
    recover_xiaoya_loading_page(page, deadline=deadline, progress=progress)
    ensure_student_course_tab(page)
    wait_for_xiaoya_shell(page, timeout_ms=5_000, settle_ms=500)
    recover_xiaoya_loading_page(page, deadline=deadline, progress=progress)


def ensure_student_course_tab(page) -> None:
    try:
        page.evaluate(
            """
            () => {
              sessionStorage.setItem("course_home_tabs_current", "study");
              const tabs = Array.from(document.querySelectorAll("[role='tab'], .ant-tabs-tab, .ant-tabs-tab-btn"));
              const target = tabs.find((node) => /我.*(听|学).*(课|课程)/.test((node.innerText || "").trim()));
              if (target) {
                const tab = target.closest("[role='tab'], .ant-tabs-tab") || target;
                const selected = tab.getAttribute("aria-selected") === "true"
                  || tab.classList.contains("ant-tabs-tab-active");
                if (!selected) tab.click();
              }
            }
            """
        )
        page.wait_for_timeout(500)
    except Exception:
        pass


def recover_xiaoya_loading_page(
    page,
    *,
    deadline: float | None = None,
    progress: ProgressCallback = None,
    attempts: int = 2,
) -> None:
    if not xiaoya_page_is_loading(page):
        return
    emit_progress(progress, "小雅：页面停在加载状态，清理本地缓存并重载")
    for attempt in range(attempts):
        if deadline is not None:
            ensure_scan_time_left(deadline, "恢复小雅加载页")
        clear_xiaoya_runtime_cache(page)
        try:
            timeout = remaining_timeout_ms(deadline, 8_000) if deadline is not None else 8_000
            page.reload(wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            pass
        wait_for_xiaoya_shell(page, timeout_ms=6_000, settle_ms=600)
        if not xiaoya_page_is_loading(page):
            emit_progress(progress, "小雅：加载状态已恢复")
            return
        if attempt == 0:
            emit_progress(progress, "小雅：首次重载仍未恢复，再试一次")
    raise PlaywrightUnavailableError(
        "小雅页面卡在“正在加载应用”。已自动清理缓存并重载，仍未恢复。"
        "请重新打开小雅登录；如果仍失败，通常是小雅静态资源在服务器网络下不可用。"
    )


def xiaoya_page_is_loading(page) -> bool:
    try:
        if page.locator(".aia_course_card, tbody tr, input[type='password']").count() > 0:
            return False
    except Exception:
        pass
    return xiaoya_text_is_loading(safe_body_text(page))


def xiaoya_text_is_loading(text: str) -> bool:
    return bool(LOADING_TEXT_RE.search(compact_text(text)))


def clear_xiaoya_runtime_cache(page) -> None:
    try:
        page.evaluate(
            """
            async () => {
              try {
                if ('serviceWorker' in navigator) {
                  const registrations = await navigator.serviceWorker.getRegistrations();
                  await Promise.all(registrations.map(registration => registration.unregister()));
                }
              } catch (_) {}
              try {
                if ('caches' in window) {
                  const names = await caches.keys();
                  await Promise.all(names.map(name => caches.delete(name)));
                }
              } catch (_) {}
              try {
                sessionStorage.setItem("course_home_tabs_current", "study");
              } catch (_) {}
            }
            """
        )
    except Exception:
        pass


def wait_for_xiaoya_shell(page, *, timeout_ms: int, settle_ms: int) -> None:
    try:
        page.wait_for_function(
            """() => {
              const body = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim();
              const hasCourses = document.querySelectorAll('.aia_course_card').length > 0;
              const hasTasks = document.querySelectorAll('tbody tr, .ant-list-item').length > 0;
              const hasLogin = document.querySelectorAll('input[type="password"], input[name*="password" i]').length > 0;
              const stillLoading = /正在加载应用|加载应用|请稍候|loading/i.test(body);
              return hasCourses || hasTasks || hasLogin || !stillLoading;
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:
        pass


def collect_visible_course_names(page) -> list[str]:
    return [entry.name for entry in collect_course_entries(page)]


def collect_course_entries(
    page,
    *,
    progress: ProgressCallback = None,
    platform_name: str = "小雅",
    deadline: float | None = None,
    max_pages: int = 30,
    max_courses: int = 100,
) -> list[CourseEntry]:
    entries: list[CourseEntry] = []
    seen: set[str] = set()
    page_number = 1
    for _ in range(max_pages):
        if deadline is not None:
            ensure_scan_time_left(deadline, "读取课程列表")
        page_entries = collect_current_page_course_entries(page, page_number=page_number)
        for entry in page_entries:
            key = entry.name.casefold()
            if not entry.name or key in seen:
                continue
            if not entry.task_url:
                emit_progress(progress, f"{platform_name}：课程 {entry.name} 未识别到任务页 URL，后续将跳过")
            seen.add(key)
            entries.append(entry)
            if len(entries) >= max_courses:
                emit_progress(progress, f"{platform_name}：课程数量达到上限 {max_courses}，停止继续翻页")
                return entries
        emit_progress(progress, f"{platform_name}：读取课程列表第 {page_number} 页，累计 {len(entries)} 门")
        if not click_next_course_page(page):
            break
        page_number += 1
    return entries


def collect_current_page_course_names(page) -> list[str]:
    return [entry.name for entry in collect_current_page_course_entries(page, page_number=1)]


def collect_current_page_course_entries(page, *, page_number: int) -> list[CourseEntry]:
    raw_entries = evaluate_course_cards(page)
    entries: list[CourseEntry] = []
    for raw in raw_entries:
        if isinstance(raw, str):
            name = extract_course_name(raw)
            task_url = ""
        elif isinstance(raw, dict):
            name = extract_course_name(str(raw.get("name") or raw.get("text") or ""))
            task_url = normalize_course_task_url(
                str(raw.get("url") or ""),
                html="\n".join(
                    [
                        str(raw.get("html") or ""),
                        str(raw.get("attributes") or ""),
                        str(raw.get("dataset") or ""),
                    ]
                ),
                base_url=page.url,
            )
        else:
            continue
        if name:
            if not task_url:
                course_id = known_course_id_for(name)
                if course_id:
                    task_url = task_url_for_course_id(page.url, course_id)
            entries.append(CourseEntry(name=name, page_number=page_number, task_url=task_url))
    return entries


def evaluate_course_cards(page) -> list[dict] | list[str]:
    try:
        return page.evaluate(
            """
            () => Array.from(document.querySelectorAll('.aia_course_card'))
              .slice(0, 120)
              .map(card => {
                const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
                const link = card.querySelector('a[href]');
                const attrs = ['href', 'data-url', 'data-href', 'data-path', 'to'];
                const url = (link && link.getAttribute('href'))
                  || attrs.map(name => card.getAttribute(name)).find(Boolean)
                  || '';
                const nodes = [card, ...Array.from(card.querySelectorAll('*')).slice(0, 80)];
                const attributes = nodes.flatMap(node => Array.from(node.attributes || [])
                  .map(attr => `${attr.name}=${attr.value}`)).join('\\n');
                return {
                  name: card.getAttribute('data-xy-click-pt-name') || '',
                  text: compact(card.innerText || card.textContent || ''),
                  url,
                  attributes,
                  dataset: JSON.stringify(card.dataset || {}),
                  html: card.outerHTML || '',
                };
              })
            """
        )
    except Exception:
        return []


def normalize_course_task_url(raw_url: str, *, html: str, base_url: str) -> str:
    candidates = [raw_url]
    candidates.extend(match.group(1) for match in COURSE_PATH_RE.finditer(html))
    for candidate in candidates:
        value = candidate.strip()
        if not value or value.startswith(("javascript:", "#")):
            continue
        if "/mycourse/" not in value:
            continue
        return task_url_for(urljoin(base_url, value))
    course_id = find_course_id(html)
    if course_id:
        return task_url_for_course_id(base_url, course_id)
    return ""


def task_url_for_course_id(base_url: str, course_id: str) -> str:
    base = urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, f"/app/jx-web/mycourse/{course_id}/task", "", ""))


def course_id_from_task_url(url: str) -> str:
    match = re.search(r"/mycourse/([^/?#]+)/task", urlsplit(url).path)
    return match.group(1) if match else ""


def known_course_id_for(course_name: str) -> str:
    normalized = compact_text(course_name)
    return KNOWN_COURSE_IDS.get(normalized, "")


def has_real_course_assignments(assignments: list[PlatformAssignment], course_name: str) -> bool:
    target = compact_text(course_name)
    return any(
        item.platform == "小雅"
        and compact_text(item.course) == target
        and compact_text(item.title) != target
        for item in assignments
    )


def find_course_id(text: str) -> str:
    for match in COURSE_ID_RE.finditer(text):
        return match.group(1)
    for match in re.finditer(r"\b([1-9]\d{14,})\b", text):
        return match.group(1)
    return ""


def extract_course_name(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    skip_exact = {"校内公开", "教务开课", "正在进行", "即将开课", "已结束", "LPOC"}
    skip_prefixes = ("20", "学院：")
    for line in lines:
        if line in skip_exact or line.startswith(skip_prefixes):
            continue
        if re.search(r"次$|人$|教师|老师|许晓", line):
            continue
        return line
    compacted = " ".join(lines)
    match = re.search(r"教务开课\s+(.+?)\s+学院：", compacted)
    return match.group(1).strip() if match else ""


def click_next_course_page(page) -> bool:
    next_button = page.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
    if next_button.count() == 0:
        return False
    before = visible_course_signature(page)
    try:
        next_button.click(timeout=3_000)
    except Exception:
        return False
    wait_for_course_page_update(page, before)
    return visible_course_signature(page) != before


def visible_course_signature(page) -> str:
    return "\n".join(collect_current_page_course_names(page))


def wait_for_course_page_update(page, previous_signature: str) -> None:
    try:
        page.wait_for_function(
            """previous => {
              const cards = Array.from(document.querySelectorAll('.aia_course_card'));
              const names = cards.map(card => card.getAttribute('data-xy-click-pt-name') || card.innerText || '')
                .map(text => text.replace(/\\s+/g, ' ').trim());
              return names.length > 0 && names.join('\\n') !== previous;
            }""",
            previous_signature,
            timeout=4_000,
        )
    except Exception:
        pass
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass


def scan_course_tasks(
    page,
    course: CourseEntry,
    *,
    start_url: str,
    platform: str,
    deadline: float,
    max_pages: int,
    progress: ProgressCallback,
    debug: XiaoyaDebugDumper | None = None,
    course_index: int = 0,
) -> list[PlatformAssignment]:
    debug = debug or XiaoyaDebugDumper()
    if course.task_url:
        target_url = course.task_url
        emit_progress(progress, f"{platform}：进入任务页 {course.name}")
    else:
        raise PlaywrightUnavailableError("未能定位课程任务页 URL，已跳过以避免卡住")
    recorder = XiaoyaNetworkRecorder(debug, course=course.name, platform=platform, page_url=target_url)
    recorder.attach(page)
    try:
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, 15_000))
            wait_for_xiaoya_shell(page, timeout_ms=8_000, settle_ms=700)
            recover_xiaoya_loading_page(page, deadline=deadline, progress=progress)
            debug.dump_page(
                page,
                f"{course_index:03d}-task" if course_index else "task",
                course=course.name,
                course_id=course_id_from_task_url(target_url),
            )
            network_items = recorder.assignments(fallback_url=page.url)
            if network_items:
                emit_progress(progress, f"{platform}：{course.name} 通过接口识别 {len(network_items)} 条任务")
                return network_items
            return parse_xiaoya_task_page(
                page,
                course=course.name,
                platform=platform,
                deadline=deadline,
                max_pages=max_pages,
                debug=debug,
            )
        except Exception:
            debug.dump_page(page, "task-scan-error", course=course.name, course_id=course_id_from_task_url(target_url))
            raise
    finally:
        recorder.detach(page)


def go_to_course_page(page, page_number: int, *, deadline: float | None = None) -> None:
    for _ in range(max(0, page_number - 1)):
        if deadline is not None:
            ensure_scan_time_left(deadline, f"跳转课程列表第 {page_number} 页")
        if not click_next_course_page(page):
            break


def task_url_for(url: str) -> str:
    parsed = urlsplit(url)
    match = re.search(r"(.*/mycourse/[^/]+)(?:/(?:resource|task)(?:/.*)?)?$", parsed.path)
    if match:
        return urlunsplit((parsed.scheme, parsed.netloc, match.group(1) + "/task", "", ""))
    return url.rstrip("/") + "/task"


def parse_xiaoya_task_page(
    page,
    *,
    course: str,
    platform: str,
    deadline: float | None = None,
    max_pages: int = 20,
    debug: XiaoyaDebugDumper | None = None,
) -> list[PlatformAssignment]:
    debug = debug or XiaoyaDebugDumper()
    assignments: list[PlatformAssignment] = []
    for page_index in range(max_pages):
        if deadline is not None:
            ensure_scan_time_left(deadline, "读取任务列表")
        assignments.extend(collect_visible_task_rows(page, course=course, platform=platform))
        assignments.extend(parse_xiaoya_task_text(safe_body_text(page), course=course, platform=platform, url=page.url))
        if page_index >= max_pages - 1:
            break
        try:
            if not click_next_task_page(page, deadline=deadline):
                break
            wait_for_xiaoya_shell(page, timeout_ms=10_000, settle_ms=400)
            debug.dump_page(page, f"task-page-{page_index + 2}", course=course)
        except PlaywrightUnavailableError:
            debug.dump_page(page, "task-pagination-timeout", course=course)
            break
        except Exception:
            debug.dump_page(page, "task-pagination-failed", course=course)
            break
    if assignments:
        return dedupe_assignments(assignments)

    text = safe_body_text(page)
    if "老师还没有发布任务" in text or "暂无" in text:
        return []
    blocks = [CandidateBlock(text, page.url)]
    return XiaoyaAdapter().parse_candidate_blocks(blocks, fallback_url=page.url, fallback_course=course)


def collect_visible_task_rows(page, *, course: str, platform: str) -> list[PlatformAssignment]:
    assignments: list[PlatformAssignment] = []
    for cells in evaluate_task_rows(page):
        item = parse_xiaoya_row(cells, course=course, platform=platform, url=page.url)
        if item is not None:
            assignments.append(item)
    return dedupe_assignments(assignments)


def evaluate_task_rows(page) -> list[list[str]]:
    rows: list[list[str]] = []
    rows.extend(evaluate_dom_task_rows(page))
    rows.extend(evaluate_visual_task_rows(page))
    seen: set[str] = set()
    unique: list[list[str]] = []
    for cells in rows:
        cleaned = collapse_duplicate_cells([compact_text(str(cell)) for cell in cells if compact_text(str(cell))])
        key = "\n".join(cleaned)
        if len(cleaned) < 3 or key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


def evaluate_dom_task_rows(page) -> list[list[str]]:
    try:
        raw_rows = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('tbody tr, .ant-list-item'))
              .slice(0, 240)
              .map(row => {
                const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
                const cells = Array.from(row.querySelectorAll('td, .ant-list-item-meta-title, .ant-list-item-meta-description, [class*="cell"]'))
                  .map(cell => compact(cell.innerText || cell.textContent))
                  .filter(Boolean);
                const text = compact(row.innerText || row.textContent);
                return cells.length ? cells : (text ? [text] : []);
              })
              .filter(row => row.length);
            """
        )
    except Exception:
        return []
    return raw_rows if isinstance(raw_rows, list) else []


def evaluate_visual_task_rows(page) -> list[list[str]]:
    try:
        raw_rows = page.evaluate(
            """
            () => {
              const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
              const cells = Array.from(document.querySelectorAll('.ant-table tbody tr:not(.ant-table-measure-row) td, table tbody tr td'))
                .map(cell => {
                  const rect = cell.getBoundingClientRect();
                  return {
                    text: compact(cell.innerText || cell.textContent),
                    top: Math.round((rect.top + rect.height / 2) / 4) * 4,
                    left: Math.round(rect.left),
                    width: rect.width,
                    height: rect.height,
                  };
                })
                .filter(cell => cell.text && cell.width > 0 && cell.height > 0);
              const groups = new Map();
              for (const cell of cells) {
                if (!groups.has(cell.top)) groups.set(cell.top, []);
                groups.get(cell.top).push(cell);
              }
              return Array.from(groups.values())
                .map(row => row.sort((a, b) => a.left - b.left).map(cell => cell.text))
                .filter(row => row.length >= 3);
            }
            """
        )
    except Exception:
        return []
    return raw_rows if isinstance(raw_rows, list) else []


def collapse_duplicate_cells(cells: list[str]) -> list[str]:
    collapsed: list[str] = []
    for cell in cells:
        if collapsed and collapsed[-1] == cell:
            continue
        collapsed.append(cell)
    half = len(collapsed) // 2
    if half and collapsed[:half] == collapsed[half:]:
        return collapsed[:half]
    return collapsed


def click_next_task_page(page, *, deadline: float | None = None) -> bool:
    if deadline is not None:
        ensure_scan_time_left(deadline, "任务列表翻页")
    next_button = page.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
    if next_button.count() == 0:
        return False
    before = visible_row_signature(page)
    try:
        next_button.click(timeout=remaining_timeout_ms(deadline, 3_000) if deadline else 3_000)
    except Exception:
        return False
    wait_for_task_page_update(page, before)
    return visible_row_signature(page) != before


def visible_row_signature(page) -> str:
    try:
        rows = page.locator("tbody tr")
        values: list[str] = []
        for index in range(min(rows.count(), 3)):
            values.append(compact_text(rows.nth(index).inner_text(timeout=500)))
        return "\n".join(values)
    except Exception:
        return ""


def wait_for_task_page_update(page, previous_signature: str) -> None:
    try:
        page.wait_for_function(
            """previous => {
              const rows = Array.from(document.querySelectorAll('tbody tr')).slice(0, 3);
              const text = rows.map(row => row.innerText.replace(/\\s+/g, ' ').trim()).join('\\n');
              return text && text !== previous;
            }""",
            previous_signature,
            timeout=4_000,
        )
    except Exception:
        pass
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass


def parse_xiaoya_row(
    cells: list[str],
    *,
    course: str,
    platform: str,
    url: str,
    headers: list[str] | None = None,
) -> PlatformAssignment | None:
    cells = [compact_text(cell) for cell in cells if compact_text(cell)]
    if len(cells) < 3:
        return None
    joined = "\n".join(cells)
    if not TASK_WORD_RE.search(joined) and not STATUS_RE.search(joined):
        return None
    due_at = find_xiaoya_row_due(cells, headers=headers)
    if due_at is None:
        return None
    title = title_from_xiaoya_row(cells)
    if not title or title in {"标题", "老师还没有发布任务，敬请期待吧！"}:
        return None
    return PlatformAssignment(
        title=title,
        course=course,
        platform=platform,
        due_at=due_at,
        status=guess_row_status(cells),
        url=url,
    )


def parse_xiaoya_task_text(text: str, *, course: str, platform: str, url: str) -> list[PlatformAssignment]:
    lines = [compact_text(line) for line in text.splitlines() if compact_text(line)]
    assignments: list[PlatformAssignment] = []
    for index, line in enumerate(lines):
        line_title = clean_xiaoya_title(line)
        if looks_like_xiaoya_metadata(line_title) or find_datetime(line_title) is not None:
            continue
        if not TASK_WORD_RE.search(line):
            continue
        block = collect_task_text_block(lines, index)
        if not TASK_WORD_RE.search(block) or not STATUS_RE.search(block):
            continue
        if len(FULL_DATETIME_RE.findall(block)) == 0:
            continue
        cells = split_task_text_block(block)
        item = parse_xiaoya_row(cells, course=course, platform=platform, url=url)
        if item is not None:
            assignments.append(item)
    return dedupe_assignments(assignments)


def collect_task_text_block(lines: list[str], start_index: int) -> str:
    block = [lines[start_index]]
    for line in lines[start_index + 1 : start_index + 8]:
        if is_xiaoya_task_start_line(line) and FULL_DATETIME_RE.search("\n".join(block)):
            break
        block.append(line)
        if line == "进入任务":
            break
    return "\n".join(block)


def is_xiaoya_task_start_line(line: str) -> bool:
    return bool(TASK_WORD_RE.search(line) and STATUS_RE.search(line) and FULL_DATETIME_RE.search(line))


def split_task_text_block(block: str) -> list[str]:
    parts: list[str] = []
    for line in block.splitlines():
        pieces = re.split(r"\t+|\s{2,}", line)
        if len(pieces) == 1:
            pieces = [line]
        parts.extend(piece.strip() for piece in pieces if piece.strip())
    return parts


def find_xiaoya_row_due(cells: list[str], *, headers: list[str] | None = None):
    if headers:
        for index, header in enumerate(headers):
            if index < len(cells) and re.search(r"截止|到期|结束|deadline|due", header, re.IGNORECASE):
                due_at = find_datetime(cells[index])
                if due_at is not None:
                    return due_at
    joined = "\n".join(cells)
    dates = [find_datetime(match.group(0)) for match in FULL_DATETIME_RE.finditer(joined)]
    dates = [value for value in dates if value is not None]
    if len(dates) >= 2:
        return dates[-1]
    if len(cells) > 8 and cells[8].strip():
        due_at = find_datetime(cells[8])
        if due_at is not None:
            return due_at
    if re.search(r"截止|到期|结束|deadline|due", joined, re.IGNORECASE):
        return find_datetime(joined)
    return None


def title_from_xiaoya_row(cells: list[str]) -> str:
    for cell in cells:
        title = clean_xiaoya_title(cell)
        if not title or looks_like_xiaoya_metadata(title):
            continue
        if find_datetime(title) is not None:
            continue
        return title
    return first_nonempty(cells)


def clean_xiaoya_title(value: str) -> str:
    value = compact_text(value)
    value = re.split(r"\s+[\\/]\s+|\s+(?:作业|测验|问卷|讨论|任务)\s+", value, maxsplit=1)[0]
    value = FULL_DATETIME_RE.split(value, maxsplit=1)[0]
    return value.strip(" -|/\\")


def looks_like_xiaoya_metadata(value: str) -> bool:
    return bool(
        value in {
            "\\",
            "/",
            "|",
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
            "作业",
            "测验",
            "问卷",
            "讨论",
            "自主观看",
            "课堂练习",
            "全体",
            "班级",
            "个人",
            "小组",
            "进入任务",
            "仅关注待完成任务",
        }
        or bool(STATUS_RE.search(value))
    )


def first_nonempty(cells: list[str]) -> str:
    for cell in cells:
        if cell.strip():
            return cell.strip()
    return ""


def guess_row_status(cells: list[str]) -> str:
    joined = " ".join(cells)
    if "未开始" in joined or "未开放" in joined or "未到开始时间" in joined:
        return "不可完成的作业"
    if "进行中" in joined or "未提交" in joined or "待完成" in joined or "未完成" in joined:
        return "未提交"
    if "已完成" in joined:
        return "已完成"
    if "已提交" in joined or "已批改" in joined or "已批阅" in joined:
        return "已提交"
    return "未知"


def dedupe_assignments(assignments: list[PlatformAssignment]) -> list[PlatformAssignment]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[PlatformAssignment] = []
    for item in assignments:
        if looks_like_course_summary_assignment(item):
            continue
        key = (item.title.casefold(), item.course.casefold(), item.due_at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def looks_like_course_summary_assignment(item: PlatformAssignment) -> bool:
    return (
        item.platform == "小雅"
        and compact_text(item.course) != ""
        and compact_text(item.title) == compact_text(item.course)
        and item.status in {"", "未知"}
    )
