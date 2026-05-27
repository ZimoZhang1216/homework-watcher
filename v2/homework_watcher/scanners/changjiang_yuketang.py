from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.debug_dump import dump_debug_page
from homework_watcher.remote_login import profile_dir_for_user_platform
from homework_watcher.settings import Settings, load_settings
from playwright.sync_api import Page, sync_playwright


CHANGJIANG_PLATFORM_LABEL = "长江雨课堂"
CHANGJIANG_PLATFORM_KEY = "changjiang-yuketang"
DEFAULT_YUKETANG_URL = os.environ.get(
    "HW_CHANGJIANG_YUKETANG_URL",
    "https://changjiang.yuketang.cn/v2/web/index",
)
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


class ChangjiangYuketangScanner:
    platform_key = CHANGJIANG_PLATFORM_KEY

    def __init__(self, settings: Settings | None = None, *, headless: bool = True) -> None:
        self.settings = settings or load_settings()
        self.headless = headless

    @property
    def profile_dir(self) -> Path:
        return profile_dir_for_user_platform(self.settings, "default", CHANGJIANG_PLATFORM_KEY)

    def profile_dir_for_user(self, user_key: str) -> Path:
        return profile_dir_for_user_platform(self.settings, user_key, CHANGJIANG_PLATFORM_KEY)

    def scan(self, context) -> list[AssignmentCandidate]:
        summary = {
            "platform_label": CHANGJIANG_PLATFORM_LABEL,
            "status": "running",
            "message": "长江雨课堂：准备扫描",
            "discovered_courses_count": 0,
            "scanned_courses_count": 0,
            "failed_courses_count": 0,
            "parsed_assignments_count": 0,
        }
        context.metadata[CHANGJIANG_PLATFORM_KEY] = summary
        config = context.platform_config
        if config is not None and not config.enabled:
            context.emit(5, "长江雨课堂：未启用，跳过")
            summary.update(
                {
                    "status": "skipped",
                    "message": "长江雨课堂：未启用，跳过",
                }
            )
            return []

        profile_dir = self.profile_dir_for_user(context.user_key)
        profile_dir.mkdir(parents=True, exist_ok=True)
        start_url = config.base_url if config is not None and config.base_url else DEFAULT_YUKETANG_URL
        context.emit(10, "长江雨课堂：打开浏览器登录态")
        try:
            with sync_playwright() as playwright:
                browser_context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=self.headless,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1000},
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    prefer_student_entry(browser_context)
                    page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
                    assignments = self.scan_course_list(
                        page,
                        start_url=start_url,
                        scan_id=context.scan_id,
                        emit=context.emit,
                        summary=summary,
                    )
                finally:
                    browser_context.close()
            summary.update(
                {
                    "status": "succeeded",
                    "message": f"长江雨课堂：扫描完成，识别 {len(assignments)} 条作业",
                    "parsed_assignments_count": len(assignments),
                }
            )
            return assignments
        except Exception as exc:
            summary.update(
                {
                    "status": "failed",
                    "message": f"长江雨课堂：扫描失败 {type(exc).__name__}: {exc}",
                }
            )
            raise

    def scan_course_list(
        self,
        page: Page,
        *,
        start_url: str,
        scan_id: str,
        emit,
        summary: dict[str, object] | None = None,
    ) -> list[AssignmentCandidate]:
        page.goto(start_url, wait_until="domcontentloaded", timeout=20_000)
        wait_until_ready(page)
        ensure_student_tab(page)
        wait_until_ready(page, network_timeout=6_000, settle_ms=600)
        if looks_like_login_page(page):
            dump_debug_page(
                page,
                self.settings,
                scan_id=scan_id,
                stage="login-or-empty",
                course=CHANGJIANG_PLATFORM_LABEL,
                page_no=1,
            )
            raise RuntimeError("长江雨课堂登录态可能失效，请先在网页中打开长江雨课堂登录")

        course_cards = collect_course_cards(page)
        if summary is not None:
            summary["discovered_courses_count"] = len(course_cards)
        emit(25, f"长江雨课堂：发现 {len(course_cards)} 门课程")
        if not course_cards:
            text = safe_body_text(page)
            assignments = parse_yuketang_log_text(
                text,
                course=CHANGJIANG_PLATFORM_LABEL,
                platform=CHANGJIANG_PLATFORM_LABEL,
                url=page.url,
            )
            if not assignments and not looks_like_empty_state(text):
                dump_debug_page(
                    page,
                    self.settings,
                    scan_id=scan_id,
                    stage="empty-parse",
                    course=CHANGJIANG_PLATFORM_LABEL,
                    page_no=1,
                )
            emit(88, f"长江雨课堂：完成，识别 {len(assignments)} 条")
            if summary is not None:
                summary["parsed_assignments_count"] = len(assignments)
            return assignments

        assignments: list[AssignmentCandidate] = []
        scanned_courses_count = 0
        failed_courses_count = 0
        for index, course_name in enumerate(course_cards):
            percent = 25 + int((index + 1) / max(len(course_cards), 1) * 60)
            emit(percent, f"长江雨课堂：扫描课程 {index + 1}/{len(course_cards)} {course_name}")
            page.goto(start_url, wait_until="domcontentloaded", timeout=20_000)
            wait_until_ready(page)
            ensure_student_tab(page)
            wait_until_ready(page, network_timeout=6_000, settle_ms=600)
            cards = page.locator(".studentCol")
            try:
                if index >= cards.count():
                    failed_courses_count += 1
                    if summary is not None:
                        summary["failed_courses_count"] = failed_courses_count
                    continue
                cards.nth(index).click(timeout=8_000)
            except Exception:
                failed_courses_count += 1
                if summary is not None:
                    summary["failed_courses_count"] = failed_courses_count
                continue
            wait_until_ready(page, network_timeout=10_000, settle_ms=1_200)
            text = safe_body_text(page)
            page_assignments = parse_yuketang_log_text(
                text,
                course=course_name,
                platform=CHANGJIANG_PLATFORM_LABEL,
                url=page.url,
            )
            emit(percent, f"长江雨课堂：课程 {course_name} 识别 {len(page_assignments)} 条")
            assignments.extend(page_assignments)
            scanned_courses_count += 1
            if summary is not None:
                summary["scanned_courses_count"] = scanned_courses_count
                summary["parsed_assignments_count"] = len(assignments)

        assignments = dedupe_assignments(assignments)
        if summary is not None:
            summary["parsed_assignments_count"] = len(assignments)
        if not assignments:
            dump_debug_page(
                page,
                self.settings,
                scan_id=scan_id,
                stage="empty-parse",
                course=CHANGJIANG_PLATFORM_LABEL,
                page_no=1,
            )
        emit(88, f"长江雨课堂：完成，识别 {len(assignments)} 条")
        return assignments


def parse_yuketang_log_text(
    text: str,
    *,
    course: str,
    platform: str,
    url: str,
) -> list[AssignmentCandidate]:
    lines = [line.strip() for line in compact_text(text).splitlines() if line.strip()]
    assignments: list[AssignmentCandidate] = []
    for index, line in enumerate(lines):
        if "作业" not in line and "试卷" not in line:
            continue
        due_index = find_due_line_index(lines, index + 1)
        if due_index is None:
            continue
        due_at = find_yuketang_datetime(lines[due_index])
        if due_at is None:
            continue
        status_raw = normalize_yuketang_status(lines[due_index + 1 : due_index + 4])
        title = normalize_title(line)
        if not title:
            continue
        snapshot = "\n".join(lines[index : min(due_index + 4, len(lines))])
        assignments.append(
            AssignmentCandidate(
                platform=platform,
                course=course,
                title=title,
                status_raw=status_raw,
                due_at=due_at,
                url=urljoin(url, ""),
                source_key=f"changjiang-yuketang:{course}:{title}:{due_at.isoformat(timespec='seconds')}",
                raw_snapshot=sanitize_snapshot(snapshot),
            )
        )
    return dedupe_assignments(assignments)


def find_due_line_index(lines: list[str], start: int) -> int | None:
    for index in range(start, min(start + 5, len(lines))):
        if "截止" in lines[index] and find_yuketang_datetime(lines[index]) is not None:
            return index
    return None


def find_yuketang_datetime(text: str, *, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now().replace(microsecond=0)
    normalized = text.strip()
    relative = parse_relative_datetime(normalized, now)
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


def parse_relative_datetime(text: str, now: datetime) -> datetime | None:
    if "今天" not in text and "明天" not in text and "后天" not in text:
        return None
    offset = 0
    if "明天" in text:
        offset = 1
    elif "后天" in text:
        offset = 2
    date = (now + timedelta(days=offset)).date()
    time_match = re.search(r"(?P<h>\d{1,2})(?:[:：点]\s*(?P<minute>\d{1,2}))?", text)
    hour = int(time_match.group("h")) if time_match else 23
    minute = int(time_match.group("minute") or 0) if time_match else 59
    if ("下午" in text or "晚上" in text or "夜间" in text) and hour < 12:
        hour += 12
    if "上午" in text and hour == 12:
        hour = 0
    return datetime(date.year, date.month, date.day, hour, minute)


def normalize_yuketang_status(lines: list[str]) -> str:
    joined = " ".join(lines)
    if "未开始" in joined or "未开放" in joined or "未到开始时间" in joined:
        return "不可完成的作业"
    if "未作答" in joined or "未提交" in joined:
        return "未提交"
    if "得分" in joined:
        return "已完成"
    if "已作答" in joined or "已提交" in joined or "已完成" in joined:
        return "已提交"
    return "未完成"


def dedupe_assignments(assignments: list[AssignmentCandidate]) -> list[AssignmentCandidate]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[AssignmentCandidate] = []
    for item in assignments:
        key = (item.title.casefold(), item.course.casefold(), item.due_at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def prefer_student_entry(context) -> None:
    context.add_init_script(
        """
        (() => {
          try {
            if (!location.hostname.endsWith("yuketang.cn")) return;
            const raw = localStorage.getItem("vuex");
            const state = raw ? JSON.parse(raw) : {};
            state.isTeacherEntry = 2;
            localStorage.setItem("vuex", JSON.stringify(state));
          } catch (_) {
            try {
              localStorage.setItem("vuex", JSON.stringify({ isTeacherEntry: 2 }));
            } catch (_) {}
          }
        })();
        """
    )


def ensure_student_tab(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
              const raw = localStorage.getItem("vuex");
              const state = raw ? JSON.parse(raw) : {};
              state.isTeacherEntry = 2;
              localStorage.setItem("vuex", JSON.stringify(state));
              const tab = document.querySelector("#tab-student, [aria-controls='pane-student']");
              if (tab && tab.getAttribute("aria-selected") !== "true") tab.click();
            }
            """
        )
        page.wait_for_timeout(800)
    except Exception:
        pass


def collect_course_cards(page: Page) -> list[str]:
    cards = page.locator(".studentCol")
    courses: list[str] = []
    try:
        count = min(cards.count(), 80)
    except Exception:
        return courses
    for index in range(count):
        try:
            text = compact_text(cards.nth(index).inner_text(timeout=1_000))
        except Exception:
            continue
        course = first_course_line(text)
        if course:
            courses.append(course)
    return courses


def first_course_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"我教的课", "我听的课"}:
            continue
        return stripped
    return ""


def wait_until_ready(page: Page, *, network_timeout: int = 8_000, settle_ms: int = 800) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=network_timeout)
    except Exception:
        pass
    if settle_ms:
        page.wait_for_timeout(settle_ms)


def looks_like_login_page(page: Page) -> bool:
    try:
        if page.locator("input[type='password'], input[name*='password' i]").count() > 0:
            return True
        url = page.url.lower()
        body_text = page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return False
    login_url = any(marker in url for marker in ["login", "passport", "sso", "auth"])
    login_text = any(marker in body_text for marker in ["登录", "手机号", "密码", "验证码", "Sign in"])
    return login_url and login_text


def looks_like_empty_state(text: str) -> bool:
    return bool(
        re.search(
            r"暂无(?:作业|任务|待办)|没有(?:作业|任务|待办)|无(?:作业|任务|待办)|No\s+(?:homework|assignments|tasks)",
            text,
            re.IGNORECASE,
        )
    )


def safe_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""


def compact_text(text: str) -> str:
    lines = [" ".join(line.strip().split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def sanitize_snapshot(value: str) -> str:
    sanitized = re.sub(r"(?i)(cookie|authorization|token|password)\s*[:=]\s*\S+", r"\1=<redacted>", value)
    return sanitized[:1000]
