from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from ..config import DEFAULT_BROWSER_PROFILE_ROOT
from ..datetime_utils import find_datetime, to_iso
from ..parser import extract_field, parse_assignments


class PlatformError(RuntimeError):
    """Base class for platform adapter errors."""


class PlaywrightUnavailableError(PlatformError):
    """Raised when Playwright or the browser runtime is not installed."""


class LoginRequiredError(PlatformError):
    """Raised when a platform profile no longer has a valid login session."""


class PageStructureChangedError(PlatformError):
    """Raised when the adapter cannot recognize the platform page structure."""


ProgressCallback = Callable[[str], None] | None


@dataclass(frozen=True)
class PlatformAssignment:
    title: str
    course: str
    platform: str
    due_at: datetime
    status: str
    url: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["due_at"] = to_iso(self.due_at)
        return data


@dataclass(frozen=True)
class CandidateBlock:
    text: str
    url: str


DEFAULT_CANDIDATE_SELECTORS = [
    "[data-testid*='homework' i]",
    "[data-testid*='assignment' i]",
    "[data-testid*='task' i]",
    "[class*='homework' i]",
    "[class*='assignment' i]",
    "[class*='task' i]",
    "[class*='work' i]",
    "article",
    "li",
    "tr",
]


class PlaywrightPlatformAdapter:
    slug = ""
    platform_name = ""
    start_url = ""
    candidate_selectors = DEFAULT_CANDIDATE_SELECTORS
    timeout_ms = 20_000

    def __init__(
        self,
        *,
        profile_root: Path | str = DEFAULT_BROWSER_PROFILE_ROOT,
        start_url: str | None = None,
        timeout_ms: int | None = None,
    ):
        self.profile_root = Path(profile_root).expanduser()
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.user_data_dir = self.profile_root / self.slug
        self.url = start_url or self.start_url
        self.timeout_ms = timeout_ms or self.timeout_ms

    def manual_login(self) -> None:
        if not sys.stdin.isatty():
            raise RuntimeError("手动登录需要交互式终端。请在 Terminal 中运行此命令。")
        sync_playwright, playwright_error = load_playwright()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                print(f"已打开 {self.platform_name} 登录页：{self.url}")
                print("请在浏览器中手动登录。程序不会读取、保存或提交你的密码。")
                input("登录完成后按回车关闭浏览器并保存本地登录态。")
            except playwright_error as exc:
                raise PlaywrightUnavailableError(format_playwright_error(exc)) from exc
            finally:
                context.close()

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
                emit_progress(progress, f"{self.platform_name}：打开 {self.url}")
                page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except playwright_error:
                    pass
                if self.is_login_required(page):
                    raise LoginRequiredError(
                        f"{self.platform_name} 登录状态已失效。请运行：hw login {self.slug}"
                    )
                blocks = self.collect_candidate_blocks(page)
                emit_progress(progress, f"{self.platform_name}：识别到 {len(blocks)} 个候选作业块")
                assignments = self.parse_candidate_blocks(blocks, fallback_url=page.url)
                if not assignments:
                    body_text = safe_body_text(page)
                    if looks_like_empty_state(body_text):
                        return []
                    raise PageStructureChangedError(
                        f"{self.platform_name} 页面结构可能已变化：未能从 {page.url} 识别出包含截止时间的作业。"
                        f"已尝试选择器：{', '.join(self.candidate_selectors)}"
                    )
                return assignments
            except (LoginRequiredError, PageStructureChangedError):
                raise
            except playwright_error as exc:
                raise PlaywrightUnavailableError(format_playwright_error(exc)) from exc
            finally:
                context.close()

    def _launch_context(self, playwright, *, headless: bool):
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=headless,
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
            )
        except Exception as exc:
            raise PlaywrightUnavailableError(format_playwright_error(exc)) from exc

    def wait_until_ready(self, page, *, network_timeout: int = 8_000, settle_ms: int = 800) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=network_timeout)
        except Exception:
            pass
        if settle_ms:
            try:
                page.wait_for_timeout(settle_ms)
            except Exception:
                pass

    def is_login_required(self, page) -> bool:
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

    def collect_candidate_blocks(self, page) -> list[CandidateBlock]:
        seen: set[tuple[str, str]] = set()
        blocks: list[CandidateBlock] = []
        for selector in self.candidate_selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 80)
            except Exception:
                continue
            for index in range(count):
                node = locator.nth(index)
                try:
                    text = compact_text(node.inner_text(timeout=1_000))
                except Exception:
                    continue
                if not looks_like_assignment_block(text):
                    continue
                url = self.extract_node_url(page.url, node)
                key = (text, url)
                if key in seen:
                    continue
                seen.add(key)
                blocks.append(CandidateBlock(text=text, url=url))
        if blocks:
            return blocks
        try:
            body_text = page.locator("body").inner_text(timeout=3_000)
        except Exception:
            return []
        for text in split_body_into_blocks(body_text):
            compacted = compact_text(text)
            if looks_like_assignment_block(compacted):
                blocks.append(CandidateBlock(text=compacted, url=page.url))
        return blocks

    def extract_node_url(self, base_url: str, node) -> str:
        try:
            href = node.locator("a[href]").first.get_attribute("href", timeout=500)
        except Exception:
            href = None
        return urljoin(base_url, href) if href else base_url

    def parse_candidate_blocks(
        self,
        blocks: list[CandidateBlock],
        *,
        fallback_url: str,
        fallback_course: str = "",
    ) -> list[PlatformAssignment]:
        assignments: list[PlatformAssignment] = []
        seen: set[tuple[str, str, str, str]] = set()
        for block in blocks:
            parsed_items = parse_assignments(block.text)
            for item in parsed_items:
                due_at = item.due_at or find_datetime(block.text)
                if due_at is None:
                    continue
                title = sanitize_title(item.title)
                if not title:
                    continue
                course = item.course or guess_course(block.text) or fallback_course
                status = guess_status(block.text)
                url = block.url or fallback_url
                key = (title.casefold(), course.casefold(), due_at.isoformat(), url)
                if key in seen:
                    continue
                seen.add(key)
                assignments.append(
                    PlatformAssignment(
                        title=title,
                        course=course,
                        platform=self.platform_name,
                        due_at=due_at,
                        status=status,
                        url=url,
                    )
                )
        return assignments


def load_playwright():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightUnavailableError(
            "未安装 Playwright。请运行：python3 -m pip install -e . && python3 -m playwright install chromium"
        ) from exc
    return sync_playwright, PlaywrightError


def emit_progress(progress: ProgressCallback, message: str) -> None:
    if progress is not None:
        progress(message)


def format_playwright_error(exc: Exception) -> str:
    message = str(exc).strip()
    if "Executable doesn't exist" in message or "playwright install" in message:
        return "Playwright 浏览器未安装。请运行：python3 -m playwright install chromium"
    return message or exc.__class__.__name__


def compact_text(text: str) -> str:
    lines = [" ".join(line.strip().split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def looks_like_assignment_block(text: str) -> bool:
    if len(text) < 8:
        return False
    if find_datetime(text) is None and not re.search(r"截止|到期|Due|deadline", text, re.IGNORECASE):
        return False
    return bool(re.search(r"作业|任务|实验|报告|提交|homework|assignment|task", text, re.IGNORECASE))


def looks_like_empty_state(text: str) -> bool:
    return bool(
        re.search(
            r"暂无(?:作业|任务|待办)|没有(?:作业|任务|待办)|无(?:作业|任务|待办)|No\s+(?:homework|assignments|tasks)",
            text,
            re.IGNORECASE,
        )
    )


def safe_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""


def split_body_into_blocks(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n")
    parts = re.split(r"\n{2,}|(?=课程\s*[:：])|(?=作业\s*[:：])|(?=任务\s*[:：])", normalized)
    return [part.strip() for part in parts if part.strip()]


def guess_course(text: str) -> str:
    field = extract_field(text, "course")
    if field:
        return field
    for line in text.splitlines():
        match = re.search(r"(?:课程|科目|班课)\s*[:：]\s*(.+)", line)
        if match:
            return clean_metadata(match.group(1))
    return ""


def guess_status(text: str) -> str:
    checks = [
        ("逾期", ["已逾期", "逾期", "已截止", "超时"]),
        ("未提交", ["未提交", "待提交", "未完成", "待完成"]),
        ("已提交", ["已提交", "提交成功", "已完成", "已交"]),
        ("待批改", ["待批改", "批改中"]),
        ("已批改", ["已批改", "已评分", "成绩"]),
    ]
    for status, markers in checks:
        if any(marker in text for marker in markers):
            return status
    return "未知"


def sanitize_title(title: str) -> str:
    title = clean_metadata(title)
    title = re.sub(r"^(?:作业|任务|标题|作业名称|任务名称)\s*[:：]\s*", "", title)
    title = re.sub(r"\s*(?:截止|到期)(?:时间|日期)?\s*[:：]?\s*.*$", "", title)
    return title.strip()


def clean_metadata(value: str) -> str:
    value = " ".join((value or "").strip().split())
    value = re.sub(r"\s+(?:平台|来源|课程|截止|到期|状态)\s*[:：].*$", "", value)
    return value.strip()
