from __future__ import annotations

import os
from urllib.parse import urljoin

from ..datetime_utils import find_datetime
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


class ChangjiangYuketangAdapter(PlaywrightPlatformAdapter):
    slug = "changjiang-yuketang"
    platform_name = "长江雨课堂"
    start_url = os.environ.get(
        "HW_CHANGJIANG_YUKETANG_URL",
        "https://changjiang.yuketang.cn/v2/web/index",
    )
    candidate_selectors = [
        "[class*='homework' i]",
        "[class*='assignment' i]",
        "[class*='task' i]",
        "[class*='exercise' i]",
        "[class*='course' i] [class*='work' i]",
        "[class*='work' i]",
        *DEFAULT_CANDIDATE_SELECTORS,
    ]

    def fetch_assignments(
        self,
        *,
        headless: bool = True,
        progress: ProgressCallback = None,
    ) -> list[PlatformAssignment]:
        sync_playwright, playwright_error = load_playwright()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=headless)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                emit_progress(progress, f"{self.platform_name}：打开课程列表")
                page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                self.wait_until_ready(page)
                if self.is_login_required(page):
                    raise LoginRequiredError(
                        f"{self.platform_name} 登录状态已失效。请运行：hw login {self.slug}"
                    )

                course_cards = collect_course_cards(page)
                emit_progress(progress, f"{self.platform_name}：发现 {len(course_cards)} 门课程")
                if not course_cards:
                    return self.parse_candidate_blocks(
                        [CandidateBlock(safe_body_text(page), page.url)],
                        fallback_url=page.url,
                    )

                assignments: list[PlatformAssignment] = []
                for index, course_name in enumerate(course_cards):
                    emit_progress(
                        progress,
                        f"{self.platform_name}：扫描课程 {index + 1}/{len(course_cards)} {course_name}",
                    )
                    page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    self.wait_until_ready(page)
                    cards = page.locator(".studentCol")
                    if index >= cards.count():
                        continue
                    cards.nth(index).click(timeout=8_000)
                    self.wait_until_ready(page, network_timeout=10_000, settle_ms=1_200)
                    text = safe_body_text(page)
                    assignments.extend(
                        parse_yuketang_log_text(
                            text,
                            course=course_name,
                            platform=self.platform_name,
                            url=page.url,
                        )
                    )
                assignments = dedupe_assignments(assignments)
                emit_progress(progress, f"{self.platform_name}：完成，识别 {len(assignments)} 条任务")
                return assignments
            except (LoginRequiredError, PageStructureChangedError):
                raise
            except playwright_error as exc:
                raise PlaywrightUnavailableError(str(exc)) from exc
            finally:
                context.close()


def fetch_assignments(*, headless: bool = True, progress: ProgressCallback = None):
    """Fetch assignments from Changjiang Yuketang without submitting anything."""
    return ChangjiangYuketangAdapter().fetch_assignments(headless=headless, progress=progress)


def collect_course_cards(page) -> list[str]:
    cards = page.locator(".studentCol")
    courses: list[str] = []
    for index in range(min(cards.count(), 80)):
        text = compact_text(cards.nth(index).inner_text(timeout=1_000))
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


def parse_yuketang_log_text(
    text: str,
    *,
    course: str,
    platform: str,
    url: str,
) -> list[PlatformAssignment]:
    lines = [line.strip() for line in compact_text(text).splitlines() if line.strip()]
    assignments: list[PlatformAssignment] = []
    for index, line in enumerate(lines):
        if "作业" not in line and "试卷" not in line:
            continue
        due_index = find_due_line_index(lines, index + 1)
        if due_index is None:
            continue
        due_at = find_datetime(lines[due_index])
        if due_at is None:
            continue
        status = normalize_yuketang_status(lines[due_index + 1 : due_index + 4])
        assignments.append(
            PlatformAssignment(
                title=line,
                course=course,
                platform=platform,
                due_at=due_at,
                status=status,
                url=urljoin(url, ""),
            )
        )
    return dedupe_assignments(assignments)


def find_due_line_index(lines: list[str], start: int) -> int | None:
    for index in range(start, min(start + 5, len(lines))):
        if "截止" in lines[index] and find_datetime(lines[index]) is not None:
            return index
    return None


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
    return "未知"


def dedupe_assignments(assignments: list[PlatformAssignment]) -> list[PlatformAssignment]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[PlatformAssignment] = []
    for item in assignments:
        key = (item.title.casefold(), item.course.casefold(), item.due_at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
