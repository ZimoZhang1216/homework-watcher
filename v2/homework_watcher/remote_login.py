from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import async_playwright

from .settings import Settings, resolve_path


NOVNC_WEBSOCKET_PATH = "vnc/websockify"
LOGIN_SESSION_TTL_SECONDS = int(os.environ.get("HW_V2_LOGIN_SESSION_TTL_SECONDS", "1800"))
YUKETANG_URL = os.environ.get(
    "HW_CHANGJIANG_YUKETANG_URL",
    "https://changjiang.yuketang.cn/v2/web/index",
)
XIAOYA_URL = os.environ.get(
    "HW_XIAOYA_URL",
    "https://nankai.ai-augmented.com/app/jx-web/mycourse",
)


@dataclass(frozen=True)
class LoginStatus:
    platform: str
    slug: str
    user_key: str
    user_label: str
    started_at: str
    expires_in_seconds: int


class RemoteLoginManager:
    def __init__(self) -> None:
        self.active: dict[str, dict[str, object]] = {}

    async def start_yuketang(
        self,
        settings: Settings,
        *,
        user_key: str = "default",
        user_label: str = "默认账号",
    ) -> LoginStatus:
        return await self.start_platform(
            settings,
            platform="长江雨课堂",
            slug="changjiang-yuketang",
            url=YUKETANG_URL,
            user_key=user_key,
            user_label=user_label,
            prepare_context=prefer_student_entry,
            prepare_page=ensure_student_tab,
        )

    async def start_xiaoya(
        self,
        settings: Settings,
        *,
        user_key: str = "default",
        user_label: str = "默认账号",
    ) -> LoginStatus:
        return await self.start_platform(
            settings,
            platform="小雅",
            slug="xiaoya",
            url=XIAOYA_URL,
            user_key=user_key,
            user_label=user_label,
        )

    async def start_platform(
        self,
        settings: Settings,
        *,
        platform: str,
        slug: str,
        url: str,
        user_key: str,
        user_label: str,
        prepare_context=None,
        prepare_page=None,
    ) -> LoginStatus:
        await self.close_expired()
        active = self.active.get(user_key)
        if active is not None:
            return self.status(user_key)  # type: ignore[return-value]

        profile_dir = profile_dir_for_user_platform(settings, user_key, slug)
        profile_dir.mkdir(parents=True, exist_ok=True)
        remove_chromium_profile_locks(profile_dir)

        playwright = await async_playwright().start()
        context = None
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            if prepare_context is not None:
                await prepare_context(context)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            if prepare_page is not None:
                await prepare_page(page)
        except Exception:
            if context is not None:
                await context.close()
            await playwright.stop()
            raise

        self.active[user_key] = {
            "platform": platform,
            "slug": slug,
            "user_key": user_key,
            "user_label": user_label,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_monotonic": time.monotonic(),
            "playwright": playwright,
            "context": context,
        }
        return self.status(user_key)  # type: ignore[return-value]

    async def finish(self, user_key: str | None = None) -> None:
        if user_key is None:
            active_items = list(self.active.items())
            self.active.clear()
        else:
            active = self.active.pop(user_key, None)
            active_items = [(user_key, active)] if active is not None else []
        for _key, active in active_items:
            await close_active_login(active)

    async def close_expired(self, user_key: str | None = None) -> None:
        if user_key is not None:
            status = self.status(user_key)
            if status is not None and status.expires_in_seconds <= 0:
                await self.finish(user_key)
            return
        for key in list(self.active):
            status = self.status(key)
            if status is not None and status.expires_in_seconds <= 0:
                await self.finish(key)

    def status(self, user_key: str | None = None) -> LoginStatus | None:
        if user_key is None:
            if not self.active:
                return None
            active = next(iter(self.active.values()))
        else:
            active = self.active.get(user_key)
        if active is None:
            return
        started = float(active.get("started_monotonic", time.monotonic()))
        expires_in = max(0, int(LOGIN_SESSION_TTL_SECONDS - (time.monotonic() - started)))
        return LoginStatus(
            platform=str(active["platform"]),
            slug=str(active["slug"]),
            user_key=str(active["user_key"]),
            user_label=str(active["user_label"]),
            started_at=str(active["started_at"]),
            expires_in_seconds=expires_in,
        )


async def close_active_login(active: dict[str, object]) -> None:
    context = active.get("context")
    playwright = active.get("playwright")
    if context is not None:
        await context.close()
    if playwright is not None:
        await playwright.stop()


async def prefer_student_entry(context) -> None:
    await context.add_init_script(
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


async def ensure_student_tab(page) -> None:
    try:
        await page.evaluate(
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
        await page.wait_for_timeout(800)
    except Exception:
        pass


def build_novnc_url(base_url: str) -> str:
    configured = os.environ.get("HW_WEB_NOVNC_URL", "").strip()
    if not configured:
        configured = f"{base_url.rstrip('/')}/vnc/vnc.html"
    return normalize_novnc_url(configured)


def normalize_novnc_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("autoconnect", "1")
    query.setdefault("resize", "scale")
    if query.get("path", "") in {"", "websockify"}:
        query["path"] = NOVNC_WEBSOCKET_PATH
    path = parts.path or "/vnc/vnc.html"
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


def profile_dir_for_user_platform(settings: Settings, user_key: str, platform: str) -> Path:
    safe_user_key = safe_path_segment(user_key)
    safe_platform = safe_path_segment(platform)
    return resolve_path(settings.playwright_user_data_dir) / "users" / safe_user_key / safe_platform


def safe_path_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    return cleaned.strip("-") or "default"


def remove_chromium_profile_locks(profile_dir: Path) -> None:
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile_dir / lock_name
        try:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink()
        except OSError:
            pass
