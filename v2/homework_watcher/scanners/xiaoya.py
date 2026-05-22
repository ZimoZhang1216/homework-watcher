from __future__ import annotations

import re
import time as monotonic_time
from datetime import datetime, time
from pathlib import Path

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.config_loader import KnownCourseConfig
from homework_watcher.debug_dump import dump_debug_page
from homework_watcher.remote_login import profile_dir_for_user_platform
from homework_watcher.settings import Settings, load_settings
from homework_watcher.status import normalize_status
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


XIAOYA_PLATFORM_LABEL = "小雅"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?")
STATUS_RE = re.compile(r"进行中|未开始|已完成|已截止|未提交|待完成|未完成|已提交|已批阅")
MAX_XIAOYA_PAGES = 20
XIAOYA_BOOTSTRAP_LOADING_MARKERS = (
    "正在加载应用",
    "正在加载环境配置",
)
XIAOYA_TASK_READY_MARKERS = (
    "作业任务",
    "全部任务",
    "任务类型",
    "截止时间",
    "进入任务",
    "暂无数据",
    "暂无任务",
    "暂无作业",
    "没有数据",
    "无数据",
)
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
    "作业任务",
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
        if normalize_title(line) == normalize_title(course):
            continue
        if not possible_task_start_line(line):
            continue
        block = collect_task_text_block(lines, index)
        candidate = parse_xiaoya_task_row(
            split_task_text_block(block),
            course=course,
            task_url=task_url,
            course_id=course_id,
        )
        if candidate is None:
            continue
        key = (candidate.title, candidate.due_at)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    return candidates


def parse_xiaoya_task_row(
    cells: list[str], *, course: str, task_url: str, course_id: str = ""
) -> AssignmentCandidate | None:
    cleaned = [normalize_cell(cell) for cell in cells if normalize_cell(cell)]
    if not cleaned:
        return None
    joined = "\n".join(cleaned)
    dates = DATE_RE.findall(joined)
    status_match = STATUS_RE.search(joined)
    if not dates or not status_match:
        return None
    due_at = parse_xiaoya_due_at(dates[-1])
    status_raw = status_match.group(0)
    title = extract_title_from_cells(cleaned)
    if not title or is_course_summary(title=title, course=course, status_raw=status_raw, due_at=due_at):
        return None
    return AssignmentCandidate(
        platform=XIAOYA_PLATFORM_LABEL,
        course=course,
        title=title,
        status_raw=status_raw,
        due_at=due_at,
        url=task_url,
        source_key=f"xiaoya:{course_id or course}:{title}:{due_at.isoformat(timespec='seconds')}",
        raw_snapshot=sanitize_snapshot(joined[:500]),
    )


def extract_title_from_cells(cells: list[str]) -> str:
    for cell in cells:
        title = extract_title_from_line(cell)
        if title:
            return title
    return ""


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
    if looks_like_metadata(text):
        return ""
    has_row_context = "\\" in text or bool(STATUS_RE.search(text)) or bool(DATE_RE.search(text))
    if "作业" in text and has_row_context:
        match = re.match(r"(?P<title>.+?)\s*\\?\s*作业(?:\s|$)", text)
        if match:
            return normalize_title(match.group("title"))
    if "测验" in text and has_row_context:
        match = re.match(r"(?P<title>.+?)\s*\\?\s*测验(?:\s|$)", text)
        if match:
            return normalize_title(match.group("title"))
    if STATUS_RE.search(text):
        before_status = STATUS_RE.split(text, maxsplit=1)[0]
        return normalize_title(before_status.replace("\\", " ").replace("作业", " ").replace("测验", " "))
    if len(text) > 80:
        return ""
    return normalize_title(text)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip(" /\\|")


def normalize_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def possible_task_start_line(line: str) -> bool:
    title = extract_title_from_line(line)
    if not title:
        return False
    return not DATE_RE.search(title)


def collect_task_text_block(lines: list[str], start_index: int) -> str:
    block = [lines[start_index]]
    for line in lines[start_index + 1 : start_index + 20]:
        current = "\n".join(block)
        if possible_task_start_line(line) and DATE_RE.search(current) and STATUS_RE.search(current):
            break
        block.append(line)
        if line == "进入任务" and DATE_RE.search(current):
            break
    return "\n".join(block)


def split_task_text_block(block: str) -> list[str]:
    cells: list[str] = []
    for line in block.splitlines():
        pieces = re.split(r"\t+|\s{2,}", line.strip())
        if len(pieces) == 1:
            pieces = [line]
        cells.extend(piece.strip() for piece in pieces if piece.strip())
    return cells


def looks_like_metadata(text: str) -> bool:
    if text in {"全体", "个人", "小组", "公开", "私有"}:
        return True
    if text.startswith(("共", "第")) and ("页" in text or "条" in text):
        return True
    return False


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

    def __init__(self, settings: Settings | None = None, *, headless: bool = True) -> None:
        self.settings = settings or load_settings()
        self.headless = headless

    @property
    def profile_dir(self) -> Path:
        return profile_dir_for_user_platform(self.settings, "default", "xiaoya")

    def profile_dir_for_user(self, user_key: str) -> Path:
        return profile_dir_for_user_platform(self.settings, user_key, "xiaoya")

    def scan(self, context) -> list[AssignmentCandidate]:
        config = context.platform_config
        if config is None or not config.enabled:
            context.emit(5, "小雅：未启用，跳过")
            return []
        if not config.known_courses:
            context.emit(5, "小雅：没有配置 known_courses，跳过")
            return []

        profile_dir = self.profile_dir_for_user(context.user_key)
        profile_dir.mkdir(parents=True, exist_ok=True)
        context.emit(10, "小雅：打开浏览器登录态")
        results: list[AssignmentCandidate] = []
        with sync_playwright() as playwright:
            browser_context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self.headless,
                viewport={"width": 1400, "height": 1000},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
                for index, course in enumerate(config.known_courses, start=1):
                    percent = 15 + int(index / max(len(config.known_courses), 1) * 70)
                    context.emit(percent, f"小雅：扫描 known course {index}/{len(config.known_courses)} {course.course}")
                    try:
                        results.extend(
                            self.scan_known_course_page(
                                page,
                                course,
                                course_timeout_seconds=30,
                                scan_id=context.scan_id,
                                emit=lambda message, p=percent: context.emit(p, message),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - one course must not block the platform.
                        context.emit(percent, f"小雅：课程 {course.course} 失败，已跳过：{type(exc).__name__}: {exc}")
                context.emit(88, f"小雅：完成，识别 {len(results)} 条")
                return results
            finally:
                browser_context.close()

    def scan_known_course_page(
        self,
        page: Page,
        course: KnownCourseConfig,
        *,
        course_timeout_seconds: int,
        scan_id: str = "manual",
        emit=None,
    ) -> list[AssignmentCandidate]:
        deadline = monotonic_time.monotonic() + course_timeout_seconds
        page.goto(course.task_url, wait_until="domcontentloaded", timeout=min(10000, course_timeout_seconds * 1000))
        remaining_ms = max(1000, int((deadline - monotonic_time.monotonic()) * 1000))
        if emit:
            emit(f"小雅：等待课程 {course.course} 任务页加载完成")
        text = wait_for_xiaoya_task_page_ready(page, timeout_ms=remaining_ms)
        if not text.strip() or looks_like_xiaoya_bootstrap_loading(text):
            dump_debug_page(
                page,
                self.settings,
                scan_id=scan_id,
                stage="not-ready",
                course=course.course,
                page_no=1,
            )
            raise RuntimeError("小雅任务页加载超时，仍停在应用启动页")
        if looks_like_login_page(text):
            dump_debug_page(
                page,
                self.settings,
                scan_id=scan_id,
                stage="login-or-empty",
                course=course.course,
                page_no=1,
            )
            raise RuntimeError("小雅登录态可能失效，请先运行 login-xiaoya 手动登录")
        all_candidates: list[AssignmentCandidate] = []
        seen_pages: set[str] = set()
        seen_assignments: set[tuple[str, datetime]] = set()

        for page_no in range(1, MAX_XIAOYA_PAGES + 1):
            if monotonic_time.monotonic() >= deadline:
                if emit:
                    emit(f"小雅：课程 {course.course} 超过 {course_timeout_seconds} 秒，返回已解析结果")
                break
            page_key = compact_page_key(text)
            if page_key in seen_pages:
                if emit:
                    emit(f"小雅：课程 {course.course} 页码内容重复，停止分页")
                break
            seen_pages.add(page_key)

            page_candidates = self.scan_known_course_page_content(page, course, text)
            if page_no == 1 and not page_candidates:
                dump_debug_page(
                    page,
                    self.settings,
                    scan_id=scan_id,
                    stage="empty-parse",
                    course=course.course,
                    page_no=page_no,
                )
            for candidate in page_candidates:
                key = (candidate.title, candidate.due_at)
                if key in seen_assignments:
                    continue
                seen_assignments.add(key)
                all_candidates.append(candidate)
            if emit:
                emit(
                    f"小雅：课程 {course.course} 第 {page_no} 页识别 {len(page_candidates)} 条 "
                    f"titles={[candidate.title for candidate in page_candidates]}"
                )

            if page_no >= MAX_XIAOYA_PAGES:
                break
            previous_text = text
            if not click_next_page(page):
                break
            try:
                page.wait_for_function(
                    "(oldText) => document.body && document.body.innerText !== oldText",
                    arg=previous_text,
                    timeout=10000,
                )
                remaining_ms = max(1000, int((deadline - monotonic_time.monotonic()) * 1000))
                text = wait_for_xiaoya_task_page_ready(page, timeout_ms=remaining_ms)
            except PlaywrightTimeoutError:
                if emit:
                    emit(f"小雅：课程 {course.course} 翻页后内容未变化，停止分页")
                break

        return all_candidates

    def scan_known_course_text(self, course: KnownCourseConfig, text: str) -> list[AssignmentCandidate]:
        return parse_xiaoya_task_text(
            text,
            course=course.course,
            task_url=course.task_url,
            course_id=course.course_id,
        )

    def scan_known_course_page_content(
        self, page: Page, course: KnownCourseConfig, text: str
    ) -> list[AssignmentCandidate]:
        dom_candidates = parse_xiaoya_task_rows(
            evaluate_task_rows(page),
            course=course.course,
            task_url=course.task_url,
            course_id=course.course_id,
        )
        if dom_candidates:
            return dom_candidates
        return self.scan_known_course_text(course, text)


def parse_xiaoya_task_rows(
    rows: list[list[str]], *, course: str, task_url: str, course_id: str = ""
) -> list[AssignmentCandidate]:
    candidates: list[AssignmentCandidate] = []
    seen: set[tuple[str, datetime]] = set()
    for row in rows:
        candidate = parse_xiaoya_task_row(
            row,
            course=course,
            task_url=task_url,
            course_id=course_id,
        )
        if candidate is None:
            continue
        key = (candidate.title, candidate.due_at)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def evaluate_task_rows(page: Page) -> list[list[str]]:
    rows: list[list[str]] = []
    rows.extend(evaluate_dom_task_rows(page))
    rows.extend(evaluate_visual_task_rows(page))
    seen: set[str] = set()
    unique: list[list[str]] = []
    for row in rows:
        cleaned = collapse_duplicate_cells([normalize_cell(str(cell)) for cell in row if normalize_cell(str(cell))])
        key = "\n".join(cleaned)
        if len(cleaned) < 3 or key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


def evaluate_dom_task_rows(page: Page) -> list[list[str]]:
    try:
        raw_rows = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('tbody tr, .ant-list-item, [role="row"]'))
              .slice(0, 240)
              .map(row => {
                const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
                const cells = Array.from(row.querySelectorAll('td, [role="cell"], .ant-list-item-meta-title, .ant-list-item-meta-description, [class*="cell"]'))
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


def evaluate_visual_task_rows(page: Page) -> list[list[str]]:
    try:
        raw_rows = page.evaluate(
            """
            () => {
              const compact = value => (value || '').replace(/\\s+/g, ' ').trim();
              const cells = Array.from(document.querySelectorAll('.ant-table tbody tr:not(.ant-table-measure-row) td, table tbody tr td, [role="row"] [role="cell"]'))
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


def read_visible_text(page: Page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5000))
    except PlaywrightTimeoutError:
        return str(page.evaluate("() => document.body ? document.body.innerText : ''"))


def wait_for_xiaoya_task_page_ready(page: Page, *, timeout_ms: int) -> str:
    deadline = monotonic_time.monotonic() + max(timeout_ms, 1000) / 1000
    last_text = ""
    while monotonic_time.monotonic() < deadline:
        last_text = read_body_inner_text(page)
        if looks_like_xiaoya_task_page_ready(last_text):
            return last_text
        page.wait_for_timeout(500)
    return last_text or read_visible_text(page)


def read_body_inner_text(page: Page) -> str:
    try:
        return str(page.evaluate("() => document.body ? document.body.innerText : ''"))
    except Exception:  # noqa: BLE001 - polling should keep waiting through transient page states.
        return ""


def looks_like_xiaoya_task_page_ready(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if looks_like_login_page(text):
        return True
    if looks_like_xiaoya_bootstrap_loading(text):
        return False
    if any(marker in compact for marker in XIAOYA_TASK_READY_MARKERS):
        return True
    return bool(DATE_RE.search(text) and STATUS_RE.search(text))


def looks_like_xiaoya_bootstrap_loading(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    return any(marker in compact for marker in XIAOYA_BOOTSTRAP_LOADING_MARKERS) and not any(
        marker in compact for marker in XIAOYA_TASK_READY_MARKERS
    )


def looks_like_login_page(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    login_markers = ("登录", "统一身份认证", "验证码", "账号", "密码")
    task_markers = ("作业任务", "全部任务", "课程内容")
    return any(marker in compact for marker in login_markers) and not any(
        marker in compact for marker in task_markers
    )


def compact_page_key(text: str) -> str:
    return re.sub(r"\s+", "", text)[:4000]


def click_next_page(page: Page) -> bool:
    return bool(
        page.evaluate(
            """
            () => {
              const selectors = [
                '.ant-pagination-next:not(.ant-pagination-disabled)',
                'li[title="下一页"]:not(.ant-pagination-disabled)',
                '.el-pagination .btn-next:not(:disabled)',
                'button[aria-label*="Next"]:not([disabled])',
                'button[aria-label*="下一页"]:not([disabled])'
              ];
              for (const selector of selectors) {
                const element = document.querySelector(selector);
                if (element && element.offsetParent !== null) {
                  element.click();
                  return true;
                }
              }
              const elements = Array.from(document.querySelectorAll('button,a,li,span'));
              for (const element of elements) {
                const text = (element.innerText || element.getAttribute('aria-label') || element.title || '').trim();
                const klass = String(element.className || '');
                const disabled = element.disabled ||
                  element.getAttribute('aria-disabled') === 'true' ||
                  klass.includes('disabled') ||
                  klass.includes('is-disabled');
                if (!disabled && element.offsetParent !== null && ['下一页', '>', '›', '»'].includes(text)) {
                  element.click();
                  return true;
                }
              }
              return false;
            }
            """
        )
    )


def login_xiaoya(settings: Settings | None = None) -> None:
    active_settings = settings or load_settings()
    profile_dir = profile_dir_for_user_platform(active_settings, "default", "xiaoya")
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1400, "height": 1000},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://nankai.ai-augmented.com/app/jx-web/mycourse", wait_until="domcontentloaded")
            print("已打开小雅登录页。请在浏览器中手动登录；程序不会读取、保存或提交你的密码。")
            input("登录完成后按回车关闭浏览器并保存本地登录态。")
        finally:
            context.close()
