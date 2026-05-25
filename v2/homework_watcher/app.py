from __future__ import annotations

import threading
from datetime import datetime
from html import escape

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
from .database import assignment_to_dict, create_session_factory, init_db, list_assignments, list_todos
from .git_utils import git_commit
from .logging_utils import read_latest_scan_log
from .remote_login import RemoteLoginManager, build_novnc_url
from .scan_progress import ScanProgressStore
from .scan_service import ScanService, latest_scan_result
from .settings import load_settings


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
        latest_result = latest_scan_result(user.username)
        return HTMLResponse(
            render_page(
                "当前待办",
                f"""
                <section class="panel">
                  <div class="panel-title">
                    <h2>当前待办</h2>
                    <span class="count">{len(todos)}</span>
                  </div>
                  {render_assignment_table(todos)}
                  <div class="actions">
                    <form method="post" action="/scan?redirect=1" id="scan-form">
                      <button type="submit" id="scan-button">立即扫描</button>
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
            return redirect_to_login("用户名或密码不正确。")
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

    @app.post("/scan")
    def scan(request: Request, redirect: bool = Query(False)):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return RedirectResponse(LOGIN_PATH, status_code=303) if redirect else login_required_json()
        result = ScanService(settings, user_key=user.username).run_scan()
        if redirect:
            return RedirectResponse("/", status_code=303)
        return result.to_dict()

    @app.post("/api/scan/start")
    def api_scan_start(request: Request):
        user = current_user_from_request(request, session_factory, settings)
        if user is None:
            return login_required_json()
        snapshot, started = scan_progress.start(user.username)
        if not started:
            return snapshot.to_dict()

        def run_scan_job(owner_key: str, scan_id: str) -> None:
            try:
                def emit_progress(percent: int, message: str) -> None:
                    scan_progress.update(owner_key, scan_id, percent, message)

                result = ScanService(settings, user_key=owner_key).run_scan(progress=emit_progress)
                scan_progress.finish_success(owner_key, scan_id, result.to_dict())
            except Exception as exc:  # noqa: BLE001 - keep the web progress endpoint alive.
                scan_progress.finish_failed(owner_key, scan_id, f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(
            target=run_scan_job,
            args=(user.username, snapshot.scan_id),
            name=f"scan-{user.username}",
            daemon=True,
        )
        thread.start()
        return scan_progress.get(user.username).to_dict()

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
                  {render_assignment_table(items)}
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
        {error_html}
        <form method="post" action="/login" class="auth-form">
          <label>用户名<input name="username" autocomplete="username" required></label>
          <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
          <button type="submit">登录</button>
        </form>
      </div>
      <div>
        <h2>注册</h2>
        <form method="post" action="/register" class="auth-form">
          <label>用户名<input name="username" autocomplete="username" required></label>
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


def render_scan_progress_panel() -> str:
    return """
    <div class="scan-progress" id="scan-progress" data-start-url="/api/scan/start" data-progress-url="/api/scan/progress">
      <div class="progress-header">
        <span id="scan-progress-status">等待扫描</span>
        <strong id="scan-progress-percent">0%</strong>
      </div>
      <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="progress-fill" id="scan-progress-fill"></div>
      </div>
      <p class="muted progress-message" id="scan-progress-message">尚未开始扫描。</p>
    </div>
    """


def render_scan_summary(result) -> str:
    if result is None:
        return ""
    summary = result.platform_summaries.get("xiaoya", {})
    if not summary:
        return ""
    fields = [
        ("已配置课程", "known_courses_count"),
        ("自动发现课程", "discovered_courses_count"),
        ("合并后课程", "merged_courses_count"),
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
        <span class="count">{escape(str(summary.get("merged_courses_count", 0)))}</span>
      </div>
      <div class="summary-grid">{cells}</div>
    </section>
    """


def render_assignment_table(assignments: list[dict[str, object]]) -> str:
    if not assignments:
        return '<p class="muted">当前没有待办。扫描服务接入后会在这里显示作业。</p>'

    rows = []
    for item in assignments:
        url = str(item.get("url") or "")
        title = escape(str(item.get("title") or ""))
        link = f'<a href="{escape(url)}" target="_blank" rel="noreferrer">{title}</a>' if url else title
        rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('platform') or ''))}</td>"
            f"<td>{escape(str(item.get('course') or ''))}</td>"
            f"<td>{link}</td>"
            f"<td>{escape(str(item.get('status_raw') or ''))}</td>"
            f"<td>{escape(str(item.get('due_at') or ''))}</td>"
            f"<td>{escape(str(item.get('last_seen_at') or ''))}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>平台</th><th>课程</th><th>标题</th><th>状态</th><th>截止时间</th><th>最后发现</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


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
  <title>{escape(title)} - homework-watcher v2</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f2; color: #1f2924; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 48px 24px; }}
	    header {{ display: flex; justify-content: space-between; gap: 24px; align-items: center; margin-bottom: 28px; }}
	    h1 {{ font-size: 34px; margin: 0; letter-spacing: 0; }}
	    h2 {{ margin-top: 0; }}
    .badge {{ border: 1px solid #9ab5a8; color: #1f6b4b; border-radius: 6px; padding: 2px 8px; font-weight: 700; }}
    .panel {{ background: #fff; border: 1px solid #d8ded6; border-radius: 8px; padding: 24px; }}
    .panel-title {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    .count {{ display: inline-flex; min-width: 28px; height: 28px; align-items: center; justify-content: center; background: #e7f2ec; color: #1f6b4b; border-radius: 999px; font-weight: 700; }}
    .muted {{ color: #66736d; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 12px 10px; border-top: 1px solid #e2e6df; vertical-align: top; }}
    th {{ color: #66736d; font-size: 14px; }}
    a {{ color: #145f45; }}
	    button, .button-link {{ display: inline-flex; align-items: center; min-height: 42px; border: 1px solid #1f6b4b; background: #1f6b4b; color: #fff; border-radius: 6px; padding: 0 16px; font: inherit; font-weight: 700; text-decoration: none; cursor: pointer; }}
	    button.secondary {{ background: #fff; color: #1f6b4b; }}
	    button.compact {{ min-height: 32px; padding: 0 10px; }}
	    .button-link {{ background: #fff; color: #1f6b4b; }}
	    .primary-link {{ background: #1f6b4b; color: #fff; }}
	    .actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 20px; }}
	    .account {{ display: flex; align-items: center; gap: 12px; color: #66736d; }}
	    .auth-panel {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; }}
	    .auth-form {{ display: grid; gap: 14px; }}
	    label {{ display: grid; gap: 6px; color: #66736d; font-weight: 700; }}
	    input {{ min-height: 40px; border: 1px solid #cfd8d0; border-radius: 6px; padding: 0 10px; font: inherit; }}
    .error {{ color: #9b1c1c; font-weight: 700; }}
	    .login-panel, .summary-panel {{ margin-top: 18px; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 14px; margin-top: 12px; }}
    .summary-grid div {{ border-top: 1px solid #e2e6df; padding-top: 10px; }}
    .summary-grid strong {{ display: block; margin-top: 4px; font-size: 22px; }}
    .scan-progress {{ margin-top: 20px; border-top: 1px solid #e2e6df; padding-top: 16px; }}
    .progress-header {{ display: flex; justify-content: space-between; align-items: baseline; gap: 16px; font-weight: 700; }}
    .progress-track {{ height: 12px; margin-top: 10px; overflow: hidden; background: #edf0ea; border: 1px solid #d8ded6; border-radius: 999px; }}
    .progress-fill {{ width: 0%; height: 100%; background: #1f6b4b; transition: width 180ms ease; }}
    .progress-message {{ min-height: 22px; margin: 10px 0 0; }}
    button[disabled] {{ opacity: 0.72; cursor: wait; }}
    .remote-panel {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 420px); gap: 24px; align-items: start; }}
    .remote-actions p {{ margin: 0 0 10px; color: #1f2924; }}
    .label {{ display: inline-block; min-width: 72px; color: #66736d; font-weight: 700; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f2f4ef; border-radius: 6px; padding: 16px; max-height: 560px; overflow: auto; }}
    footer {{ margin-top: 32px; color: #66736d; font-size: 14px; }}
	    @media (max-width: 720px) {{ .remote-panel, .auth-panel {{ grid-template-columns: 1fr; }} header {{ align-items: flex-start; flex-direction: column; }} }}
	  </style>
</head>
<body>
  <main>
	    <header>
	      <h1>作业提醒网站 <span class="badge">{escape(settings.app_version)}</span></h1>
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
      const form = document.getElementById("scan-form");
      const panel = document.getElementById("scan-progress");
      if (!form || !panel) return;

      const button = document.getElementById("scan-button");
      const fill = document.getElementById("scan-progress-fill");
      const percentText = document.getElementById("scan-progress-percent");
      const statusText = document.getElementById("scan-progress-status");
      const messageText = document.getElementById("scan-progress-message");
      const track = panel.querySelector(".progress-track");
      let pollTimer = null;
      let reloadTimer = null;

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
          failed: "扫描失败"
        };
        statusText.textContent = labels[snapshot.status] || "扫描状态";
        if (button) {
          button.disabled = snapshot.status === "running";
          button.textContent = snapshot.status === "running" ? "扫描中" : "立即扫描";
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
            if (!pollTimer) pollTimer = window.setInterval(pollProgress, 1000);
            return;
          }
          stopPolling();
          if (snapshot.status === "succeeded" && !reloadTimer) {
            messageText.textContent = "扫描完成，正在刷新待办。";
            reloadTimer = window.setTimeout(() => window.location.reload(), 900);
          }
        } catch (error) {
          stopPolling();
          statusText.textContent = "扫描状态不可用";
          messageText.textContent = "无法读取扫描进度。";
          if (button) button.disabled = false;
        }
      };

      const startPolling = () => {
        stopPolling();
        pollProgress();
        pollTimer = window.setInterval(pollProgress, 1000);
      };

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (button) button.disabled = true;
        try {
          const response = await fetch(panel.dataset.startUrl, {
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
          statusText.textContent = "启动失败";
          messageText.textContent = "无法启动扫描。";
          if (button) button.disabled = false;
        }
      });

      pollProgress();
    })();
  </script>
    """


app = create_app()


def main() -> None:
    settings = load_settings()
    uvicorn.run("homework_watcher.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
