from __future__ import annotations

import threading
from datetime import datetime
from html import escape
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import (
    AUTH_COOKIE_NAME,
    AuthError,
    CurrentUser,
    authenticate_user,
    create_session_token,
    create_user,
    get_user,
    parse_urlencoded_form,
    read_session_username,
    user_to_current,
)
from .database import (
    add_manual_assignment,
    assignment_to_dict,
    create_session_factory,
    init_db,
    is_manual_assignment_dict,
    list_assignments,
    list_todos,
    set_manual_assignment_completed,
)
from .git_utils import git_commit
from .logging_utils import read_latest_scan_log
from .remote_login import RemoteLoginManager, build_novnc_url
from .scan_errors import format_scan_failure
from .scan_progress import ScanCancelled, ScanProgressStore
from .scan_service import latest_scan_result
from .settings import load_settings
from .web_scan import ServerScanCommandError, run_server_scan_command


LOGIN_PATH = "/login"


def create_app() -> FastAPI:
    settings = load_settings()
    init_db(settings)
    session_factory = create_session_factory(settings)
    login_manager = RemoteLoginManager()
    scan_progress = ScanProgressStore()
    app = FastAPI(title="homework-watcher-v2")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "version": settings.app_version,
            "git_commit": git_commit(),
            "database_path": str(settings.database_path),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        with session_factory() as session:
            todos = [assignment_to_dict(item) for item in list_todos(session, owner_key=user.username)]
        login_status = login_manager.status(user.username)
        latest_result = scan_progress.get(user.username).result or latest_scan_result(user.username)
        return HTMLResponse(
            render_page(
                "当前待办",
                f"""
                <section class="panel">
                  <div class="panel-title">
                    <h2>当前待办</h2>
                    <span class="count">{len(todos)}</span>
                  </div>
                  <p class="muted page-note">这里只显示未完成作业；已完成记录可在“查看所有记录”中确认。先用“扫描课程”保存小雅课程列表，再用“扫描任务”读取已登录平台的作业列表。</p>
                  {render_scan_error_panel(request.query_params.get("scan_error", ""))}
                  {render_scan_guide()}
                  {render_assignment_table(todos)}
                  {render_manual_assignment_panel(request.query_params.get("manual_error", ""))}
                  <div class="actions">
                    <form method="post" action="/scan?redirect=1" class="scan-form" data-scan-start-url="/api/scan/start">
                      <button type="submit" class="scan-action-button">扫描任务</button>
                    </form>
                    <form method="post" action="/courses/scan?redirect=1" class="scan-form" data-scan-start-url="/api/courses/scan/start">
                      <button type="submit" class="secondary scan-action-button">扫描课程</button>
                    </form>
                    <a class="button-link" href="/logs/latest">查看最近扫描日志</a>
                    <a class="button-link" href="/assignments">查看所有记录</a>
                  </div>
                  {render_scan_progress_panel()}
                </section>
                {render_scan_summary(latest_result)}
                {render_platform_login_panel(login_status)}
                """,
                settings=settings,
                user=user,
            )
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is not None:
            return RedirectResponse("/", status_code=303)
        error = request.query_params.get("error", "")
        return HTMLResponse(render_page("登录", render_auth_panel(error), settings=settings))

    @app.post("/login")
    async def login(request: Request):
        fields = parse_urlencoded_form(await request.body())
        with session_factory() as session:
            user = authenticate_user(
                session,
                username=fields.get("username", ""),
                password=fields.get("password", ""),
            )
        if user is None:
            return redirect_to_login("学号或密码不正确。")
        response = RedirectResponse("/", status_code=303)
        set_auth_cookie(response, user.username, settings)
        return response

    @app.post("/register")
    async def register(request: Request):
        fields = parse_urlencoded_form(await request.body())
        try:
            with session_factory() as session:
                user = create_user(
                    session,
                    username=fields.get("username", ""),
                    password=fields.get("password", ""),
                    display_name=fields.get("display_name", ""),
                )
        except AuthError as exc:
            return redirect_to_login(str(exc))
        response = RedirectResponse("/", status_code=303)
        set_auth_cookie(response, user.username, settings)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is not None:
            await login_manager.finish(user.username)
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        response.delete_cookie(AUTH_COOKIE_NAME)
        return response

    @app.post("/manual-assignments")
    async def create_manual_assignment(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        fields = parse_urlencoded_form(await request.body())
        try:
            due_at = parse_manual_due_at(fields.get("due_at", ""))
            with session_factory() as session:
                add_manual_assignment(
                    session,
                    owner_key=user.username,
                    title=fields.get("title", ""),
                    due_at=due_at,
                    completed=fields.get("completed") == "1",
                    recurrence=fields.get("recurrence", "none"),
                )
        except ValueError as exc:
            return RedirectResponse(f"/?{urlencode({'manual_error': str(exc)})}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/manual-assignments/{assignment_id}/completion")
    async def update_manual_assignment_completion(request: Request, assignment_id: int):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        fields = parse_urlencoded_form(await request.body())
        with session_factory() as session:
            set_manual_assignment_completed(
                session,
                owner_key=user.username,
                assignment_id=assignment_id,
                completed=fields.get("completed") == "1",
            )
        return RedirectResponse("/", status_code=303)

    @app.post("/scan")
    def scan(request: Request, redirect: bool = Query(False)):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return RedirectResponse(LOGIN_PATH, status_code=303) if redirect else login_required_json()
        try:
            result = run_server_scan_command(settings, owner_key=user.username, mode="tasks")
        except ServerScanCommandError as exc:
            message = format_scan_failure(exc.result, fallback=str(exc))
            if redirect:
                return RedirectResponse(f"/?{urlencode({'scan_error': message})}", status_code=303)
            return JSONResponse({"error": message, "result": exc.result}, status_code=500)
        if redirect:
            return RedirectResponse("/", status_code=303)
        return result

    @app.post("/courses/scan")
    def scan_courses(request: Request, redirect: bool = Query(False)):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return RedirectResponse(LOGIN_PATH, status_code=303) if redirect else login_required_json()
        try:
            result = run_server_scan_command(settings, owner_key=user.username, mode="courses")
        except ServerScanCommandError as exc:
            message = format_scan_failure(exc.result, fallback=str(exc))
            if redirect:
                return RedirectResponse(f"/?{urlencode({'scan_error': message})}", status_code=303)
            return JSONResponse({"error": message, "result": exc.result}, status_code=500)
        if redirect:
            return RedirectResponse("/", status_code=303)
        return result

    @app.post("/api/scan/start")
    def api_scan_start(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        return start_background_scan(user.username, mode="tasks").to_dict()

    @app.post("/api/courses/scan/start")
    def api_courses_scan_start(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        return start_background_scan(user.username, mode="courses").to_dict()

    def start_background_scan(owner_key: str, *, mode: str):
        snapshot, started = scan_progress.start(owner_key)
        if not started:
            return snapshot

        def run_scan_job(active_owner_key: str, scan_id: str, active_mode: str) -> None:
            try:
                def emit_progress(percent: int, message: str) -> None:
                    scan_progress.raise_if_cancelled(active_owner_key, scan_id)
                    scan_progress.update(active_owner_key, scan_id, percent, message)

                result = run_server_scan_command(
                    settings,
                    owner_key=active_owner_key,
                    mode=active_mode,
                    emit=emit_progress,
                    check_cancelled=lambda: scan_progress.raise_if_cancelled(active_owner_key, scan_id),
                )
                scan_progress.raise_if_cancelled(active_owner_key, scan_id)
                scan_progress.finish_success(active_owner_key, scan_id, result)
            except ScanCancelled:
                scan_progress.finish_cancelled(active_owner_key, scan_id)
            except ServerScanCommandError as exc:
                scan_progress.finish_failed(
                    active_owner_key,
                    scan_id,
                    format_scan_failure(exc.result, fallback=str(exc)),
                )
            except Exception as exc:  # noqa: BLE001 - keep the web progress endpoint alive.
                scan_progress.finish_failed(
                    active_owner_key,
                    scan_id,
                    format_scan_failure(None, fallback=f"{type(exc).__name__}: {exc}"),
                )

        thread = threading.Thread(
            target=run_scan_job,
            args=(owner_key, snapshot.scan_id, mode),
            name=f"{mode}-scan-{owner_key}",
            daemon=True,
        )
        thread.start()
        return scan_progress.get(owner_key)

    @app.post("/api/scan/cancel")
    def api_scan_cancel(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        snapshot, _cancelled = scan_progress.cancel(user.username)
        return snapshot.to_dict()

    @app.get("/api/scan/progress")
    def api_scan_progress(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        return scan_progress.get(user.username).to_dict()

    @app.get("/api/todos")
    def api_todos(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        with session_factory() as session:
            return [assignment_to_dict(item) for item in list_todos(session, owner_key=user.username)]

    @app.get("/api/assignments")
    def api_assignments(
        request: Request,
        platform: str | None = None,
        course: str | None = None,
        status: str | None = None,
    ):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        with session_factory() as session:
            items = [assignment_to_dict(item) for item in list_assignments(session, owner_key=user.username)]
        if platform:
            items = [item for item in items if item["platform"] == platform]
        if course:
            items = [item for item in items if item["course"] == course]
        if status:
            items = [
                item
                for item in items
                if item["status_raw"] == status or item["status_normalized"] == status
            ]
        return items

    @app.get("/api/scans/latest")
    def api_latest_scan(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        progress = scan_progress.get(user.username)
        if progress.result:
            return progress.result
        result = latest_scan_result(user.username)
        return result.to_dict() if result else {"scan": None}

    @app.get("/assignments", response_class=HTMLResponse)
    def assignments_page(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        with session_factory() as session:
            items = [assignment_to_dict(item) for item in list_assignments(session, owner_key=user.username)]
        return HTMLResponse(
            render_page(
                "所有记录",
                f"""
                <section class="panel">
                  <div class="panel-title">
                    <h2>所有记录</h2>
                    <span class="count">{len(items)}</span>
                  </div>
                  {render_assignment_table(items, allow_manual_completion=False)}
                  <div class="actions"><a class="button-link" href="/">返回待办</a></div>
                </section>
                """,
                settings=settings,
                user=user,
            )
        )

    @app.get("/logs/latest", response_class=HTMLResponse)
    def latest_logs_page(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        lines = read_latest_scan_log(settings, owner_key=user.username)
        content = "\n".join(escape(line) for line in lines) or "暂无扫描日志。"
        return HTMLResponse(
            render_page(
                "最近扫描日志",
                f"""
                <section class="panel">
                  <h2>最近扫描日志</h2>
                  <pre>{content}</pre>
                  {render_scan_progress_panel()}
                  <div class="actions"><a class="button-link" href="/">返回待办</a></div>
                </section>
                """,
                settings=settings,
                user=user,
            )
        )

    @app.post("/login/changjiang-yuketang")
    async def start_yuketang_login(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        response = await start_platform_login(
            login_manager.start_yuketang,
            settings,
            user=user,
            failure_title="长江雨课堂登录启动失败",
        )
        if response is not None:
            return response
        return RedirectResponse("/remote-login", status_code=303)

    @app.post("/login/xiaoya")
    async def start_xiaoya_login(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        response = await start_platform_login(
            login_manager.start_xiaoya,
            settings,
            user=user,
            failure_title="小雅登录启动失败",
        )
        if response is not None:
            return response
        return RedirectResponse("/remote-login", status_code=303)

    @app.get("/remote-login", response_class=HTMLResponse)
    async def remote_login(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        await login_manager.close_expired(user.username)
        status = login_manager.status(user.username)
        if status is None:
            return HTMLResponse(
                render_page(
                    "远程登录",
                    render_message_panel("没有远程登录会话", "请从首页打开长江雨课堂登录。", back_href="/"),
                    settings=settings,
                    user=user,
                )
            )
        return HTMLResponse(
            render_page(
                "远程登录",
                render_remote_login_panel(status, build_novnc_url(str(request.base_url))),
                settings=settings,
                user=user,
            )
        )

    @app.post("/remote-login/finish")
    async def finish_remote_login(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        await login_manager.finish(user.username)
        return RedirectResponse("/", status_code=303)

    @app.post("/remote-login/cancel")
    async def cancel_remote_login(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return redirect_to_login()
        await login_manager.finish(user.username)
        return RedirectResponse("/", status_code=303)

    return app


def current_user_from_request(request: Request, session_factory, settings) -> CurrentUser | None:
    username = read_session_username(request.cookies.get(AUTH_COOKIE_NAME), settings.session_secret)
    if username is None:
        return None
    with session_factory() as session:
        user = get_user(session, username)
        if user is None:
            return None
        return user_to_current(user)


def set_auth_cookie(response, username: str, settings) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_token(username, settings.session_secret),
        httponly=True,
        samesite="lax",
        max_age=14 * 24 * 60 * 60,
    )


def redirect_to_login(error: str = "") -> RedirectResponse:
    target = LOGIN_PATH
    if error:
        from urllib.parse import urlencode

        target = f"{LOGIN_PATH}?{urlencode({'error': error})}"
    return RedirectResponse(target, status_code=303)


def login_required_json() -> JSONResponse:
    return JSONResponse({"error": "login_required"}, status_code=401)


async def start_platform_login(starter, settings, *, user: CurrentUser, failure_title: str):
    try:
        await starter(
            settings,
            user_key=user.username,
            user_label=user.display_name,
        )
    except Exception as exc:  # noqa: BLE001 - return the operator-facing failure.
        return HTMLResponse(
            render_page(
                failure_title,
                render_message_panel(
                    failure_title,
                    f"{type(exc).__name__}: {exc}",
                    back_href="/",
                ),
                settings=settings,
                user=user,
            ),
            status_code=500,
        )
    return None


def render_auth_panel(error: str = "") -> str:
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return f"""
    <section class="panel auth-panel">
      <div>
        <h2>登录</h2>
        <p class="muted page-note">请使用学号登录本站；平台登录态需要在进入系统后单独授权。</p>
        {error_html}
        <form method="post" action="/login" class="auth-form">
          <label>学号<input name="username" autocomplete="username" inputmode="numeric" required></label>
          <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
          <button type="submit">登录</button>
        </form>
      </div>
      <div>
        <h2>注册</h2>
        <p class="muted page-note">显示名只用于页面展示，可以继续填写姓名或昵称。</p>
        <form method="post" action="/register" class="auth-form">
          <label>学号<input name="username" autocomplete="username" inputmode="numeric" required></label>
          <label>显示名<input name="display_name" autocomplete="name"></label>
          <label>密码<input name="password" type="password" autocomplete="new-password" minlength="8" required></label>
          <button type="submit" class="secondary">创建账号</button>
        </form>
      </div>
    </section>
    """


def render_platform_login_panel(status) -> str:
    if status is not None:
        return f"""
        <section class="panel login-panel">
          <div class="panel-title">
	            <h2>平台登录</h2>
	            <span class="count">开</span>
	          </div>
	          <p class="muted">{escape(status.platform)} 远程登录窗口已打开，剩余 {escape(str(status.expires_in_seconds // 60))} 分钟。</p>
	          <div class="actions">
	            <a class="button-link" href="/remote-login">继续登录</a>
	          </div>
	        </section>
        """
    return """
    <section class="panel login-panel">
      <div class="panel-title">
        <h2>平台登录</h2>
        <span class="count">2</span>
      </div>
      <p class="muted">打开服务器上的平台浏览器窗口，通过 noVNC 手动登录并保存浏览器登录态。</p>
      <div class="actions">
        <form method="post" action="/login/changjiang-yuketang">
          <button type="submit">长江雨课堂登录</button>
        </form>
        <form method="post" action="/login/xiaoya">
          <button type="submit" class="secondary">小雅登录</button>
        </form>
      </div>
    </section>
    """


def render_remote_login_panel(status, novnc_url: str) -> str:
    remaining_minutes = max(1, (status.expires_in_seconds + 59) // 60)
    return f"""
    <section class="panel remote-panel">
      <div>
        <h2>正在登录：{escape(status.platform)}</h2>
        <p class="muted">在远程浏览器中完成登录。程序只保存浏览器登录态，不读取平台密码。</p>
      </div>
      <div class="remote-actions">
        <p><span class="label">开始时间</span>{escape(status.started_at)}</p>
        <p><span class="label">账号</span>{escape(status.user_label)}</p>
        <p><span class="label">自动释放</span>{escape(str(remaining_minutes))} 分钟</p>
        <div class="actions">
          <a class="button-link primary-link" target="_blank" rel="noreferrer" href="{escape(novnc_url)}">打开远程浏览器</a>
          <form method="post" action="/remote-login/finish"><button type="submit">我已完成登录</button></form>
          <form method="post" action="/remote-login/cancel"><button type="submit" class="secondary">放弃本次登录</button></form>
        </div>
      </div>
    </section>
    """


def render_message_panel(title: str, message: str, *, back_href: str) -> str:
    return f"""
    <section class="panel">
      <h2>{escape(title)}</h2>
      <p class="muted">{escape(message)}</p>
      <div class="actions"><a class="button-link" href="{escape(back_href)}">返回</a></div>
    </section>
    """


def render_scan_error_panel(message: str = "") -> str:
    if not message:
        return ""
    return f"""
    <div class="scan-error" role="alert">
      <strong>上次扫描失败</strong>
      <p>{escape(message)}</p>
    </div>
    """


def render_scan_progress_panel() -> str:
    return """
    <div class="scan-progress" id="scan-progress" data-start-url="/api/scan/start" data-progress-url="/api/scan/progress" data-cancel-url="/api/scan/cancel">
      <div class="progress-header">
        <span id="scan-progress-status">等待扫描</span>
        <strong id="scan-progress-percent">0%</strong>
      </div>
      <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="progress-fill" id="scan-progress-fill"></div>
      </div>
      <p class="muted progress-message" id="scan-progress-message">尚未开始扫描。</p>
      <div class="progress-actions">
        <button type="button" class="secondary" id="scan-cancel-button" disabled>强制结束</button>
      </div>
    </div>
    """


def render_scan_guide() -> str:
    return """
    <div class="scan-guide" aria-label="扫描步骤指南">
      <div class="guide-step">
        <span class="guide-index">1</span>
        <div><strong>登录本站</strong><span>用学号进入系统。</span></div>
      </div>
      <div class="guide-step">
        <span class="guide-index">2</span>
        <div><strong>授权平台</strong><span>在“平台登录”中分别打开长江雨课堂和小雅。移动端进入这两个平台时通常只能扫码登录，请用手机扫码完成授权。</span></div>
      </div>
      <div class="guide-step">
        <span class="guide-index">3</span>
        <div><strong>开始扫描</strong><span>课程有变化时先点“扫描课程”；平时点“扫描任务”，等待进度条完成。</span></div>
      </div>
      <div class="guide-step">
        <span class="guide-index">4</span>
        <div><strong>查看待办</strong><span>只保留未完成作业；已完成记录在“查看所有记录”中。</span></div>
      </div>
    </div>
    """


def render_manual_assignment_panel(error: str = "") -> str:
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return f"""
    <div class="manual-add">
      <h3>手动添加作业</h3>
      {error_html}
      <form method="post" action="/manual-assignments" class="manual-form">
        <label>作业名<input name="title" maxlength="300" required></label>
        <label>截止时间<input name="due_at" type="datetime-local" required></label>
        <label>重复周期
          <select name="recurrence">
            <option value="none">不重复</option>
            <option value="daily">每天</option>
            <option value="weekly">每周</option>
            <option value="monthly">每月</option>
          </select>
        </label>
        <label class="check-row"><input name="completed" type="checkbox" value="1"><span>已完成</span></label>
        <button type="submit">添加作业</button>
      </form>
    </div>
    """


def render_scan_summary(result) -> str:
    if result is None:
        return ""
    summary = platform_summaries_from_scan_result(result).get("xiaoya", {})
    if not summary:
        return ""
    status_message = str(summary.get("message") or "").strip()
    status_note = f'<p class="muted summary-note">{escape(status_message)}</p>' if status_message else ""
    fields = [
        ("已保存课程", "cached_courses_count"),
        ("本次发现课程", "discovered_courses_count"),
        ("待扫描课程", "merged_courses_count"),
        ("已扫描课程", "scanned_courses_count"),
        ("失败课程", "failed_courses_count"),
        ("解析作业", "parsed_assignments_count"),
        ("当前待办", "todo_count"),
    ]
    cells = "".join(
        f'<div><span class="label">{escape(label)}</span><strong>{escape(str(summary.get(key, 0)))}</strong></div>'
        for label, key in fields
    )
    return f"""
    <section class="panel summary-panel">
      <div class="panel-title">
        <h2>小雅最近扫描摘要</h2>
        <span class="count">{escape(str(summary_count(summary)))}</span>
      </div>
      {status_note}
      <div class="summary-grid">{cells}</div>
    </section>
    """


def summary_count(summary: dict[str, object]) -> object:
    return (
        summary.get("saved_courses_count")
        or summary.get("cached_courses_count")
        or summary.get("merged_courses_count")
        or 0
    )


def platform_summaries_from_scan_result(result) -> dict[str, dict[str, object]]:
    if isinstance(result, dict):
        value = result.get("platform_summaries") or {}
    else:
        value = getattr(result, "platform_summaries", {}) or {}
    return value if isinstance(value, dict) else {}


def render_assignment_table(
    assignments: list[dict[str, object]],
    *,
    allow_manual_completion: bool = True,
) -> str:
    if not assignments:
        return '<p class="muted">当前没有待办。扫描服务接入后会在这里显示作业。</p>'

    rows = []
    for item in assignments:
        url = str(item.get("url") or "")
        title = escape(str(item.get("title") or ""))
        link = f'<a href="{escape(url)}" target="_blank" rel="noreferrer">{title}</a>' if url else title
        status = (
            render_manual_completion_control(item)
            if allow_manual_completion and is_manual_assignment_dict(item)
            else escape(str(item.get("status_raw") or ""))
        )
        rows.append(
            "<tr>"
            f"<td data-label=\"平台\">{escape(str(item.get('platform') or ''))}</td>"
            f"<td data-label=\"课程\">{escape(str(item.get('course') or ''))}</td>"
            f"<td data-label=\"标题\">{link}</td>"
            f"<td data-label=\"状态\">{status}</td>"
            f"<td data-label=\"截止时间\">{escape(str(item.get('due_at') or ''))}</td>"
            f"<td data-label=\"距今时间\">{escape(format_due_distance(item.get('due_at')))}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>平台</th><th>课程</th><th>标题</th><th>状态</th><th>截止时间</th><th>距今时间</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_manual_completion_control(item: dict[str, object]) -> str:
    assignment_id = str(item.get("id") or "")
    completed = str(item.get("status_normalized") or "") == "completed"
    checked = " checked" if completed else ""
    label = "已完成" if completed else "未完成"
    return f"""
    <form method="post" action="/manual-assignments/{escape(assignment_id)}/completion" class="inline-form">
      <input type="hidden" name="completed" value="0">
      <label class="check-action">
        <input type="checkbox" name="completed" value="1"{checked} onchange="this.form.submit()">
        <span>{label}</span>
      </label>
    </form>
    """


def parse_manual_due_at(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError("请选择截止时间")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("截止时间格式不正确")


def format_due_distance(value: object, *, now: datetime | None = None) -> str:
    if not value:
        return ""
    try:
        due_at = datetime.fromisoformat(str(value))
    except ValueError:
        return ""
    current = now or datetime.now()
    if due_at.tzinfo is not None and current.tzinfo is None:
        current = current.astimezone(due_at.tzinfo)
    seconds = int((due_at - current).total_seconds())
    if abs(seconds) < 60:
        return "现在"
    label = "还有" if seconds >= 0 else "已过"
    seconds = abs(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours and len(parts) < 2:
        parts.append(f"{hours}小时")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}分钟")
    return label + "".join(parts or ["1分钟"])


def render_page(title: str, body: str, *, settings, user: CurrentUser | None = None) -> str:
    account_html = ""
    if user is not None:
        account_html = f"""
        <div class="account">
          <span>{escape(user.display_name)}</span>
          <form method="post" action="/logout"><button type="submit" class="secondary compact">退出</button></form>
        </div>
        """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{escape(title)} - NKU作业提醒系统</title>
  <style>
    :root {{
      color-scheme: light;
      --nankai-purple-rgb: 126, 12, 110;
      --nankai-purple-hex: #711a5f;
      --primary: var(--nankai-purple-hex);
      --primary-strong: rgb(var(--nankai-purple-rgb));
      --primary-soft: #f3e4f1;
      --primary-ring: rgba(var(--nankai-purple-rgb), 0.24);
      --accent: #b79042;
      --bg: #f8f5fa;
      --surface: #ffffff;
      --surface-subtle: #fbf9fc;
      --text: #211b25;
      --muted: #6d6172;
      --border: #e4d9e8;
      --border-strong: #d4c2da;
      --danger: #b42318;
      --shadow: 0 16px 42px rgba(49, 31, 58, 0.09);
      --shadow-soft: 0 8px 22px rgba(49, 31, 58, 0.07);
      --on-primary: #ffffff;
    }}
    html {{ min-height: 100%; background: var(--bg); }}
    body {{
      min-height: 100%;
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    main {{ max-width: 1160px; margin: 0 auto; padding: 48px 24px; }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: center;
      margin-bottom: 28px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--border);
    }}
    h1 {{ font-size: 34px; margin: 0; letter-spacing: 0; color: var(--text); }}
    h2 {{ margin: 0; font-size: 24px; color: var(--text); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border-strong);
      background: var(--primary-soft);
      color: var(--primary);
      border-radius: 8px;
      padding: 2px 10px;
      font-weight: 800;
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--surface) 60%, transparent);
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 24px;
      box-shadow: var(--shadow);
      overflow-x: auto;
    }}
    .panel + .panel {{ margin-top: 18px; }}
    .panel-title {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    .page-note {{ max-width: 760px; margin: 10px 0 18px; }}
    .scan-guide {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0 18px;
    }}
    .guide-step {{
      display: grid;
      grid-template-columns: 30px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-subtle);
      padding: 12px;
    }}
    .guide-index {{
      display: inline-flex;
      width: 28px;
      height: 28px;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      background: var(--primary);
      color: var(--on-primary);
      font-weight: 800;
    }}
    .guide-step strong {{ display: block; color: var(--text); }}
    .guide-step span:not(.guide-index) {{ display: block; margin-top: 2px; color: var(--muted); font-size: 14px; }}
    .count {{
      display: inline-flex;
      min-width: 30px;
      height: 30px;
      align-items: center;
      justify-content: center;
      background: var(--primary-soft);
      color: var(--primary);
      border: 1px solid var(--border-strong);
      border-radius: 999px;
      font-weight: 800;
    }}
    .muted {{ color: var(--muted); }}
    table {{ width: 100%; min-width: 760px; border-collapse: collapse; margin-top: 8px; }}
    th, td {{ text-align: left; padding: 13px 10px; border-top: 1px solid var(--border); vertical-align: top; }}
    th {{ color: var(--muted); font-size: 13px; font-weight: 800; }}
    tbody tr:hover {{ background: var(--surface-subtle); }}
    a {{ color: var(--primary); text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    a:hover {{ color: var(--primary-strong); }}
    button, .button-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      border: 1px solid var(--primary);
      background: var(--primary);
      color: var(--on-primary);
      border-radius: 8px;
      padding: 0 16px;
      font: inherit;
      font-weight: 800;
      text-decoration: none;
      cursor: pointer;
      box-shadow: var(--shadow-soft);
      transition: background 140ms ease, border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
    }}
    button:hover, .button-link:hover {{ border-color: var(--primary-strong); background: var(--primary-strong); color: var(--on-primary); transform: translateY(-1px); }}
    button.secondary, .button-link {{
      background: var(--surface);
      color: var(--primary);
      box-shadow: none;
    }}
    button.secondary:hover, .button-link:hover {{ background: var(--primary-soft); color: var(--primary-strong); }}
    button.compact {{ min-height: 32px; padding: 0 10px; border-radius: 7px; }}
    .primary-link {{ background: var(--primary); color: var(--on-primary); box-shadow: var(--shadow-soft); }}
    .primary-link:hover {{ border-color: var(--primary-strong); background: var(--primary-strong); color: var(--on-primary); }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 20px; }}
    .account {{ display: flex; align-items: center; gap: 12px; color: var(--muted); }}
    .auth-panel {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; }}
    .auth-form {{ display: grid; gap: 14px; }}
    label {{ display: grid; gap: 6px; color: var(--muted); font-weight: 800; }}
    input, select {{
      min-height: 42px;
      border: 1px solid var(--border-strong);
      background: var(--surface-subtle);
      color: var(--text);
      border-radius: 8px;
      padding: 0 11px;
      font: inherit;
      accent-color: var(--primary);
    }}
    select {{ appearance: auto; }}
    input[type="checkbox"] {{ min-height: auto; width: 18px; height: 18px; padding: 0; }}
    input:focus, button:focus-visible, .button-link:focus-visible {{
      outline: 3px solid var(--primary-ring);
      outline-offset: 2px;
    }}
    .manual-add {{
      margin-top: 18px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-subtle);
      padding: 16px;
    }}
    h3 {{ margin: 0 0 12px; font-size: 18px; }}
    .manual-form {{
      display: grid;
      grid-template-columns: minmax(180px, 1.2fr) minmax(180px, 0.9fr) minmax(120px, 0.7fr) auto auto;
      gap: 12px;
      align-items: end;
    }}
    .check-row, .check-action {{
      display: inline-flex;
      flex-direction: row;
      gap: 8px;
      align-items: center;
      min-height: 42px;
      color: var(--text);
      font-weight: 800;
    }}
    .inline-form {{ margin: 0; }}
    .error {{ color: var(--danger); font-weight: 800; }}
    .scan-error {{
      margin: 14px 0 16px;
      border: 1px solid color-mix(in srgb, var(--danger) 42%, var(--border));
      border-radius: 8px;
      background: color-mix(in srgb, var(--danger) 8%, var(--surface));
      color: var(--text);
      padding: 12px 14px;
    }}
    .scan-error strong {{ display: block; color: var(--danger); margin-bottom: 4px; }}
    .scan-error p {{ margin: 0; white-space: pre-line; overflow-wrap: anywhere; }}
    .login-panel, .summary-panel {{ margin-top: 18px; }}
    .summary-note {{ margin: 8px 0 0; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 14px; margin-top: 14px; }}
    .summary-grid div {{ border: 1px solid var(--border); background: var(--surface-subtle); border-radius: 8px; padding: 12px; }}
    .summary-grid strong {{ display: block; margin-top: 4px; font-size: 24px; color: var(--primary); }}
    .scan-progress {{ margin-top: 20px; border-top: 1px solid var(--border); padding-top: 16px; }}
    .progress-header {{ display: flex; justify-content: space-between; align-items: baseline; gap: 16px; font-weight: 800; }}
    .progress-track {{
      height: 12px;
      margin-top: 10px;
      overflow: hidden;
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: 999px;
    }}
    .progress-fill {{ width: 0%; height: 100%; background: var(--primary); transition: width 180ms ease; }}
    .progress-message {{ min-height: 22px; margin: 10px 0 0; white-space: pre-line; overflow-wrap: anywhere; }}
    .progress-actions {{ display: flex; justify-content: flex-end; margin-top: 10px; }}
    button[disabled] {{ opacity: 0.64; cursor: wait; transform: none; }}
    .remote-panel {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 420px); gap: 24px; align-items: start; }}
    .remote-actions p {{ margin: 0 0 10px; color: var(--text); }}
    .label {{ display: inline-block; min-width: 72px; color: var(--muted); font-weight: 800; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      max-height: 560px;
      overflow: auto;
    }}
    footer {{ margin-top: 32px; color: var(--muted); font-size: 14px; }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        color-scheme: dark;
        --primary: #d86fc8;
        --primary-strong: #f0a8e3;
        --primary-soft: #351a33;
        --primary-ring: rgba(216, 111, 200, 0.34);
        --accent: #d7bb75;
        --bg: #141018;
        --surface: #201725;
        --surface-subtle: #2a2030;
        --text: #f7eff8;
        --muted: #c6b8c9;
        --border: #3c2f42;
        --border-strong: #5a4660;
        --danger: #ff9b8e;
        --shadow: 0 18px 42px rgba(0, 0, 0, 0.34);
        --shadow-soft: 0 10px 24px rgba(0, 0, 0, 0.28);
        --on-primary: #170d15;
      }}
    }}
    @media (max-width: 720px) {{
      main {{ padding: 28px 16px; }}
      .remote-panel, .auth-panel {{ grid-template-columns: 1fr; }}
      header {{ align-items: flex-start; flex-direction: column; }}
      h1 {{ font-size: 30px; }}
      .panel {{ padding: 18px; }}
      .panel-title {{ align-items: flex-start; }}
      .page-note {{ margin-bottom: 16px; }}
      .scan-guide {{ grid-template-columns: 1fr; }}
      .manual-form {{ grid-template-columns: 1fr; }}
      .manual-form button {{ width: 100%; }}
      .actions {{ flex-direction: column; }}
      .actions > *, .actions form, .actions button, .actions .button-link {{ width: 100%; }}
      .actions button, .actions .button-link {{ box-sizing: border-box; }}
      .auth-form button {{ width: 100%; }}
      .progress-actions {{ justify-content: stretch; }}
      .progress-actions button {{ width: 100%; }}
      .account {{ flex-wrap: wrap; }}
      .account button.compact {{ width: auto; }}
      table {{ min-width: 0; }}
      thead {{ display: none; }}
      tbody, tr, td {{ display: block; }}
      tbody tr {{
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 10px 12px;
        background: var(--surface-subtle);
      }}
      tbody tr + tr {{ margin-top: 12px; }}
      td {{
        display: grid;
        grid-template-columns: 82px minmax(0, 1fr);
        gap: 10px;
        border-top: 0;
        padding: 8px 0;
      }}
      td::before {{
        content: attr(data-label);
        color: var(--muted);
        font-weight: 800;
      }}
      td a {{ overflow-wrap: anywhere; }}
      pre {{ max-height: 480px; font-size: 13px; }}
      footer {{ overflow-wrap: anywhere; }}
    }}
	  </style>
</head>
<body>
  <main>
	    <header>
	      <h1>NKU作业提醒系统 <span class="badge">{escape(settings.app_version)}</span></h1>
	      <div class="account">
	        <span>{escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</span>
	        {account_html}
	      </div>
	    </header>
    {body}
    <footer>commit {escape(git_commit())} · database {escape(str(settings.database_path))}</footer>
  </main>
  {render_page_script()}
</body>
</html>"""


def render_page_script() -> str:
    return """
  <script>
    (() => {
      const forms = Array.from(document.querySelectorAll("form[data-scan-start-url]"));
      const panel = document.getElementById("scan-progress");
      if (!panel) return;

      const buttons = forms.flatMap((form) => Array.from(form.querySelectorAll("button")));
      const cancelButton = document.getElementById("scan-cancel-button");
      const fill = document.getElementById("scan-progress-fill");
      const percentText = document.getElementById("scan-progress-percent");
      const statusText = document.getElementById("scan-progress-status");
      const messageText = document.getElementById("scan-progress-message");
      const track = panel.querySelector(".progress-track");
      let pollTimer = null;
      let reloadTimer = null;
      let sawRunningScan = false;

      buttons.forEach((button) => {
        button.dataset.originalText = button.textContent;
      });

      const setScanButtons = (running) => {
        buttons.forEach((button) => {
          button.disabled = running;
          button.textContent = running ? "扫描中" : (button.dataset.originalText || "扫描");
        });
      };

      const setProgress = (snapshot) => {
        const percent = Math.max(0, Math.min(100, Number(snapshot.percent || 0)));
        fill.style.width = `${percent}%`;
        percentText.textContent = `${percent}%`;
        track.setAttribute("aria-valuenow", String(percent));
        messageText.textContent = snapshot.error || snapshot.message || "";
        const labels = {
          idle: "等待扫描",
          running: "正在扫描",
          succeeded: "扫描完成",
          failed: "扫描失败",
          cancelled: "已强制结束"
        };
        statusText.textContent = labels[snapshot.status] || "扫描状态";
        setScanButtons(snapshot.status === "running");
        if (cancelButton) {
          cancelButton.disabled = snapshot.status !== "running";
          cancelButton.textContent = "强制结束";
        }
      };

      const stopPolling = () => {
        if (pollTimer) window.clearInterval(pollTimer);
        pollTimer = null;
      };

      const pollProgress = async () => {
        try {
          const response = await fetch(panel.dataset.progressUrl, { credentials: "same-origin" });
          if (response.status === 401) {
            window.location.href = "/login";
            return;
          }
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const snapshot = await response.json();
          setProgress(snapshot);
          if (snapshot.status === "running") {
            sawRunningScan = true;
            if (!pollTimer) pollTimer = window.setInterval(pollProgress, 1000);
            return;
          }
          stopPolling();
          if (snapshot.status === "succeeded" && sawRunningScan && !reloadTimer) {
            messageText.textContent = "扫描完成，正在刷新待办。";
            sawRunningScan = false;
            reloadTimer = window.setTimeout(() => window.location.reload(), 900);
          }
        } catch (error) {
          if (sawRunningScan) {
            statusText.textContent = "正在扫描";
            messageText.textContent = "进度暂时不可用，继续尝试。";
            if (!pollTimer) pollTimer = window.setInterval(pollProgress, 1000);
            return;
          }
          stopPolling();
          statusText.textContent = "扫描状态不可用";
          messageText.textContent = "无法读取扫描进度。";
          setScanButtons(false);
        }
      };

      const startPolling = () => {
        stopPolling();
        pollProgress();
        pollTimer = window.setInterval(pollProgress, 1000);
      };

      if (forms.length) {
        forms.forEach((form) => {
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          setScanButtons(true);
          sawRunningScan = true;
          try {
            const response = await fetch(form.dataset.scanStartUrl, {
              method: "POST",
              credentials: "same-origin"
            });
            if (response.status === 401) {
              window.location.href = "/login";
              return;
            }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            setProgress(await response.json());
            startPolling();
          } catch (error) {
            sawRunningScan = false;
            statusText.textContent = "启动失败";
            messageText.textContent = "无法启动扫描。";
            setScanButtons(false);
          }
        });
        });
      }

      if (cancelButton) {
        cancelButton.addEventListener("click", async () => {
          cancelButton.disabled = true;
          cancelButton.textContent = "结束中";
          try {
            const response = await fetch(panel.dataset.cancelUrl, {
              method: "POST",
              credentials: "same-origin"
            });
            if (response.status === 401) {
              window.location.href = "/login";
              return;
            }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            sawRunningScan = false;
            setProgress(await response.json());
            stopPolling();
          } catch (error) {
            cancelButton.disabled = false;
            cancelButton.textContent = "强制结束";
            messageText.textContent = "无法强制结束扫描。";
          }
        });
      }

      pollProgress();
    })();
  </script>
    """


app = create_app()


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "homework_watcher.app:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
