from __future__ import annotations

import os
import re
import time
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


FULL_DATETIME_RE = re.compile(
    r"20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*(?:日)?\s+"
    r"\d{1,2}\s*[:：]\s*\d{1,2}(?:\s*[:：]\s*\d{1,2})?"
)
LOADING_TEXT_RE = re.compile(r"正在加载应用|加载应用|请稍候|loading", re.IGNORECASE)


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
    scan_timeout_seconds = int(os.environ.get("HW_XIAOYA_SCAN_TIMEOUT_SECONDS", "600"))
    course_timeout_seconds = int(os.environ.get("HW_XIAOYA_COURSE_TIMEOUT_SECONDS", "45"))
    max_course_pages = int(os.environ.get("HW_XIAOYA_MAX_COURSE_PAGES", "30"))
    max_courses = int(os.environ.get("HW_XIAOYA_MAX_COURSES", "80"))
    max_task_pages = int(os.environ.get("HW_XIAOYA_MAX_TASK_PAGES", "5"))
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
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=headless)
            deadline = time.monotonic() + self.scan_timeout_seconds
            try:
                page = context.pages[0] if context.pages else context.new_page()
                emit_progress(progress, f"{self.platform_name}：打开课程列表")
                page.goto(self.url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, self.timeout_ms))
                self.wait_until_ready(page, network_timeout=4_000, settle_ms=500)
                recover_xiaoya_loading_page(page, deadline=deadline, progress=progress)
                ensure_student_course_tab(page)
                self.wait_until_ready(page, network_timeout=4_000, settle_ms=500)
                recover_xiaoya_loading_page(page, deadline=deadline, progress=progress)
                if self.is_login_required(page):
                    raise LoginRequiredError(
                        f"{self.platform_name} 登录状态已失效。请运行：hw login {self.slug}"
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
                assignments: list[PlatformAssignment] = []
                skipped_courses: list[str] = []
                for index, course_entry in enumerate(course_entries):
                    ensure_scan_time_left(deadline, f"扫描课程 {index + 1}/{len(course_entries)}")
                    course_deadline = min(deadline, time.monotonic() + self.course_timeout_seconds)
                    course_name = course_entry.name
                    emit_progress(
                        progress,
                        f"{self.platform_name}：扫描课程 {index + 1}/{len(course_entries)} {course_name}",
                    )
                    try:
                        page.goto(self.url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(course_deadline, self.timeout_ms))
                        self.wait_until_ready(page, network_timeout=3_000, settle_ms=400)
                        recover_xiaoya_loading_page(page, deadline=course_deadline, progress=progress)
                        ensure_student_course_tab(page)
                        self.wait_until_ready(page, network_timeout=3_000, settle_ms=400)
                        recover_xiaoya_loading_page(page, deadline=course_deadline, progress=progress)
                        go_to_course_page(page, course_entry.page_number, deadline=course_deadline)
                        card = page.locator(".aia_course_card").filter(has_text=course_name).first
                        if card.count() == 0:
                            skipped_courses.append(f"{course_name}: 未找到课程卡片")
                            continue
                        card.click(timeout=remaining_timeout_ms(course_deadline, 5_000))
                        self.wait_until_ready(page, network_timeout=4_000, settle_ms=500)
                        task_url = task_url_for(page.url)
                        page.goto(task_url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(course_deadline, self.timeout_ms))
                        self.wait_until_ready(page, network_timeout=4_000, settle_ms=700)
                        recover_xiaoya_loading_page(page, deadline=course_deadline, progress=progress)
                        course_assignments = parse_xiaoya_task_page(
                            page,
                            course=course_name,
                            platform=self.platform_name,
                            deadline=course_deadline,
                            max_pages=self.max_task_pages,
                        )
                    except PlaywrightUnavailableError as exc:
                        if time.monotonic() >= deadline:
                            raise
                        skipped_courses.append(f"{course_name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course_name}，原因：{truncate(str(exc), 42)}")
                        continue
                    except Exception as exc:
                        skipped_courses.append(f"{course_name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course_name}，原因：{truncate(str(exc), 42)}")
                        continue
                    if course_assignments:
                        emit_progress(progress, f"{self.platform_name}：{course_name} 识别 {len(course_assignments)} 条任务")
                    assignments.extend(course_assignments)

                if assignments:
                    assignments = dedupe_assignments(assignments)
                    if skipped_courses:
                        emit_progress(progress, f"{self.platform_name}：完成，识别 {len(assignments)} 条任务，跳过 {len(skipped_courses)} 门课程")
                    else:
                        emit_progress(progress, f"{self.platform_name}：完成，识别 {len(assignments)} 条任务")
                    return assignments
                if course_entries:
                    if skipped_courses:
                        emit_progress(progress, f"{self.platform_name}：完成，跳过 {len(skipped_courses)} 门课程，未发现待记录任务")
                    else:
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
        wait_for_xiaoya_app_shell(page, timeout_ms=5_000, settle_ms=500)
        if not xiaoya_page_is_loading(page):
            emit_progress(progress, "小雅：加载状态已恢复")
            return
        if attempt == 0:
            emit_progress(progress, "小雅：首次重载仍未恢复，再试一次")
    raise PlaywrightUnavailableError(
        "小雅页面卡在“正在加载应用”。已自动清理缓存并重载，仍未恢复。"
        "请关闭远程浏览器后重新打开小雅登录；如果仍然如此，可能是小雅静态资源在服务器网络下暂时不可用。"
    )


def xiaoya_page_is_loading(page) -> bool:
    try:
        if page.locator(".aia_course_card").count() > 0:
            return False
    except Exception:
        pass
    return xiaoya_text_is_loading(safe_body_text(page))


def xiaoya_text_is_loading(text: str) -> bool:
    compacted = compact_text(text)
    return bool(LOADING_TEXT_RE.search(compacted))


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


def wait_for_xiaoya_app_shell(page, *, timeout_ms: int = 6_000, settle_ms: int = 500) -> None:
    try:
        page.wait_for_function(
            """() => {
              const body = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim();
              const hasCourseCards = document.querySelectorAll('.aia_course_card').length > 0;
              const hasLogin = document.querySelectorAll('input[type="password"], input[name*="password" i]').length > 0;
              const stillLoading = /正在加载应用|加载应用|请稍候|loading/i.test(body);
              return hasCourseCards || hasLogin || !stillLoading;
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:
        pass


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
        page.wait_for_timeout(800)
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
    max_courses: int = 80,
) -> list[CourseEntry]:
    entries: list[CourseEntry] = []
    seen: set[str] = set()
    page_number = 1
    for _ in range(max_pages):
        if deadline is not None:
            ensure_scan_time_left(deadline, "读取课程列表")
        for name in collect_current_page_course_names(page):
            if name and name not in seen:
                seen.add(name)
                entries.append(CourseEntry(name=name, page_number=page_number))
                if len(entries) >= max_courses:
                    emit_progress(progress, f"{platform_name}：课程数量达到上限 {max_courses}，停止继续翻页")
                    return entries
        emit_progress(progress, f"{platform_name}：读取课程列表第 {page_number} 页，累计 {len(entries)} 门")
        if not click_next_course_page(page):
            break
        page_number += 1
    return entries


def collect_current_page_course_names(page) -> list[str]:
    try:
        raw_names = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('.aia_course_card'))
              .slice(0, 100)
              .map(card => card.getAttribute('data-xy-click-pt-name') || card.innerText || '')
            """
        )
    except Exception:
        raw_names = []
    names: list[str] = []
    for text in raw_names:
        name = extract_course_name(str(text))
        if name and name not in names:
            names.append(name)
    if names:
        return names

    cards = page.locator(".aia_course_card")
    names: list[str] = []
    for index in range(min(cards.count(), 80)):
        try:
            text = compact_text(cards.nth(index).inner_text(timeout=500))
        except Exception:
            continue
        name = extract_course_name(text)
        if name and name not in names:
            names.append(name)
    return names


def go_to_course_page(page, page_number: int, *, deadline: float | None = None) -> None:
    for _ in range(max(0, page_number - 1)):
        if deadline is not None:
            ensure_scan_time_left(deadline, f"跳转课程列表第 {page_number} 页")
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
            timeout=2_000,
        )
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=1_500)
    except Exception:
        pass
    try:
        page.wait_for_timeout(300)
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


def parse_xiaoya_task_page(
    page,
    *,
    course: str,
    platform: str,
    deadline: float | None = None,
    max_pages: int = 5,
) -> list[PlatformAssignment]:
    prefer_pending_task_filter(page)
    assignments = collect_visible_task_rows(page, course=course, platform=platform)
    for _ in range(max(0, max_pages - 1)):
        if deadline is not None:
            ensure_scan_time_left(deadline, "读取任务列表")
        if not click_next_task_page(page, deadline=deadline):
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
    assignments: list[PlatformAssignment] = []
    for cell_texts in evaluate_task_table_rows(page):
        item = parse_xiaoya_row(cell_texts, course=course, platform=platform, url=page.url)
        if item is not None:
            assignments.append(item)
    if assignments:
        return dedupe_assignments(assignments)

    rows = page.locator("tbody tr")
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


def evaluate_task_table_rows(page) -> list[list[str]]:
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
                .filter(row => row.length >= 4);
            }
            """
        )
    except Exception:
        return []
    rows: list[list[str]] = []
    for raw in raw_rows:
        if not isinstance(raw, list):
            continue
        cells = [compact_text(str(cell)) for cell in raw if compact_text(str(cell))]
        cells = collapse_duplicate_cells(cells)
        if cells:
            rows.append(cells)
    return rows


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


def prefer_pending_task_filter(page) -> None:
    try:
        before = visible_row_signature(page)
        changed = page.evaluate(
            """
            () => {
              const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
              const labels = Array.from(document.querySelectorAll('label, .ant-checkbox-wrapper'));
              const label = labels.find(node => compact(node.innerText || node.textContent).includes('仅关注待完成任务'));
              if (!label) return false;
              const input = label.querySelector('input[type="checkbox"]');
              if (input && input.checked) return false;
              const target = input || label.querySelector('.ant-checkbox, span') || label;
              target.click();
              return true;
            }
            """
        )
        if changed:
            wait_for_task_filter_update(page, before)
    except Exception:
        pass


def wait_for_task_filter_update(page, previous_signature: str) -> None:
    try:
        page.wait_for_function(
            """previous => {
              const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
              const rows = Array.from(document.querySelectorAll('tbody tr')).slice(0, 3);
              const text = rows.map(row => compact(row.innerText || row.textContent)).join('\\n');
              const body = compact(document.body ? document.body.innerText : '');
              return text !== previous || /暂无|没有|空/.test(body);
            }""",
            previous_signature,
            timeout=2_500,
        )
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=1_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(300)
    except Exception:
        pass


def click_next_task_page(page, *, deadline: float | None = None) -> bool:
    if deadline is not None:
        ensure_scan_time_left(deadline, "任务列表翻页")
    next_button = page.locator(".ant-pagination-next:not(.ant-pagination-disabled)").first
    if next_button.count() == 0:
        return False
    before = visible_row_signature(page)
    try:
        timeout = 3_000 if deadline is None else remaining_timeout_ms(deadline, 3_000)
        next_button.click(timeout=timeout)
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
            timeout=1_500,
        )
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=1_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(300)
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
    due_at = find_xiaoya_row_due(cells)
    if due_at is None:
        return None
    title = title_from_xiaoya_row(cells)
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


def find_xiaoya_row_due(cells: list[str]):
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
    return dates[-1] if dates else None


def title_from_xiaoya_row(cells: list[str]) -> str:
    for cell in cells:
        value = compact_text(cell)
        if not value or looks_like_xiaoya_metadata(value):
            continue
        if find_datetime(value) is not None:
            continue
        return value
    return first_nonempty(cells)


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
        }
        or re.search(r"已完成|已提交|已批改|进行中|未完成|未提交|待完成|未开始|未开放", value)
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
    if "未提交" in joined or "待完成" in joined or "未完成" in joined or "进行中" in joined:
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
