from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

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
STATUS_RE = re.compile(r"进行中|未提交|待完成|未完成|已完成|已提交|已批改|未开始|未开放|未到开始时间")
COURSE_PATH_RE = re.compile(r"(/app/jx-web/mycourse/[^\"'<\s]+|/mycourse/[^\"'<\s]+)")


@dataclass(frozen=True)
class CourseEntry:
    name: str
    page_number: int
    task_url: str = ""


class XiaoyaAdapter(PlaywrightPlatformAdapter):
    slug = "xiaoya"
    platform_name = "小雅"
    start_url = os.environ.get(
        "HW_XIAOYA_URL",
        "https://nankai.ai-augmented.com/app/jx-web/mycourse",
    )
    scan_timeout_seconds = int(os.environ.get("HW_XIAOYA_SCAN_TIMEOUT_SECONDS", "600"))
    course_timeout_seconds = int(os.environ.get("HW_XIAOYA_COURSE_TIMEOUT_SECONDS", "60"))
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
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=headless)
            deadline = time.monotonic() + self.scan_timeout_seconds
            try:
                page = context.pages[0] if context.pages else context.new_page()
                open_xiaoya_page(page, self.url, deadline=deadline, progress=progress, label="打开课程列表")
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
                        items = scan_course_tasks(
                            page,
                            course,
                            start_url=self.url,
                            platform=self.platform_name,
                            deadline=course_deadline,
                            max_pages=self.max_task_pages,
                            progress=progress,
                        )
                    except PlaywrightUnavailableError as exc:
                        if time.monotonic() >= deadline:
                            raise
                        skipped.append(f"{course.name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course.name}，原因：{truncate(str(exc), 42)}")
                        continue
                    except Exception as exc:
                        skipped.append(f"{course.name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course.name}，原因：{truncate(str(exc), 42)}")
                        continue
                    if items:
                        emit_progress(progress, f"{self.platform_name}：{course.name} 识别 {len(items)} 条任务")
                    assignments.extend(items)

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
        for entry in collect_current_page_course_entries(page, page_number=page_number):
            key = entry.name.casefold()
            if not entry.name or key in seen:
                continue
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
                html=str(raw.get("html") or ""),
                base_url=page.url,
            )
        else:
            continue
        if name:
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
                return {
                  name: card.getAttribute('data-xy-click-pt-name') || '',
                  text: compact(card.innerText || card.textContent || ''),
                  url,
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
) -> list[PlatformAssignment]:
    if course.task_url:
        target_url = course.task_url
        emit_progress(progress, f"{platform}：进入任务页 {course.name}")
        page.goto(target_url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, 15_000))
    else:
        open_xiaoya_page(page, start_url, deadline=deadline, progress=progress, label="回到课程列表")
        go_to_course_page(page, course.page_number, deadline=deadline)
        card = page.locator(".aia_course_card").filter(has_text=course.name).first
        if card.count() == 0:
            raise PlaywrightUnavailableError("未找到课程卡片")
        card.click(timeout=remaining_timeout_ms(deadline, 6_000))
        wait_for_xiaoya_shell(page, timeout_ms=6_000, settle_ms=600)
        target_url = task_url_for(page.url)
        page.goto(target_url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, 15_000))
    wait_for_xiaoya_shell(page, timeout_ms=8_000, settle_ms=700)
    recover_xiaoya_loading_page(page, deadline=deadline, progress=progress)
    return parse_xiaoya_task_page(
        page,
        course=course.name,
        platform=platform,
        deadline=deadline,
        max_pages=max_pages,
    )


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
) -> list[PlatformAssignment]:
    assignments: list[PlatformAssignment] = []
    for page_index in range(max_pages):
        if deadline is not None:
            ensure_scan_time_left(deadline, "读取任务列表")
        assignments.extend(collect_visible_task_rows(page, course=course, platform=platform))
        assignments.extend(parse_xiaoya_task_text(safe_body_text(page), course=course, platform=platform, url=page.url))
        if not click_next_task_page(page, deadline=deadline):
            break
        wait_for_xiaoya_shell(page, timeout_ms=4_000, settle_ms=400)
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
        block = "\n".join(lines[index : index + 4])
        if not TASK_WORD_RE.search(block) or not STATUS_RE.search(block):
            continue
        if len(FULL_DATETIME_RE.findall(block)) == 0:
            continue
        cells = split_task_text_block(block)
        item = parse_xiaoya_row(cells, course=course, platform=platform, url=url)
        if item is not None:
            assignments.append(item)
    return dedupe_assignments(assignments)


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
    if "已提交" in joined or "已完成" in joined or "已批改" in joined:
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
