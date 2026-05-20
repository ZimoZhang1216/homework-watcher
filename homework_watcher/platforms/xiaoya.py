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


class XiaoyaAdapter(PlaywrightPlatformAdapter):
    slug = "xiaoya"
    platform_name = "小雅"
    start_url = os.environ.get(
        "HW_XIAOYA_URL",
        "https://nankai.ai-augmented.com/app/jx-web/mycourse",
    )
    scan_timeout_seconds = int(os.environ.get("HW_XIAOYA_SCAN_TIMEOUT_SECONDS", "120"))
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

                pending_assignments = fetch_pending_task_assignments(
                    page,
                    platform=self.platform_name,
                    progress=progress,
                    deadline=deadline,
                    start_url=self.url,
                )
                if pending_assignments:
                    pending_assignments = dedupe_assignments(pending_assignments)
                    emit_progress(progress, f"{self.platform_name}：待完成任务页识别 {len(pending_assignments)} 条任务")
                    return pending_assignments

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


def fetch_pending_task_assignments(
    page,
    *,
    platform: str,
    progress: ProgressCallback,
    deadline: float,
    start_url: str,
) -> list[PlatformAssignment]:
    emit_progress(progress, f"{platform}：尝试待完成任务页")
    original_url = page.url
    for attempt, target in enumerate(pending_task_targets(page, start_url), start=1):
        ensure_scan_time_left(deadline, "打开待完成任务页")
        try:
            if target == "__click__":
                if not click_pending_task_entry(page):
                    continue
            else:
                page.goto(target, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, 12_000))
            wait_for_pending_task_page(page)
            assignments = parse_xiaoya_task_page(
                page,
                course="",
                platform=platform,
                deadline=deadline,
                max_pages=8,
            )
        except PlaywrightUnavailableError:
            raise
        except Exception as exc:
            emit_progress(progress, f"{platform}：待完成入口 {attempt} 未可用：{truncate(str(exc), 36)}")
            continue
        if assignments:
            return assignments
        emit_progress(progress, f"{platform}：待完成入口 {attempt} 暂无可识别任务")
        try:
            page.goto(original_url, wait_until="domcontentloaded", timeout=remaining_timeout_ms(deadline, 8_000))
        except Exception:
            pass
    return []


def pending_task_targets(page, start_url: str) -> list[str]:
    targets = ["__click__"]
    try:
        hrefs = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href], [data-url], [data-href]'))
              .map(node => ({
                text: (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim(),
                href: node.getAttribute('href') || node.getAttribute('data-url') || node.getAttribute('data-href') || ''
              }))
              .filter(item => /待完成|待办|待处理|我的任务|任务中心/.test(item.text)
                || /todo|pending|task|homework|mission/i.test(item.href))
              .map(item => item.href)
            """
        )
    except Exception:
        hrefs = []
    for href in hrefs:
        target = urljoin(page.url or start_url, str(href))
        if target not in targets:
            targets.append(target)
    for target in derived_pending_task_urls(start_url):
        if target not in targets:
            targets.append(target)
    return targets


def derived_pending_task_urls(start_url: str) -> list[str]:
    parsed = urlsplit(start_url)
    paths = []
    if "/mycourse" in parsed.path:
        base = parsed.path.split("/mycourse", 1)[0]
        paths.extend(
            [
                f"{base}/todo",
                f"{base}/task",
                f"{base}/mytask",
                f"{base}/pending",
                f"{base}/homework",
                f"{base}/mission",
            ]
        )
    return [urlunsplit((parsed.scheme, parsed.netloc, path, "", "")) for path in paths]


def click_pending_task_entry(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const re = /待完成|待办|待处理|我的任务|任务中心/;
                  const nodes = Array.from(document.querySelectorAll(
                    'a, button, [role="button"], .ant-menu-item, .ant-tabs-tab, .ant-tabs-tab-btn, [class*="task"], [class*="todo"]'
                  ));
                  const target = nodes.find(node => re.test((node.innerText || node.textContent || '').trim()));
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def wait_for_pending_task_page(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=4_000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=6_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(900)
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
    assignments = collect_visible_task_rows(page, course=course, platform=platform)
    for _ in range(max_pages):
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
    try:
        row_texts = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('tbody tr, .ant-list-item, [class*="task"][class*="item"]'))
              .slice(0, 240)
              .map(row => Array.from(row.querySelectorAll('td, .ant-list-item-meta-title, .ant-list-item-meta-description, [class*="cell"]'))
                .map(cell => (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
              )
              .filter(cells => cells.length)
            """
        )
    except Exception:
        row_texts = []
    assignments: list[PlatformAssignment] = []
    for raw_cells in row_texts:
        cells = [compact_text(str(cell)) for cell in raw_cells if str(cell).strip()]
        item = parse_xiaoya_row(cells, course=course, platform=platform, url=page.url)
        if item is not None:
            assignments.append(item)
    if assignments:
        return assignments

    rows = page.locator("tbody tr")
    for index in range(min(rows.count(), 200)):
        row = rows.nth(index)
        cells = row.locator("td")
        cell_texts: list[str] = []
        for cell_index in range(cells.count()):
            text = compact_text(cells.nth(cell_index).inner_text(timeout=500))
            cell_texts.append(text)
        item = parse_xiaoya_row(cell_texts, course=course, platform=platform, url=page.url)
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
) -> PlatformAssignment | None:
    if len(cells) < 4:
        return None
    joined = "\n".join(cells)
    due_source = cells[8] if len(cells) > 8 and cells[8].strip() else joined
    due_at = find_datetime(due_source)
    if due_at is None:
        return None
    row_course, title = infer_course_and_title(cells, fallback_course=course)
    if not title or title in {"标题", "老师还没有发布任务，敬请期待吧！"}:
        return None
    status = guess_row_status(cells)
    return PlatformAssignment(
        title=title,
        course=row_course,
        platform=platform,
        due_at=due_at,
        status=status,
        url=url,
    )


def infer_course_and_title(cells: list[str], *, fallback_course: str) -> tuple[str, str]:
    values = [cell.strip() for cell in cells if cell.strip()]
    if fallback_course:
        return fallback_course, first_task_title(values)
    due_index = next((index for index, cell in enumerate(values) if find_datetime(cell) is not None), len(values))
    candidates = [
        value
        for value in values[:due_index]
        if not looks_like_status(value)
        and not looks_like_task_type(value)
        and find_datetime(value) is None
    ]
    if len(candidates) >= 2:
        return candidates[0], candidates[1]
    return "", first_task_title(values)


def first_task_title(cells: list[str]) -> str:
    for cell in cells:
        if looks_like_status(cell) or looks_like_task_type(cell) or find_datetime(cell) is not None:
            continue
        return cell.strip()
    return first_nonempty(cells)


def looks_like_status(value: str) -> bool:
    return any(marker in value for marker in ["未提交", "待完成", "未完成", "已提交", "已完成", "已批改", "未开始", "未开放"])


def looks_like_task_type(value: str) -> bool:
    return value.strip() in {"作业", "任务", "考试", "测验", "测试", "问卷", "讨论", "资料"}


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
