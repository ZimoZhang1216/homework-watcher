from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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


@dataclass(frozen=True)
class CourseEntry:
    name: str
    page_number: int


class XiaoyaAdapter(PlaywrightPlatformAdapter):
    slug = "xiaoya"
    platform_name = "小雅"
    start_url = os.environ.get(
        "HW_XIAOYA_URL",
        "https://nankai.ai-augmented.com/app/jx-web/mycourse",
    )
    candidate_selectors = [
        "[class*='homework' i]",
        "[class*='assignment' i]",
        "[class*='task' i]",
        "[class*='work' i]",
        "[class*='activity' i]",
        "[class*='course' i]",
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

                course_entries = collect_course_entries(page)
                emit_progress(progress, f"{self.platform_name}：发现 {len(course_entries)} 门课程")
                assignments: list[PlatformAssignment] = []
                for index, course_entry in enumerate(course_entries):
                    course_name = course_entry.name
                    emit_progress(
                        progress,
                        f"{self.platform_name}：扫描课程 {index + 1}/{len(course_entries)} {course_name}",
                    )
                    page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    self.wait_until_ready(page)
                    go_to_course_page(page, course_entry.page_number)
                    card = page.locator(".aia_course_card").filter(has_text=course_name).first
                    if card.count() == 0:
                        continue
                    card.click(timeout=8_000)
                    self.wait_until_ready(page, network_timeout=10_000, settle_ms=1_000)
                    task_url = task_url_for(page.url)
                    page.goto(task_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    self.wait_until_ready(page, network_timeout=10_000, settle_ms=1_200)
                    course_assignments = parse_xiaoya_task_page(
                        page,
                        course=course_name,
                        platform=self.platform_name,
                    )
                    if course_assignments:
                        emit_progress(progress, f"{self.platform_name}：{course_name} 识别 {len(course_assignments)} 条任务")
                    assignments.extend(course_assignments)

                if assignments:
                    assignments = dedupe_assignments(assignments)
                    emit_progress(progress, f"{self.platform_name}：完成，识别 {len(assignments)} 条任务")
                    return assignments
                if course_entries:
                    emit_progress(progress, f"{self.platform_name}：完成，未发现待记录任务")
                    return []
                return self.parse_candidate_blocks(
                    [CandidateBlock(safe_body_text(page), page.url)],
                    fallback_url=page.url,
                )
            except (LoginRequiredError, PageStructureChangedError):
                raise
            except playwright_error as exc:
                raise PlaywrightUnavailableError(str(exc)) from exc
            finally:
                context.close()


def fetch_assignments(*, headless: bool = True, progress: ProgressCallback = None):
    """Fetch assignments from Xiaoya without submitting anything."""
    return XiaoyaAdapter().fetch_assignments(headless=headless, progress=progress)


def collect_visible_course_names(page) -> list[str]:
    return [entry.name for entry in collect_course_entries(page)]


def collect_course_entries(page) -> list[CourseEntry]:
    entries: list[CourseEntry] = []
    seen: set[str] = set()
    page_number = 1
    for _ in range(20):
        for name in collect_current_page_course_names(page):
            if name and name not in seen:
                seen.add(name)
                entries.append(CourseEntry(name=name, page_number=page_number))
        if not click_next_course_page(page):
            break
        page_number += 1
    return entries


def collect_current_page_course_names(page) -> list[str]:
    cards = page.locator(".aia_course_card")
    names: list[str] = []
    for index in range(min(cards.count(), 80)):
        text = compact_text(cards.nth(index).inner_text(timeout=1_000))
        name = extract_course_name(text)
        if name and name not in names:
            names.append(name)
    return names


def go_to_course_page(page, page_number: int) -> None:
    for _ in range(max(0, page_number - 1)):
        if not click_next_course_page(page):
            break


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
              const names = cards.map(card => card.getAttribute('data-xy-click-pt-name') || card.innerText)
                .map(text => text.replace(/\\s+/g, ' ').trim());
              return names.length > 0 && names.join('\\n') !== previous;
            }""",
            previous_signature,
            timeout=5_000,
        )
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(600)
    except Exception:
        pass


def extract_course_name(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    skip_prefixes = ("20", "校内", "教务", "学院：")
    for line in lines:
        if line.startswith(skip_prefixes):
            continue
        if re.search(r"次$|人$|教师|老师", line):
            continue
        return line
    compacted = " ".join(lines)
    match = re.search(r"教务开课\s+(.+?)\s+学院：", compacted)
    return match.group(1).strip() if match else ""


def task_url_for(url: str) -> str:
    parsed = urlsplit(url)
    match = re.search(r"(.*/mycourse/[^/]+)(?:/(?:resource|task)(?:/.*)?)?$", parsed.path)
    if match:
        return urlunsplit((parsed.scheme, parsed.netloc, match.group(1) + "/task", "", ""))
    return url.rstrip("/") + "/task"


def parse_xiaoya_task_page(page, *, course: str, platform: str) -> list[PlatformAssignment]:
    assignments = collect_visible_task_rows(page, course=course, platform=platform)
    for _ in range(20):
        if not click_next_task_page(page):
            break
        assignments.extend(collect_visible_task_rows(page, course=course, platform=platform))
    if assignments:
        return dedupe_assignments(assignments)

    text = safe_body_text(page)
    if "老师还没有发布任务" in text or "暂无" in text:
        return []
    blocks = [CandidateBlock(text, page.url)]
    return XiaoyaAdapter().parse_candidate_blocks(blocks, fallback_url=page.url, fallback_course=course)


def collect_visible_task_rows(page, *, course: str, platform: str) -> list[PlatformAssignment]:
    rows = page.locator("tbody tr")
    assignments: list[PlatformAssignment] = []
    for index in range(min(rows.count(), 200)):
        row = rows.nth(index)
        cells = row.locator("td")
        cell_texts: list[str] = []
        for cell_index in range(cells.count()):
            text = compact_text(cells.nth(cell_index).inner_text(timeout=1_000))
            cell_texts.append(text)
        item = parse_xiaoya_row(cell_texts, course=course, platform=platform, url=page.url)
        if item is not None:
            assignments.append(item)
    return assignments


def click_next_task_page(page) -> bool:
    next_button = page.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
    if next_button.count() == 0:
        return False
    before = visible_row_signature(page)
    try:
        next_button.click(timeout=3_000)
    except Exception:
        return False
    wait_for_task_page_update(page, before)
    return visible_row_signature(page) != before


def visible_row_signature(page) -> str:
    rows = page.locator("tbody tr")
    values: list[str] = []
    for index in range(min(rows.count(), 3)):
        try:
            values.append(compact_text(rows.nth(index).inner_text(timeout=500)))
        except Exception:
            continue
    return "\n".join(values)


def wait_for_task_page_update(page, previous_signature: str) -> None:
    try:
        page.wait_for_function(
            """previous => {
              const rows = Array.from(document.querySelectorAll('tbody tr')).slice(0, 3);
              const text = rows.map(row => row.innerText.replace(/\\s+/g, ' ').trim()).join('\\n');
              return text && text !== previous;
            }""",
            previous_signature,
            timeout=5_000,
        )
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(600)
    except Exception:
        pass


def parse_xiaoya_row(
    cells: list[str],
    *,
    course: str,
    platform: str,
    url: str,
) -> PlatformAssignment | None:
    if len(cells) < 4:
        return None
    joined = "\n".join(cells)
    due_source = cells[8] if len(cells) > 8 and cells[8].strip() else joined
    due_at = find_datetime(due_source)
    if due_at is None:
        return None
    title = first_nonempty(cells)
    if not title or title in {"标题", "老师还没有发布任务，敬请期待吧！"}:
        return None
    status = guess_row_status(cells)
    return PlatformAssignment(
        title=title,
        course=course,
        platform=platform,
        due_at=due_at,
        status=status,
        url=url,
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
    if "未提交" in joined or "待完成" in joined or "未完成" in joined:
        return "未提交"
    if "已提交" in joined or "已完成" in joined or "已批改" in joined:
        return "已提交"
    if len(cells) > 3 and cells[3].strip():
        return cells[3].strip()
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
