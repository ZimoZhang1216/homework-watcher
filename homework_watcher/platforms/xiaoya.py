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


@dataclass(frozen=True)
class CourseEntry:
    name: str
    page_number: int


@dataclass(frozen=True)
class TaskRowCandidate:
    headers: list[str]
    cells: list[str]
    text: str
    url: str


class XiaoyaAdapter(PlaywrightPlatformAdapter):
    slug = "xiaoya"
    platform_name = "小雅"
    start_url = os.environ.get(
        "HW_XIAOYA_URL",
        "https://nankai.ai-augmented.com/app/jx-web/mycourse",
    )
    scan_timeout_seconds = int(os.environ.get("HW_XIAOYA_SCAN_TIMEOUT_SECONDS", "600"))
    max_course_pages = int(os.environ.get("HW_XIAOYA_MAX_COURSE_PAGES", "30"))
    max_courses = int(os.environ.get("HW_XIAOYA_MAX_COURSES", "80"))
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
                self.wait_until_ready(page)
                ensure_student_course_tab(page)
                self.wait_until_ready(page, network_timeout=6_000, settle_ms=600)
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
                course_errors: list[str] = []
                for index, course_entry in enumerate(course_entries):
                    ensure_scan_time_left(deadline, f"扫描课程 {index + 1}/{len(course_entries)}")
                    course_name = course_entry.name
                    emit_progress(
                        progress,
                        f"{self.platform_name}：扫描课程 {index + 1}/{len(course_entries)} {course_name}",
                    )
                    try:
                        page.goto(self.url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, self.timeout_ms))
                        self.wait_until_ready(page)
                        ensure_student_course_tab(page)
                        self.wait_until_ready(page, network_timeout=6_000, settle_ms=600)
                        go_to_course_page(page, course_entry.page_number, deadline=deadline)
                        card = page.locator(".aia_course_card").filter(has_text=course_name).first
                        if card.count() == 0:
                            course_errors.append(f"{course_name}: 未找到课程卡片")
                            continue
                        card.click(timeout=remaining_timeout_ms(deadline, 8_000))
                        self.wait_until_ready(page, network_timeout=10_000, settle_ms=1_000)
                        task_url = task_url_for(page.url)
                        page.goto(task_url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, self.timeout_ms))
                        self.wait_until_ready(page, network_timeout=10_000, settle_ms=1_200)
                        course_assignments = parse_xiaoya_task_page(
                            page,
                            course=course_name,
                            platform=self.platform_name,
                            deadline=deadline,
                        )
                    except PlaywrightUnavailableError:
                        raise
                    except Exception as exc:
                        course_errors.append(f"{course_name}: {truncate(str(exc), 80)}")
                        emit_progress(progress, f"{self.platform_name}：跳过课程 {course_name}，原因：{truncate(str(exc), 42)}")
                        continue
                    if course_assignments:
                        emit_progress(progress, f"{self.platform_name}：{course_name} 识别 {len(course_assignments)} 条任务")
                    assignments.extend(course_assignments)

                if assignments:
                    assignments = dedupe_assignments(assignments)
                    emit_progress(progress, f"{self.platform_name}：完成，识别 {len(assignments)} 条任务")
                    return assignments
                if course_entries:
                    if course_errors:
                        emit_progress(progress, f"{self.platform_name}：完成，跳过 {len(course_errors)} 门课程，未发现待记录任务")
                        return []
                    emit_progress(progress, f"{self.platform_name}：完成，未发现待记录任务")
                    return []
                return self.parse_candidate_blocks(
                    [CandidateBlock(safe_body_text(page), page.url)],
                    fallback_url=page.url,
                )
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
    return max(1_000, min(default_ms, remaining))


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
        name = extract_course_name(compact_text(str(text)))
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


def parse_xiaoya_task_page(
    page,
    *,
    course: str,
    platform: str,
    deadline: float | None = None,
    max_pages: int = 20,
) -> list[PlatformAssignment]:
    assignments = collect_visible_task_items(page, course=course, platform=platform)
    for _ in range(max_pages):
        if deadline is not None:
            ensure_scan_time_left(deadline, "读取任务列表")
        if not click_next_task_page(page, deadline=deadline):
            break
        assignments.extend(collect_visible_task_items(page, course=course, platform=platform))
    if assignments:
        return dedupe_assignments(assignments)

    text = safe_body_text(page)
    if "老师还没有发布任务" in text or "暂无" in text:
        return []
    blocks = [CandidateBlock(text, page.url)]
    return XiaoyaAdapter().parse_candidate_blocks(blocks, fallback_url=page.url, fallback_course=course)


def collect_visible_task_items(page, *, course: str, platform: str) -> list[PlatformAssignment]:
    rows, unresolved = collect_visible_task_rows(page, course=course, platform=platform)
    detail_items = collect_task_detail_items(page, unresolved, course=course, platform=platform)
    return dedupe_assignments([*rows, *detail_items, *collect_visible_task_blocks(page, course=course, platform=platform)])


def collect_visible_task_rows(
    page,
    *,
    course: str,
    platform: str,
) -> tuple[list[PlatformAssignment], list[TaskRowCandidate]]:
    assignments: list[PlatformAssignment] = []
    unresolved: list[TaskRowCandidate] = []
    for candidate in collect_task_row_candidates(page):
        item = parse_xiaoya_row_candidate(candidate, course=course, platform=platform, fallback_url=page.url)
        if item is None and len(candidate.cells) == 1:
            item = parse_xiaoya_task_block(candidate.cells[0], course=course, platform=platform, url=candidate.url or page.url)
        if item is not None:
            assignments.append(item)
            continue
        if should_open_task_detail(candidate):
            unresolved.append(candidate)
    return assignments, unresolved


def collect_task_row_candidates(page) -> list[TaskRowCandidate]:
    rows: list[TaskRowCandidate] = []
    seen: set[tuple[str, str]] = set()
    for scroll_left in (0, 1_000_000):
        scroll_task_tables(page, scroll_left)
        for raw in evaluate_task_rows(page):
            candidate = TaskRowCandidate(
                headers=[compact_text(str(value)) for value in raw.get("headers", []) if compact_text(str(value))],
                cells=[compact_text(str(value)) for value in raw.get("cells", []) if compact_text(str(value))],
                text=compact_text(str(raw.get("text", ""))),
                url=str(raw.get("url", "") or ""),
            )
            if not candidate.cells and not candidate.text:
                continue
            key = ("\n".join(candidate.cells) or candidate.text, candidate.url)
            if key in seen:
                continue
            seen.add(key)
            rows.append(candidate)
    return rows


def scroll_task_tables(page, scroll_left: int) -> None:
    try:
        page.evaluate(
            """
            scrollLeft => {
              for (const node of document.querySelectorAll('.ant-table-body, .ant-table-content')) {
                node.scrollLeft = scrollLeft;
              }
            }
            """,
            scroll_left,
        )
        page.wait_for_timeout(250)
    except Exception:
        pass


def evaluate_task_rows(page) -> list[dict]:
    try:
        return page.evaluate(
            """
            () => {
              const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
              const visibleEnough = node => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const tables = Array.from(document.querySelectorAll('.ant-table, table'));
              const result = [];
              for (const table of tables) {
                const root = table.closest('.ant-table') || table;
                const headers = Array.from(root.querySelectorAll('thead th'))
                  .map(node => compact(node.innerText || node.textContent))
                  .filter(Boolean);
                const rows = Array.from(root.querySelectorAll('tbody tr'))
                  .filter(row => !row.classList.contains('ant-table-measure-row'));
                for (const row of rows) {
                  const cells = Array.from(row.querySelectorAll('td'))
                    .filter(cell => visibleEnough(cell) || compact(cell.textContent))
                    .map(cell => compact(cell.innerText || cell.textContent))
                    .filter(Boolean);
                  const text = compact(row.innerText || row.textContent);
                  const link = row.querySelector('a[href]');
                  const href = link ? link.getAttribute('href') : '';
                  if (cells.length || text) result.push({ headers, cells, text, url: href || '' });
                }
              }
              return result;
            }
            """
        )
    except Exception:
        return []


def parse_xiaoya_row_candidate(
    candidate: TaskRowCandidate,
    *,
    course: str,
    platform: str,
    fallback_url: str,
) -> PlatformAssignment | None:
    item = parse_xiaoya_row(
        candidate.cells,
        course=course,
        platform=platform,
        url=urljoin(fallback_url, candidate.url) if candidate.url else fallback_url,
        headers=candidate.headers,
    )
    if item is not None:
        return item
    if re.search(r"截止|到期|结束|deadline|due", candidate.text, re.IGNORECASE):
        return parse_xiaoya_task_block(candidate.text, course=course, platform=platform, url=fallback_url)
    return None


def should_open_task_detail(candidate: TaskRowCandidate) -> bool:
    text = "\n".join([*candidate.cells, candidate.text])
    if not candidate.url or candidate.url.startswith(("javascript:", "#")):
        return False
    if not re.search(r"作业|任务|实习|练习|测验|问卷", text):
        return False
    if re.search(r"已完成|已提交|已批改", text) and not re.search(r"进行中|未完成|未提交|待完成", text):
        return False
    return True


def collect_task_detail_items(
    page,
    candidates: list[TaskRowCandidate],
    *,
    course: str,
    platform: str,
) -> list[PlatformAssignment]:
    assignments: list[PlatformAssignment] = []
    seen_urls: set[str] = set()
    for candidate in candidates[:40]:
        target_url = urljoin(page.url, candidate.url)
        if target_url in seen_urls:
            continue
        seen_urls.add(target_url)
        detail_page = page.context.new_page()
        try:
            detail_page.goto(target_url, wait_until="domcontentloaded", timeout=12_000)
            wait_for_detail_page(detail_page)
            text = safe_body_text(detail_page)
            item = parse_xiaoya_task_block(text, course=course, platform=platform, url=detail_page.url)
            if item is None:
                due_at = find_xiaoya_due_datetime(text)
                title = first_task_title_from_candidate(candidate)
                if due_at is not None and title:
                    item = PlatformAssignment(
                        title=title,
                        course=course,
                        platform=platform,
                        due_at=due_at,
                        status=guess_row_status([*candidate.cells, text]),
                        url=detail_page.url,
                    )
            if item is not None:
                assignments.append(item)
        except Exception:
            continue
        finally:
            try:
                detail_page.close()
            except Exception:
                pass
    return assignments


def wait_for_detail_page(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=6_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(600)
    except Exception:
        pass


def first_task_title_from_candidate(candidate: TaskRowCandidate) -> str:
    headers = candidate.headers
    cells = candidate.cells
    for index, header in enumerate(headers):
        if index < len(cells) and re.search(r"标题|名称|任务|作业", header):
            title = clean_xiaoya_task_title(cells[index], fallback_course="")
            if title:
                return title
    for cell in cells:
        title = clean_xiaoya_task_title(cell, fallback_course="")
        if title and find_datetime(title) is None and not looks_like_metadata_cell(title):
            return title
    return ""


def collect_visible_task_blocks(page, *, course: str, platform: str) -> list[PlatformAssignment]:
    try:
        texts = page.evaluate(
            """
            () => {
              const selectors = [
                '.ant-card',
                '.ant-list-item',
                '[class*="task"]',
                '[class*="homework"]',
                '[class*="work"]',
                '[class*="activity"]'
              ];
              const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
              const visible = node => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              return nodes
                .filter(visible)
                .map(node => (node.innerText || node.textContent || '').trim())
                .filter(text => text.length >= 8 && text.length <= 1200)
                .filter(text => /任务单|作业|任务|提交/.test(text))
                .filter(text => /截止|到期|结束|Due|deadline/i.test(text));
            }
            """
        )
    except Exception:
        texts = []
    assignments: list[PlatformAssignment] = []
    seen_texts: set[str] = set()
    for raw_text in texts:
        text = compact_text(str(raw_text))
        if text in seen_texts:
            continue
        seen_texts.add(text)
        item = parse_xiaoya_task_block(text, course=course, platform=platform, url=page.url)
        if item is not None:
            assignments.append(item)
    return assignments


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
    headers: list[str] | None = None,
) -> PlatformAssignment | None:
    if len(cells) < 4:
        return None
    joined = "\n".join(cells)
    due_at = find_due_at_from_row(cells, headers or [])
    if due_at is None:
        return None
    title = title_from_row(cells, headers or [])
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


def find_due_at_from_row(cells: list[str], headers: list[str]):
    for index, header in enumerate(headers):
        if index >= len(cells):
            continue
        if re.search(r"截止|到期|结束|deadline|due", header, re.IGNORECASE):
            due_at = find_datetime(cells[index])
            if due_at is not None:
                return due_at
    for cell in cells:
        if re.search(r"截止|到期|结束|deadline|due", cell, re.IGNORECASE):
            due_at = find_xiaoya_due_datetime(cell)
            if due_at is not None:
                return due_at
    if len(cells) > 8 and cells[8].strip():
        return find_datetime(cells[8])
    return None


def title_from_row(cells: list[str], headers: list[str]) -> str:
    for index, header in enumerate(headers):
        if index < len(cells) and re.search(r"标题|名称|任务|作业", header):
            title = clean_xiaoya_task_title(cells[index], fallback_course="")
            if title:
                return title
    for cell in cells:
        title = clean_xiaoya_task_title(cell, fallback_course="")
        if title and not looks_like_metadata_cell(title):
            return title
    return first_nonempty(cells)


def parse_xiaoya_task_block(text: str, *, course: str, platform: str, url: str) -> PlatformAssignment | None:
    text = compact_text(text)
    if not text or "老师还没有发布任务" in text:
        return None
    if not re.search(r"任务单|作业|任务|提交", text):
        return None
    due_at = find_xiaoya_due_datetime(text)
    if due_at is None:
        return None
    title = extract_xiaoya_task_title(text, fallback_course=course)
    if not title:
        return None
    return PlatformAssignment(
        title=title,
        course=course,
        platform=platform,
        due_at=due_at,
        status=guess_row_status(text.splitlines()),
        url=url,
    )


def find_xiaoya_due_datetime(text: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if re.search(r"截止|到期|结束|完成时间|deadline|due", line, re.IGNORECASE):
            match = re.search(r"(?:截止(?:时间|日期)?|到期(?:时间|日期)?|结束(?:时间|日期)?|完成时间|deadline|due)\s*[:：]?\s*(.+)", line, re.IGNORECASE)
            if match:
                due_at = find_datetime(match.group(1))
                if due_at is not None:
                    return due_at
            due_at = find_datetime(line)
            if due_at is not None:
                return due_at
    return find_datetime(text)


def extract_xiaoya_task_title(text: str, *, fallback_course: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        match = re.search(r"(?:任务单名称|任务名称|作业名称|标题|名称|任务|作业)\s*[:：]\s*(.+)", line)
        if match:
            value = clean_xiaoya_task_title(match.group(1), fallback_course=fallback_course)
            if value:
                return value
    preferred = [line for line in lines if "任务单" in line and clean_xiaoya_task_title(line, fallback_course=fallback_course)]
    preferred.extend(line for line in lines if re.search(r"作业|任务", line) and clean_xiaoya_task_title(line, fallback_course=fallback_course))
    preferred.extend(lines)
    for line in preferred:
        value = clean_xiaoya_task_title(line, fallback_course=fallback_course)
        if value:
            return value
    return ""


def clean_xiaoya_task_title(value: str, *, fallback_course: str) -> str:
    value = compact_text(value).replace("\n", " ")
    value = re.sub(r"(?:截止|到期|结束|完成时间|deadline|due)\s*[:：]?.*$", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^(?:课程|课程名称|状态|类型|发布人|发布对象|开始时间|发布时间|截止时间|结束时间)\s*[:：]\s*", "", value).strip()
    value = re.sub(r"^(?:待完成|未完成|未提交|已完成|已提交|进行中|查看|进入任务)\s*$", "", value).strip()
    if fallback_course:
        value = re.sub(rf"^{re.escape(fallback_course)}\s*[:：｜| -]*", "", value).strip()
    if value in {"任务单", "作业", "任务", "标题", "名称", fallback_course}:
        return ""
    if find_datetime(value) is not None:
        return ""
    return value


def looks_like_metadata_cell(value: str) -> bool:
    return bool(
        re.fullmatch(r"[\\/｜| -]+", value)
        or re.fullmatch(r"全体|个人|小组|作业|任务|测验|问卷|讨论|自主观看|课堂练习", value)
        or re.search(r"已完成|已提交|已批改|未开始|未开放|进行中|未提交|待完成", value)
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
