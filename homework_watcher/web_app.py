from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr
from html import escape
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .config import APP_DIR
from .datetime_utils import now_local
from .db import HomeworkDB
from .email_report import (
    EmailConfig,
    build_email_report,
    build_email_subject,
    is_recurring_assignment,
    send_email_report,
    truthy,
)
from .platforms import ADAPTER_CLASSES, canonical_slugs
from .platforms.base import (
    LoginRequiredError,
    PageStructureChangedError,
    PlaywrightUnavailableError,
    format_playwright_error,
)
from .recurring_assignments import materialize_recurring_assignments
from .statuses import assignment_is_done, platform_status_is_done


WEB_DIR = Path(os.environ.get("HW_WEB_DIR", APP_DIR / "web")).expanduser()
WEB_DB_PATH = Path(os.environ.get("HW_WEB_DB_PATH", WEB_DIR / "web.db")).expanduser()
SESSION_COOKIE = "homework_watcher_session"
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 260_000
JobProgress = Callable[[str, int | None], None]


@dataclass(frozen=True)
class WebUser:
    id: int
    email: str
    report_email: str
    created_at: str


@dataclass(frozen=True)
class WebJob:
    id: int
    user_id: int
    kind: str
    status: str
    message: str
    progress: int
    created_at: str
    updated_at: str


class WebStore:
    def __init__(self, path: Path | str = WEB_DB_PATH):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    report_email TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    progress INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            self.ensure_columns()
            self.conn.commit()

    def ensure_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "progress" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")

    def create_user(self, *, email: str, password: str, report_email: str = "") -> WebUser:
        email = normalize_email(email)
        report_email = normalize_email(report_email or email)
        validate_email(email)
        validate_email(report_email)
        if len(password) < 10:
            raise ValueError("密码至少需要 10 个字符。")
        timestamp = timestamp_now()
        password_hash = hash_password(password)
        with self.lock:
            try:
                cursor = self.conn.execute(
                    """
                    INSERT INTO users (email, password_hash, report_email, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (email, password_hash, report_email, timestamp),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("这个邮箱已经注册。") from exc
            return self.get_user(cursor.lastrowid)

    def authenticate(self, *, email: str, password: str) -> WebUser | None:
        row = self.get_user_row_by_email(normalize_email(email))
        if row is None:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return row_to_user(row)

    def get_user(self, user_id: int) -> WebUser:
        row = self.get_user_row(user_id)
        if row is None:
            raise KeyError(f"user not found: {user_id}")
        return row_to_user(row)

    def get_user_row(self, user_id: int):
        with self.lock:
            return self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def get_user_row_by_email(self, email: str):
        with self.lock:
            return self.conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    def update_report_email(self, *, user_id: int, report_email: str) -> WebUser:
        report_email = normalize_email(report_email)
        validate_email(report_email)
        with self.lock:
            self.conn.execute("UPDATE users SET report_email = ? WHERE id = ?", (report_email, user_id))
            self.conn.commit()
        return self.get_user(user_id)

    def list_users(self) -> list[WebUser]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        return [row_to_user(row) for row in rows]

    def create_job(self, *, user_id: int, kind: str) -> WebJob:
        timestamp = timestamp_now()
        with self.lock:
            cursor = self.conn.execute(
                """
                INSERT INTO jobs (user_id, kind, status, message, progress, created_at, updated_at)
                VALUES (?, ?, 'running', '等待开始', 0, ?, ?)
                """,
                (user_id, kind, timestamp, timestamp),
            )
            self.conn.commit()
            return self.get_job(cursor.lastrowid)

    def finish_job(self, *, job_id: int, status: str, message: str) -> None:
        progress = 100 if status == "success" else 0
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET status = ?, message = ?, progress = ?, updated_at = ? WHERE id = ?",
                (status, message, progress, timestamp_now(), job_id),
            )
            self.conn.commit()

    def update_job_progress(self, *, job_id: int, message: str, progress: int | None = None) -> None:
        normalized = None if progress is None else max(0, min(100, int(progress)))
        with self.lock:
            if normalized is None:
                self.conn.execute(
                    "UPDATE jobs SET message = ?, updated_at = ? WHERE id = ?",
                    (message, timestamp_now(), job_id),
                )
            else:
                self.conn.execute(
                    "UPDATE jobs SET message = ?, progress = ?, updated_at = ? WHERE id = ?",
                    (message, normalized, timestamp_now(), job_id),
                )
            self.conn.commit()

    def get_job(self, job_id: int) -> WebJob:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        return row_to_job(row)

    def recent_jobs(self, *, user_id: int, limit: int = 8) -> list[WebJob]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [row_to_job(row) for row in rows]


class LoginSessionManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.active: dict | None = None
        self.starting: dict | None = None

    async def start(self, *, user: WebUser, platform: str) -> None:
        adapter_class = adapter_class_for(platform)
        adapter = adapter_class(profile_root=user_browser_profile_root(user.id))
        with self.lock:
            if self.active is not None or self.starting is not None:
                raise RuntimeError("已有同学正在远程浏览器中登录。请稍后再试。")
            self.starting = {"user_id": user.id, "platform": adapter.platform_name}
        async_playwright, playwright_error = load_async_playwright_for_web()
        playwright = None
        context = None
        started = False
        try:
            playwright = await async_playwright().start()
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(adapter.user_data_dir),
                headless=False,
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(adapter.url, wait_until="domcontentloaded", timeout=adapter.timeout_ms)
            with self.lock:
                self.active = {
                    "user_id": user.id,
                    "email": user.email,
                    "platform": adapter.platform_name,
                    "slug": adapter.slug,
                    "started_at": timestamp_now(),
                    "playwright": playwright,
                    "context": context,
                    "playwright_error": playwright_error,
                }
            started = True
        except playwright_error as exc:
            raise PlaywrightUnavailableError(format_playwright_error(exc)) from exc
        except Exception:
            raise
        finally:
            if not started:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        pass
            with self.lock:
                self.starting = None

    async def finish(self, *, user_id: int) -> None:
        with self.lock:
            if self.active is None:
                return
            if self.active["user_id"] != user_id:
                raise RuntimeError("当前远程登录会话不属于你，不能关闭。")
            active = self.active
            self.active = None
        context = active["context"]
        playwright = active["playwright"]
        try:
            await context.close()
        finally:
            await playwright.stop()

    def status_for(self, *, user_id: int) -> dict | None:
        with self.lock:
            if self.active is None:
                return None
            visible = dict(self.active)
            visible.pop("playwright", None)
            visible.pop("context", None)
            visible.pop("playwright_error", None)
            visible["owned_by_current_user"] = visible["user_id"] == user_id
            return visible


def create_app():
    store = WebStore()
    login_manager = LoginSessionManager()
    app = FastAPI(title="homework-watcher web")

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return page("输入错误", message_page("输入错误", str(exc)), status_code=400)

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError):
        return page("操作失败", message_page("操作失败", str(exc)), status_code=400)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = current_user(request, store)
        if user is None:
            return page("homework-watcher", public_home())
        return dashboard_page(user, store, login_manager)

    @app.post("/register")
    async def register(request: Request):
        data = await read_form(request)
        user = store.create_user(
            email=data.get("email", ""),
            password=data.get("password", ""),
            report_email=data.get("report_email", ""),
        )
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, user.id)
        return response

    @app.post("/login")
    async def login(request: Request):
        data = await read_form(request)
        user = store.authenticate(email=data.get("email", ""), password=data.get("password", ""))
        if user is None:
            return page("登录失败", message_page("登录失败", "邮箱或密码不正确。"), status_code=401)
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, user.id)
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.post("/settings/report-email")
    async def update_report_email(request: Request):
        user = require_user(request, store)
        data = await read_form(request)
        store.update_report_email(user_id=user.id, report_email=data.get("report_email", ""))
        return RedirectResponse("/", status_code=303)

    @app.post("/platform-login/{platform}")
    async def platform_login(request: Request, platform: str):
        user = require_user(request, store)
        await login_manager.start(user=user, platform=platform)
        return RedirectResponse("/remote-login", status_code=303)

    @app.get("/remote-login", response_class=HTMLResponse)
    async def remote_login(request: Request):
        user = require_user(request, store)
        return remote_login_page(user, login_manager)

    @app.post("/remote-login/finish")
    async def finish_remote_login(request: Request):
        user = require_user(request, store)
        await login_manager.finish(user_id=user.id)
        return RedirectResponse("/", status_code=303)

    @app.post("/assignments/{assignment_id}/done")
    async def mark_assignment_done(request: Request, assignment_id: int):
        user = require_user(request, store)
        db = HomeworkDB(user_homework_db_path(user.id))
        try:
            db.mark_done(assignment_id)
        finally:
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/scan")
    async def scan(request: Request):
        user = require_user(request, store)
        start_background_job(store, user=user, kind="scan", task=lambda progress: scan_user_homework(user, progress=progress))
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/send-report")
    async def send_report(request: Request):
        user = require_user(request, store)
        start_background_job(store, user=user, kind="send-report", task=lambda progress: send_user_report(user, progress=progress))
        return RedirectResponse("/", status_code=303)

    @app.post("/admin/run-daily")
    async def run_daily(request: Request):
        if not admin_authorized(request):
            return PlainTextResponse("unauthorized", status_code=401)
        started = 0
        for user in store.list_users():
            start_background_job(store, user=user, kind="daily", task=lambda progress, user=user: daily_user_run(user, progress=progress))
            started += 1
        return PlainTextResponse(f"started {started} daily jobs\n")

    return app


def public_home() -> str:
    return """
    <section class="auth-shell">
      <div class="brand-panel">
        <div class="brand-mark">HW</div>
        <p class="eyebrow">Homework Watcher</p>
        <h1>作业日报托管台</h1>
        <div class="boundary-list" aria-label="安全边界">
          <span>不保存平台密码</span>
          <span>手动完成验证码</span>
          <span>只发送日报</span>
        </div>
      </div>
      <div class="auth-forms">
        <form method="post" action="/login" class="form-panel">
          <h2>登录</h2>
          <label>邮箱<input name="email" type="email" autocomplete="email" required></label>
          <label>服务密码<input name="password" type="password" autocomplete="current-password" required></label>
          <button type="submit">登录</button>
        </form>
        <form method="post" action="/register" class="form-panel muted-panel">
          <h2>注册</h2>
          <label>邮箱<input name="email" type="email" autocomplete="email" required></label>
          <label>日报收件邮箱<input name="report_email" type="email" autocomplete="email"></label>
          <label>服务密码<input name="password" type="password" minlength="10" autocomplete="new-password" required></label>
          <button type="submit" class="secondary">创建账号</button>
        </form>
      </div>
    </section>
    """


def dashboard_page(user: WebUser, store: WebStore, login_manager: LoginSessionManager):
    db = HomeworkDB(user_homework_db_path(user.id))
    try:
        assignments = db.list_assignments(include_done=False)
    finally:
        db.close()
    jobs = store.recent_jobs(user_id=user.id)
    active_login = login_manager.status_for(user_id=user.id)
    now = now_local()
    total = len(assignments)
    overdue = sum(1 for item in assignments if item.due_at < now)
    today = sum(1 for item in assignments if item.due_at.date() == now.date() and item.due_at >= now)
    next_due = min(assignments, key=lambda item: item.due_at, default=None)
    body = [
        "<header class='app-header'>",
        "<div><p class='eyebrow'>Dashboard</p>",
        f"<h1>{escape(user.email)}</h1></div>",
        "<form method='post' action='/logout'><button class='ghost'>退出</button></form>",
        "</header>",
        "<section class='metric-grid'>",
        f"<div class='metric'><span>待办</span><strong>{total}</strong></div>",
        f"<div class='metric danger'><span>逾期</span><strong>{overdue}</strong></div>",
        f"<div class='metric warn'><span>今日截止</span><strong>{today}</strong></div>",
        f"<div class='metric'><span>最近截止</span><strong>{escape(next_due.due_at.strftime('%m-%d %H:%M') if next_due else '无')}</strong></div>",
        "</section>",
        "<section class='dashboard-grid'>",
        "<div class='workspace-main'>",
        "<section class='section-band'><div class='section-title'><h2>当前待办</h2><span class='count-pill'>"
        f"{total}</span></div>",
    ]
    if assignments:
        body.append(render_assignment_table(assignments[:20]))
    else:
        body.append("<div class='empty-state'>暂无待办</div>")
    body.extend(
        [
            "</section>",
            "<section class='section-band'><div class='section-title'><h2>最近任务</h2></div>",
        ]
    )
    if jobs:
        body.append("<div class='job-list'>")
        for job in jobs:
            body.append(render_job_row(job))
        body.append("</div>")
    else:
        body.append("<div class='empty-state'>暂无任务</div>")
    body.extend(
        [
            "</section></div>",
            "<aside class='workspace-side'>",
            "<section class='form-panel'><h2>日报邮箱</h2>",
            "<form method='post' action='/settings/report-email'>",
            f"<label>收件邮箱<input name='report_email' type='email' value='{escape(user.report_email)}' required></label>",
            "<button>保存</button></form></section>",
            "<section class='form-panel'><h2>平台登录态</h2>",
            "<div class='actions vertical'>",
            "<form method='post' action='/platform-login/changjiang-yuketang'><button type='submit'>长江雨课堂</button></form>",
            "<form method='post' action='/platform-login/xiaoya'><button type='submit' class='secondary'>小雅</button></form>",
            "</div>",
        ]
    )
    if active_login:
        body.append("<a class='inline-link' href='/remote-login'>查看当前远程登录会话</a>")
    body.extend(
        [
            "</section>",
            "<section class='form-panel accent-panel'><h2>运行</h2><div class='actions vertical'>",
            "<form method='post' action='/jobs/scan'><button type='submit'>立即扫描</button></form>",
            "<form method='post' action='/jobs/send-report'><button type='submit' class='secondary'>发送日报</button></form>",
            "</div></section>",
            "</aside></section>",
        ]
    )
    if any(job.status == "running" for job in jobs):
        body.append("<script>setTimeout(() => location.reload(), 2500);</script>")
    return page("Dashboard", "\n".join(body))


def remote_login_page(user: WebUser, login_manager: LoginSessionManager):
    active = login_manager.status_for(user_id=user.id)
    if active is None:
        return page("远程登录", message_page("没有远程登录会话", "请从首页选择一个平台开始登录。"))
    if not active["owned_by_current_user"]:
        return page("远程登录占用中", message_page("远程登录占用中", "已有其他同学正在登录，请稍后再试。"), status_code=409)
    novnc_url = os.environ.get("HW_WEB_NOVNC_URL", "").strip()
    link = (
        f"<p><a class='button' target='_blank' rel='noreferrer' href='{escape(novnc_url)}'>打开远程浏览器</a></p>"
        if novnc_url
        else "<p class='callout warn'>未配置 HW_WEB_NOVNC_URL。请部署 noVNC 后把公开访问地址写入该环境变量。</p>"
    )
    return page(
        "远程登录",
        f"""
        <section class="remote-shell">
          <div>
            <p class="eyebrow">Remote Login</p>
            <h1>正在登录：{escape(active['platform'])}</h1>
            <p class="lede">在远程浏览器里手动登录平台。程序只保存浏览器登录态，不读取平台密码。</p>
          </div>
          <div class="form-panel">
          <p><span class="field-label">开始时间</span>{escape(active['started_at'])}</p>
          {link}
          <p class="muted-text">登录完成后点击下面按钮关闭远程浏览器并保存登录态。</p>
          <form method="post" action="/remote-login/finish"><button>我已完成登录</button></form>
          </div>
        </section>
        """,
    )


def scan_user_homework(user: WebUser, *, progress: JobProgress | None = None) -> str:
    db = HomeworkDB(user_homework_db_path(user.id))
    created = 0
    seen = 0
    errors: list[str] = []
    slugs = canonical_slugs()
    emit_job_progress(progress, "准备扫描平台", 5)
    try:
        for index, slug in enumerate(slugs):
            adapter = ADAPTER_CLASSES[slug](profile_root=user_browser_profile_root(user.id))
            platform_start = 10 + int(index * 75 / max(1, len(slugs)))
            platform_end = 10 + int((index + 1) * 75 / max(1, len(slugs)))
            emit_job_progress(progress, f"开始扫描 {adapter.platform_name}", platform_start)
            try:
                items = adapter.fetch_assignments(
                    headless=True,
                    progress=platform_progress_adapter(progress, start=platform_start, end=platform_end),
                )
            except (LoginRequiredError, PageStructureChangedError, PlaywrightUnavailableError) as exc:
                errors.append(f"{adapter.platform_name}: {exc}")
                emit_job_progress(progress, f"{adapter.platform_name} 扫描失败：{exc}", platform_end)
                continue
            emit_job_progress(progress, f"{adapter.platform_name}：写入数据库 {len(items)} 条", platform_end)
            for item in items:
                assignment, was_created = db.add_assignment(
                    title=item.title,
                    course=item.course,
                    platform=item.platform,
                    due_at=item.due_at,
                    status=item.status,
                    url=item.url,
                )
                if assignment.id is not None and platform_status_is_done(item.status):
                    assignment = db.mark_done(assignment.id)
                seen += 1
                created += 1 if was_created else 0
            emit_job_progress(progress, f"{adapter.platform_name}：完成，识别 {len(items)} 条", platform_end)
        emit_job_progress(progress, "补齐本周固定作业", 90)
        materialize_recurring_assignments(db, now=now_local(), horizon_days=7)
    finally:
        db.close()
    message = f"扫描完成：识别 {seen} 条，新增 {created} 条"
    if errors:
        message += "；部分平台失败：" + "；".join(errors[:2])
    emit_job_progress(progress, message, 100)
    return message


def send_user_report(user: WebUser, *, progress: JobProgress | None = None) -> str:
    emit_job_progress(progress, "准备生成日报", 10)
    db = HomeworkDB(user_homework_db_path(user.id))
    try:
        now = now_local()
        materialize_recurring_assignments(db, now=now, horizon_days=days_until_end_of_week(now))
        assignments = db.list_assignments(include_done=False)
    finally:
        db.close()
    emit_job_progress(progress, "连接 SMTP 并发送日报", 70)
    config = email_config_for_recipient(user.report_email)
    subject = send_email_report(assignments, config=config, now=now)
    emit_job_progress(progress, f"已发送：{subject}", 100)
    return f"已发送：{subject}"


def daily_user_run(user: WebUser, *, progress: JobProgress | None = None) -> str:
    scan_message = scan_user_homework(user, progress=lambda message, percent=None: emit_job_progress(progress, message, scale_progress(percent, 0, 70)))
    report_message = send_user_report(user, progress=lambda message, percent=None: emit_job_progress(progress, message, scale_progress(percent, 70, 100)))
    return f"{scan_message}；{report_message}"


def start_background_job(store: WebStore, *, user: WebUser, kind: str, task: Callable[[JobProgress], str]) -> WebJob:
    job = store.create_job(user_id=user.id, kind=kind)

    def runner() -> None:
        def report_progress(message: str, percent: int | None = None) -> None:
            store.update_job_progress(job_id=job.id, message=message, progress=percent)

        try:
            report_progress("正在运行", 5)
            message = task(report_progress)
        except Exception as exc:
            store.finish_job(job_id=job.id, status="failed", message=str(exc))
        else:
            store.finish_job(job_id=job.id, status="success", message=message)

    threading.Thread(target=runner, daemon=True).start()
    return job


def emit_job_progress(progress: JobProgress | None, message: str, percent: int | None = None) -> None:
    if progress is not None:
        progress(message, percent)


def platform_progress_adapter(progress: JobProgress | None, *, start: int, end: int):
    def report(message: str) -> None:
        percent = start + 2
        match = re.search(r"扫描课程\s+(\d+)/(\d+)", message)
        if match:
            current, total = int(match.group(1)), max(1, int(match.group(2)))
            percent = start + int((end - start) * min(current, total) / total)
        elif "完成" in message:
            percent = end
        emit_job_progress(progress, message, min(end, max(start, percent)))

    return report


def scale_progress(percent: int | None, start: int, end: int) -> int | None:
    if percent is None:
        return None
    return start + int((end - start) * max(0, min(100, int(percent))) / 100)


async def read_form(request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def current_user(request, store: WebStore) -> WebUser | None:
    value = request.cookies.get(SESSION_COOKIE, "")
    user_id = verify_session_cookie(value)
    if user_id is None:
        return None
    try:
        return store.get_user(user_id)
    except KeyError:
        return None


def require_user(request, store: WebStore) -> WebUser:
    user = current_user(request, store)
    if user is None:
        raise RuntimeError("请先登录。")
    return user


def set_session_cookie(response, user_id: int) -> None:
    expires = int(time.time()) + SESSION_DAYS * 24 * 60 * 60
    payload = f"{user_id}:{expires}"
    signature = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    response.set_cookie(
        SESSION_COOKIE,
        f"{payload}:{signature}",
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=truthy(os.environ.get("HW_WEB_SECURE_COOKIES", "0")),
        samesite="lax",
    )


def verify_session_cookie(value: str) -> int | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None
    user_id_raw, expires_raw, signature = parts
    payload = f"{user_id_raw}:{expires_raw}"
    expected = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        expires = int(expires_raw)
        user_id = int(user_id_raw)
    except ValueError:
        return None
    if expires < int(time.time()):
        return None
    return user_id


def session_secret() -> bytes:
    configured = os.environ.get("HW_WEB_SECRET_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")
    secret_path = WEB_DIR / "secret.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        secret_path.chmod(0o600)
    return secret_path.read_text(encoding="utf-8").strip().encode("utf-8")


def admin_authorized(request) -> bool:
    token = os.environ.get("HW_WEB_ADMIN_TOKEN", "").strip()
    if not token:
        return False
    provided = request.headers.get("x-admin-token", "") or request.query_params.get("token", "")
    return hmac.compare_digest(provided, token)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_raw, salt, digest = stored.split("$", 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(candidate, digest)


def email_config_for_recipient(recipient: str) -> EmailConfig:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "").strip() or "587")
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("EMAIL_FROM", username).strip()
    use_ssl = truthy(os.environ.get("SMTP_SSL", "0"))
    starttls = truthy(os.environ.get("SMTP_STARTTLS", "1")) and not use_ssl
    missing = [name for name, value in {
        "SMTP_HOST": host,
        "SMTP_USERNAME": username,
        "SMTP_PASSWORD": password,
        "EMAIL_FROM": sender,
    }.items() if not value]
    if missing:
        raise ValueError("缺少服务端邮件配置：" + ", ".join(missing))
    validate_email(recipient)
    return EmailConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=[recipient],
        use_ssl=use_ssl,
        starttls=starttls,
    )


def adapter_class_for(platform: str):
    try:
        return ADAPTER_CLASSES[platform]
    except KeyError as exc:
        raise ValueError(f"未知平台：{platform}") from exc


def load_playwright_for_web():
    from .platforms.base import load_playwright

    return load_playwright()


def load_async_playwright_for_web():
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PlaywrightUnavailableError(
            "未安装 Playwright。请运行：python3 -m pip install -e . && python3 -m playwright install chromium"
        ) from exc
    return async_playwright, PlaywrightError


def user_homework_db_path(user_id: int) -> Path:
    return user_root(user_id) / "homework.db"


def user_browser_profile_root(user_id: int) -> Path:
    path = user_root(user_id) / "browser-profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_root(user_id: int) -> Path:
    path = WEB_DIR / "users" / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_email(value: str) -> str:
    return value.strip().lower()


def validate_email(value: str) -> None:
    parsed_name, parsed_email = parseaddr(value)
    if parsed_name or parsed_email != value or "@" not in value:
        raise ValueError("邮箱格式不正确。")


def timestamp_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_to_user(row) -> WebUser:
    return WebUser(
        id=row["id"],
        email=row["email"],
        report_email=row["report_email"],
        created_at=row["created_at"],
    )


def row_to_job(row) -> WebJob:
    return WebJob(
        id=row["id"],
        user_id=row["user_id"],
        kind=row["kind"],
        status=row["status"],
        message=row["message"],
        progress=row["progress"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def days_until_end_of_week(now) -> int:
    days_until_sunday = 6 - now.weekday()
    end_of_sunday = now.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=days_until_sunday)
    return max(0, (end_of_sunday - now).days + 1)


def render_assignment_table(assignments) -> str:
    now = now_local()
    rows = [
        "<div class='table-wrap'><table>",
        "<thead><tr><th>课程</th><th>作业</th><th>平台</th><th>截止</th><th>状态</th><th>操作</th></tr></thead>",
        "<tbody>",
    ]
    for item in assignments:
        due_class = assignment_due_class(item, now)
        status_label = "已完成" if assignment_is_done(item) else item.status or "未提交"
        rows.append(
            "<tr>"
            f"<td class='table-course'>{escape(item.course or '未填写')}</td>"
            f"<td><strong>{escape(item.title)}</strong></td>"
            f"<td>{escape(item.platform or '未填写')}</td>"
            f"<td><span class='status-badge {due_class}'>{escape(item.due_at.strftime('%Y-%m-%d %H:%M'))}</span></td>"
            f"<td>{escape(status_label)}</td>"
            f"<td>{render_assignment_action(item)}</td>"
            "</tr>"
        )
    rows.append("</tbody></table></div>")
    return "".join(rows)


def assignment_due_class(item, now) -> str:
    if item.due_at < now:
        return "overdue"
    if item.due_at.date() == now.date():
        return "today"
    if item.due_at <= now + timedelta(days=3):
        return "soon"
    return "normal"


def render_assignment_action(item) -> str:
    if item.id is None or not is_recurring_assignment(item):
        return "<span class='muted-text'>-</span>"
    return (
        f"<form method='post' action='/assignments/{item.id}/done' class='inline-form'>"
        "<label class='check-control'>"
        "<input type='checkbox' onchange='this.form.submit()'>"
        "<span>完成</span>"
        "</label>"
        "</form>"
    )


def render_job_row(job: WebJob) -> str:
    progress = max(0, min(100, job.progress))
    progress_markup = ""
    if job.status == "running":
        progress_markup = (
            "<div class='progress-line'>"
            f"<div class='progress-track'><span style='width: {progress}%'></span></div>"
            f"<span class='progress-value'>{progress}%</span>"
            "</div>"
        )
    return (
        "<div class='job-row'>"
        f"<span class='status-badge {job_status_class(job.status)}'>{escape(job_status_label(job.status))}</span>"
        f"<strong>{escape(job_kind_label(job.kind))}</strong>"
        f"<time>{escape(job.updated_at)}</time>"
        f"<p>{escape(job.message or '处理中')}</p>"
        f"{progress_markup}"
        "</div>"
    )


def job_kind_label(kind: str) -> str:
    return {
        "scan": "平台扫描",
        "send-report": "发送日报",
        "daily": "每日运行",
    }.get(kind, kind)


def job_status_label(status: str) -> str:
    return {
        "running": "运行中",
        "success": "成功",
        "failed": "失败",
    }.get(status, status)


def job_status_class(status: str) -> str:
    return {
        "running": "running",
        "success": "success",
        "failed": "failed",
    }.get(status, "normal")


def message_page(title: str, message: str) -> str:
    return f"<section class='panel'><h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href='/'>返回</a></p></section>"


def page(title: str, body: str, *, status_code: int = 200):
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f4f5f1;
      --surface: #ffffff;
      --surface-soft: #fafaf7;
      --ink: #20231f;
      --muted: #686f67;
      --line: #d9dbd3;
      --primary: #245b45;
      --primary-dark: #183f31;
      --blue: #2e5d7e;
      --amber: #9a5a00;
      --red: #a03a2f;
      --green-soft: #edf6f1;
      --amber-soft: #fff5df;
      --red-soft: #fff0ee;
      --shadow: 0 18px 40px rgba(34, 39, 32, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html {{ color-scheme: light; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        linear-gradient(180deg, rgba(36, 91, 69, 0.06), transparent 260px),
        var(--bg);
      color: var(--ink);
    }}
    body::before {{
      content: "";
      display: block;
      height: 5px;
      background: linear-gradient(90deg, var(--primary), var(--blue), var(--amber));
    }}
    main {{ width: min(1180px, calc(100vw - 32px)); margin: 30px auto 48px; }}
    h1, h2, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 12px; font-size: clamp(30px, 4vw, 56px); line-height: 1.05; letter-spacing: 0; }}
    h2 {{ margin-bottom: 14px; font-size: 18px; line-height: 1.25; letter-spacing: 0; }}
    p {{ line-height: 1.6; color: var(--muted); }}
    a {{ color: var(--blue); }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      border: 1px solid var(--primary);
      border-radius: 7px;
      background: var(--primary);
      color: #fff;
      padding: 10px 14px;
      font: inherit;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      transition: background 0.16s ease, border-color 0.16s ease, transform 0.16s ease;
    }}
    button:hover, .button:hover {{ background: var(--primary-dark); border-color: var(--primary-dark); transform: translateY(-1px); }}
    button.secondary, .button.secondary {{ background: #fff; color: var(--primary); border-color: #b7c8bf; }}
    button.secondary:hover, .button.secondary:hover {{ background: var(--green-soft); color: var(--primary-dark); }}
    button.ghost {{ background: transparent; color: var(--ink); border-color: var(--line); }}
    button.ghost:hover {{ background: #fff; border-color: #bcc0b7; }}
    label {{ display: block; margin: 12px 0; color: var(--ink); font-weight: 700; }}
    input {{
      width: 100%;
      margin-top: 7px;
      padding: 11px 12px;
      border: 1px solid #c9ccc3;
      border-radius: 7px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    input:focus {{ outline: 3px solid rgba(36, 91, 69, 0.16); border-color: var(--primary); }}
    .auth-shell {{ display: grid; grid-template-columns: minmax(0, 1.12fr) minmax(340px, 0.88fr); gap: 24px; align-items: start; }}
    .brand-panel {{ padding: 26px 8px 0 0; }}
    .brand-mark {{
      width: 54px;
      height: 54px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: var(--primary);
      color: #fff;
      font-size: 17px;
      font-weight: 800;
      letter-spacing: 0;
      box-shadow: var(--shadow);
    }}
    .brand-panel h1 {{ max-width: 680px; }}
    .eyebrow {{
      margin: 18px 0 10px;
      color: var(--primary);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .lede {{ max-width: 680px; font-size: 17px; }}
    .boundary-list {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }}
    .boundary-list span {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      border: 1px solid #cfd8cf;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.72);
      color: var(--primary-dark);
      padding: 7px 12px;
      font-size: 13px;
      font-weight: 700;
    }}
    .auth-forms, .workspace-main, .workspace-side {{ display: grid; gap: 16px; }}
    .form-panel, .panel, .section-band, .metric {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.75) inset;
    }}
    .muted-panel {{ background: var(--surface-soft); }}
    .form-panel h2, .panel h1 {{ margin-bottom: 14px; }}
    .app-header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }}
    .app-header h1 {{ margin: 0; font-size: clamp(24px, 3vw, 38px); overflow-wrap: anywhere; }}
    .app-header .eyebrow {{ margin-top: 0; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .metric {{ min-height: 104px; }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; font-weight: 700; }}
    .metric strong {{ display: block; margin-top: 14px; font-size: 31px; line-height: 1; overflow-wrap: anywhere; }}
    .metric.warn {{ border-color: #ead39b; background: var(--amber-soft); }}
    .metric.danger {{ border-color: #e8bbb5; background: var(--red-soft); }}
    .dashboard-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 18px; align-items: start; }}
    .section-title {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }}
    .section-title h2 {{ margin: 0; }}
    .count-pill {{
      display: inline-flex;
      min-width: 32px;
      min-height: 26px;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      background: var(--green-soft);
      color: var(--primary);
      padding: 4px 10px;
      font-weight: 800;
    }}
    .empty-state {{
      display: grid;
      min-height: 120px;
      place-items: center;
      border: 1px dashed #c7cbc0;
      border-radius: 8px;
      background: var(--surface-soft);
      color: var(--muted);
      font-weight: 700;
    }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; min-width: 720px; border-collapse: collapse; }}
    th, td {{ padding: 13px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 13px; font-weight: 800; }}
    tr:last-child td {{ border-bottom: 0; }}
    td strong {{ display: block; line-height: 1.35; }}
    .table-course {{ width: 20%; font-weight: 700; }}
    .inline-form {{ margin: 0; }}
    .check-control {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 30px;
      margin: 0;
      color: var(--primary);
      font-size: 13px;
      font-weight: 800;
      cursor: pointer;
      white-space: nowrap;
    }}
    .check-control input {{
      width: 16px;
      height: 16px;
      margin: 0;
      accent-color: var(--primary);
      cursor: pointer;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 26px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--muted);
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .status-badge.success {{ border-color: #b7d8c8; background: var(--green-soft); color: #1f6a50; }}
    .status-badge.failed, .status-badge.overdue {{ border-color: #e5b7b1; background: var(--red-soft); color: var(--red); }}
    .status-badge.running, .status-badge.today, .status-badge.soon {{ border-color: #e5c989; background: var(--amber-soft); color: var(--amber); }}
    .job-list {{ border-top: 1px solid var(--line); }}
    .job-row {{ display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 13px 0; border-bottom: 1px solid var(--line); }}
    .job-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .job-row p {{ grid-column: 2 / -1; margin: 0; color: var(--muted); overflow-wrap: anywhere; }}
    .job-row time {{ color: var(--muted); font-size: 13px; white-space: nowrap; }}
    .progress-line {{ grid-column: 2 / -1; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; }}
    .progress-track {{ height: 8px; overflow: hidden; border-radius: 999px; background: #e7e9e1; }}
    .progress-track span {{ display: block; height: 100%; border-radius: inherit; background: var(--primary); }}
    .progress-value {{ color: var(--muted); font-size: 12px; font-weight: 800; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .actions.vertical {{ display: grid; gap: 10px; }}
    .actions.vertical form, .actions.vertical button {{ width: 100%; }}
    .inline-link {{ display: inline-flex; margin-top: 12px; font-weight: 700; }}
    .accent-panel {{ border-color: #c6d6ce; background: linear-gradient(180deg, #ffffff, var(--green-soft)); }}
    .remote-shell {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px); gap: 22px; align-items: start; }}
    .remote-shell h1 {{ font-size: clamp(30px, 4vw, 48px); }}
    .field-label {{ display: block; color: var(--muted); font-size: 13px; font-weight: 800; }}
    .callout {{ border-radius: 8px; padding: 12px; background: var(--amber-soft); border: 1px solid #e5c989; }}
    .warn {{ color: var(--amber); }}
    .muted-text {{ color: var(--muted); }}
    @media (max-width: 900px) {{
      main {{ width: min(100vw - 24px, 720px); margin-top: 22px; }}
      .auth-shell, .dashboard-grid, .metric-grid, .remote-shell {{ grid-template-columns: 1fr; }}
      .brand-panel {{ padding-top: 4px; }}
      .app-header {{ align-items: stretch; }}
      .app-header form {{ flex: 0 0 auto; }}
      .job-row {{ grid-template-columns: 1fr; }}
      .job-row p {{ grid-column: 1; }}
      .job-row time {{ white-space: normal; }}
    }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""
    return HTMLResponse(html, status_code=status_code)


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "homework_watcher.web_app:app",
        host=os.environ.get("HW_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("HW_WEB_PORT", "8080")),
        reload=truthy(os.environ.get("HW_WEB_RELOAD", "0")),
    )
