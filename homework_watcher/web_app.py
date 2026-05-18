from __future__ import annotations

import hashlib
import hmac
import os
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
from .email_report import EmailConfig, build_email_report, build_email_subject, send_email_report, truthy
from .platforms import ADAPTER_CLASSES, canonical_slugs
from .platforms.base import LoginRequiredError, PageStructureChangedError, PlaywrightUnavailableError
from .recurring_assignments import materialize_recurring_assignments


WEB_DIR = Path(os.environ.get("HW_WEB_DIR", APP_DIR / "web")).expanduser()
WEB_DB_PATH = Path(os.environ.get("HW_WEB_DB_PATH", WEB_DIR / "web.db")).expanduser()
SESSION_COOKIE = "homework_watcher_session"
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 260_000


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
    created_at: str
    updated_at: str


class WebStore:
    def __init__(self, path: Path | str = WEB_DB_PATH):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            self.conn.commit()

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
                INSERT INTO jobs (user_id, kind, status, message, created_at, updated_at)
                VALUES (?, ?, 'running', '', ?, ?)
                """,
                (user_id, kind, timestamp, timestamp),
            )
            self.conn.commit()
            return self.get_job(cursor.lastrowid)

    def finish_job(self, *, job_id: int, status: str, message: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET status = ?, message = ?, updated_at = ? WHERE id = ?",
                (status, message, timestamp_now(), job_id),
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

    def start(self, *, user: WebUser, platform: str) -> None:
        with self.lock:
            if self.active is not None:
                raise RuntimeError("已有同学正在远程浏览器中登录。请稍后再试。")
            adapter_class = adapter_class_for(platform)
            adapter = adapter_class(profile_root=user_browser_profile_root(user.id))
            sync_playwright, playwright_error = load_playwright_for_web()
            playwright = sync_playwright().start()
            try:
                context = adapter._launch_context(playwright, headless=False)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(adapter.url, wait_until="domcontentloaded", timeout=adapter.timeout_ms)
            except Exception:
                if "context" in locals():
                    try:
                        context.close()
                    except Exception:
                        pass
                try:
                    playwright.stop()
                finally:
                    pass
                raise
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

    def finish(self, *, user_id: int) -> None:
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
            context.close()
        finally:
            playwright.stop()

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
        login_manager.start(user=user, platform=platform)
        return RedirectResponse("/remote-login", status_code=303)

    @app.get("/remote-login", response_class=HTMLResponse)
    async def remote_login(request: Request):
        user = require_user(request, store)
        return remote_login_page(user, login_manager)

    @app.post("/remote-login/finish")
    async def finish_remote_login(request: Request):
        user = require_user(request, store)
        login_manager.finish(user_id=user.id)
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/scan")
    async def scan(request: Request):
        user = require_user(request, store)
        start_background_job(store, user=user, kind="scan", task=lambda: scan_user_homework(user))
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/send-report")
    async def send_report(request: Request):
        user = require_user(request, store)
        start_background_job(store, user=user, kind="send-report", task=lambda: send_user_report(user))
        return RedirectResponse("/", status_code=303)

    @app.post("/admin/run-daily")
    async def run_daily(request: Request):
        if not admin_authorized(request):
            return PlainTextResponse("unauthorized", status_code=401)
        started = 0
        for user in store.list_users():
            start_background_job(store, user=user, kind="daily", task=lambda user=user: daily_user_run(user))
            started += 1
        return PlainTextResponse(f"started {started} daily jobs\n")

    return app


def public_home() -> str:
    return """
    <section class="grid">
      <div>
        <h1>homework-watcher</h1>
        <p>集中托管作业日报服务。平台登录必须由用户在远程浏览器中手动完成；系统不保存平台密码，不自动提交作业，不绕过验证码。</p>
      </div>
      <form method="post" action="/login" class="panel">
        <h2>登录</h2>
        <label>邮箱<input name="email" type="email" required></label>
        <label>服务密码<input name="password" type="password" required></label>
        <button type="submit">登录</button>
      </form>
      <form method="post" action="/register" class="panel">
        <h2>注册</h2>
        <label>邮箱<input name="email" type="email" required></label>
        <label>日报收件邮箱<input name="report_email" type="email"></label>
        <label>服务密码<input name="password" type="password" minlength="10" required></label>
        <button type="submit">创建账号</button>
      </form>
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
    body = [
        f"<h1>{escape(user.email)}</h1>",
        "<div class='toolbar'><form method='post' action='/logout'><button>退出</button></form></div>",
        "<section class='panel'><h2>日报邮箱</h2>",
        "<form method='post' action='/settings/report-email'>",
        f"<label>收件邮箱<input name='report_email' type='email' value='{escape(user.report_email)}' required></label>",
        "<button>保存</button></form></section>",
        "<section class='panel'><h2>平台登录态</h2>",
        "<p>点击后会打开服务端远程浏览器。请只在远程浏览器里手动登录平台，不要把密码交给本系统。</p>",
        "<div class='actions'>",
        "<form method='post' action='/platform-login/changjiang-yuketang'><button>登录长江雨课堂</button></form>",
        "<form method='post' action='/platform-login/xiaoya'><button>登录小雅</button></form>",
        "</div>",
    ]
    if active_login:
        body.append("<p><a href='/remote-login'>查看当前远程登录会话</a></p>")
    body.extend(
        [
            "</section>",
            "<section class='panel'><h2>运行</h2><div class='actions'>",
            "<form method='post' action='/jobs/scan'><button>立即扫描</button></form>",
            "<form method='post' action='/jobs/send-report'><button>发送日报</button></form>",
            "</div></section>",
            "<section class='panel'><h2>当前待办</h2>",
        ]
    )
    if assignments:
        body.append("<ol>")
        for item in assignments[:20]:
            body.append(
                "<li>"
                f"{escape(item.course or '未填写')} | {escape(item.title)} | "
                f"{escape(item.platform or '未填写')} | {escape(item.due_at.strftime('%Y-%m-%d %H:%M'))}"
                "</li>"
            )
        body.append("</ol>")
    else:
        body.append("<p>暂无待办。</p>")
    body.append("</section><section class='panel'><h2>最近任务</h2>")
    if jobs:
        body.append("<ol>")
        for job in jobs:
            body.append(f"<li>{escape(job.kind)} | {escape(job.status)} | {escape(job.message)}</li>")
        body.append("</ol>")
    else:
        body.append("<p>暂无任务。</p>")
    body.append("</section>")
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
        else "<p class='warn'>未配置 HW_WEB_NOVNC_URL。请部署 noVNC 后把公开访问地址写入该环境变量。</p>"
    )
    return page(
        "远程登录",
        f"""
        <section class="panel">
          <h1>正在登录：{escape(active['platform'])}</h1>
          <p>开始时间：{escape(active['started_at'])}</p>
          {link}
          <p>登录完成后点击下面按钮关闭远程浏览器并保存登录态。</p>
          <form method="post" action="/remote-login/finish"><button>我已完成登录</button></form>
        </section>
        """,
    )


def scan_user_homework(user: WebUser) -> str:
    db = HomeworkDB(user_homework_db_path(user.id))
    created = 0
    seen = 0
    errors: list[str] = []
    try:
        for slug in canonical_slugs():
            adapter = ADAPTER_CLASSES[slug](profile_root=user_browser_profile_root(user.id))
            try:
                items = adapter.fetch_assignments(headless=True)
            except (LoginRequiredError, PageStructureChangedError, PlaywrightUnavailableError) as exc:
                errors.append(f"{adapter.platform_name}: {exc}")
                continue
            for item in items:
                _, was_created = db.add_assignment(
                    title=item.title,
                    course=item.course,
                    platform=item.platform,
                    due_at=item.due_at,
                    status=item.status,
                    url=item.url,
                )
                seen += 1
                created += 1 if was_created else 0
        materialize_recurring_assignments(db, now=now_local(), horizon_days=7)
    finally:
        db.close()
    message = f"扫描完成：识别 {seen} 条，新增 {created} 条"
    if errors:
        message += "；部分平台失败：" + "；".join(errors[:2])
    return message


def send_user_report(user: WebUser) -> str:
    db = HomeworkDB(user_homework_db_path(user.id))
    try:
        now = now_local()
        materialize_recurring_assignments(db, now=now, horizon_days=days_until_end_of_week(now))
        assignments = db.list_assignments(include_done=False)
    finally:
        db.close()
    config = email_config_for_recipient(user.report_email)
    subject = send_email_report(assignments, config=config, now=now)
    return f"已发送：{subject}"


def daily_user_run(user: WebUser) -> str:
    scan_message = scan_user_homework(user)
    report_message = send_user_report(user)
    return f"{scan_message}；{report_message}"


def start_background_job(store: WebStore, *, user: WebUser, kind: str, task: Callable[[], str]) -> WebJob:
    job = store.create_job(user_id=user.id, kind=kind)

    def runner() -> None:
        try:
            message = task()
        except Exception as exc:
            store.finish_job(job_id=job.id, status="failed", message=str(exc))
        else:
            store.finish_job(job_id=job.id, status="success", message=message)

    threading.Thread(target=runner, daemon=True).start()
    return job


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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def days_until_end_of_week(now) -> int:
    days_until_sunday = 6 - now.weekday()
    end_of_sunday = now.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=days_until_sunday)
    return max(0, (end_of_sunday - now).days + 1)


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
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f7f4; color: #171717; }}
    main {{ width: min(1080px, calc(100vw - 32px)); margin: 32px auto; }}
    h1, h2 {{ margin: 0 0 16px; }}
    p {{ line-height: 1.55; }}
    a {{ color: #0f5f8f; }}
    .grid {{ display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 16px; align-items: start; }}
    .panel {{ background: #fff; border: 1px solid #deded8; border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
    label {{ display: block; margin: 12px 0; font-weight: 600; }}
    input {{ box-sizing: border-box; width: 100%; margin-top: 6px; padding: 10px; border: 1px solid #c9c9c2; border-radius: 6px; font: inherit; }}
    button, .button {{ display: inline-block; border: 0; border-radius: 6px; background: #1f6f52; color: #fff; padding: 10px 14px; font: inherit; text-decoration: none; cursor: pointer; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .toolbar {{ display: flex; justify-content: flex-end; }}
    .warn {{ color: #8a4b00; }}
    @media (max-width: 860px) {{ .grid {{ grid-template-columns: 1fr; }} }}
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
