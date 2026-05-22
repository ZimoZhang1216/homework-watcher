from __future__ import annotations

from datetime import datetime
from html import escape

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .database import assignment_to_dict, create_session_factory, init_db, list_assignments, list_todos
from .git_utils import git_commit
from .logging_utils import read_latest_scan_log
from .remote_login import RemoteLoginManager, build_novnc_url
from .scan_service import ScanService, latest_scan_result
from .settings import load_settings


DEFAULT_USER_KEY = "default"
DEFAULT_USER_LABEL = "默认账号"


def create_app() -> FastAPI:
    settings = load_settings()
    init_db(settings)
    session_factory = create_session_factory(settings)
    login_manager = RemoteLoginManager()
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
    def index():
        with session_factory() as session:
            todos = [assignment_to_dict(item) for item in list_todos(session)]
        login_status = login_manager.status()
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
                    <form method="post" action="/scan?redirect=1">
                      <button type="submit">立即扫描</button>
                    </form>
                    <a class="button-link" href="/logs/latest">查看最近扫描日志</a>
                    <a class="button-link" href="/assignments">查看所有记录</a>
                  </div>
                </section>
                {render_yuketang_login_panel(login_status)}
                """,
                settings=settings,
            )
        )

    @app.post("/scan")
    def scan(redirect: bool = Query(False)):
        result = ScanService(settings).run_scan()
        if redirect:
            return RedirectResponse("/", status_code=303)
        return result.to_dict()

    @app.get("/api/todos")
    def api_todos():
        with session_factory() as session:
            return [assignment_to_dict(item) for item in list_todos(session)]

    @app.get("/api/assignments")
    def api_assignments(
        platform: str | None = None,
        course: str | None = None,
        status: str | None = None,
    ):
        with session_factory() as session:
            items = [assignment_to_dict(item) for item in list_assignments(session)]
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
    def api_latest_scan():
        result = latest_scan_result()
        return result.to_dict() if result else {"scan": None}

    @app.get("/assignments", response_class=HTMLResponse)
    def assignments_page():
        with session_factory() as session:
            items = [assignment_to_dict(item) for item in list_assignments(session)]
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
            )
        )

    @app.get("/logs/latest", response_class=HTMLResponse)
    def latest_logs_page():
        lines = read_latest_scan_log(settings)
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
            )
        )

    @app.post("/login/changjiang-yuketang")
    async def start_yuketang_login():
        try:
            await login_manager.start_yuketang(
                settings,
                user_key=DEFAULT_USER_KEY,
                user_label=DEFAULT_USER_LABEL,
            )
        except Exception as exc:  # noqa: BLE001 - return the operator-facing failure.
            return HTMLResponse(
                render_page(
                    "长江雨课堂登录启动失败",
                    render_message_panel(
                        "长江雨课堂登录启动失败",
                        f"{type(exc).__name__}: {exc}",
                        back_href="/",
                    ),
                    settings=settings,
                ),
                status_code=500,
            )
        return RedirectResponse("/remote-login", status_code=303)

    @app.get("/remote-login", response_class=HTMLResponse)
    async def remote_login(request: Request):
        await login_manager.close_expired()
        status = login_manager.status()
        if status is None:
            return HTMLResponse(
                render_page(
                    "远程登录",
                    render_message_panel("没有远程登录会话", "请从首页打开长江雨课堂登录。", back_href="/"),
                    settings=settings,
                )
            )
        return HTMLResponse(
            render_page(
                "远程登录",
                render_remote_login_panel(status, build_novnc_url(str(request.base_url))),
                settings=settings,
            )
        )

    @app.post("/remote-login/finish")
    async def finish_remote_login():
        await login_manager.finish()
        return RedirectResponse("/", status_code=303)

    @app.post("/remote-login/cancel")
    async def cancel_remote_login():
        await login_manager.finish()
        return RedirectResponse("/", status_code=303)

    return app


def render_yuketang_login_panel(status) -> str:
    if status is not None:
        return f"""
        <section class="panel login-panel">
          <div class="panel-title">
            <h2>平台登录</h2>
            <span class="count">开</span>
          </div>
          <p class="muted">长江雨课堂远程登录窗口已打开，剩余 {escape(str(status.expires_in_seconds // 60))} 分钟。</p>
          <div class="actions">
            <a class="button-link" href="/remote-login">继续登录</a>
          </div>
        </section>
        """
    return """
    <section class="panel login-panel">
      <div class="panel-title">
        <h2>平台登录</h2>
        <span class="count">1</span>
      </div>
      <p class="muted">打开服务器上的长江雨课堂浏览器窗口，通过 noVNC 手动登录并保存浏览器登录态。</p>
      <div class="actions">
        <form method="post" action="/login/changjiang-yuketang">
          <button type="submit">长江雨课堂登录</button>
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


def render_page(title: str, body: str, *, settings) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - homework-watcher v2</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f2; color: #1f2924; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 48px 24px; }}
    header {{ display: flex; justify-content: space-between; gap: 24px; align-items: baseline; margin-bottom: 28px; }}
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
    .button-link {{ background: #fff; color: #1f6b4b; }}
    .primary-link {{ background: #1f6b4b; color: #fff; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 20px; }}
    .login-panel {{ margin-top: 18px; }}
    .remote-panel {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 420px); gap: 24px; align-items: start; }}
    .remote-actions p {{ margin: 0 0 10px; color: #1f2924; }}
    .label {{ display: inline-block; min-width: 72px; color: #66736d; font-weight: 700; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f2f4ef; border-radius: 6px; padding: 16px; max-height: 560px; overflow: auto; }}
    footer {{ margin-top: 32px; color: #66736d; font-size: 14px; }}
    @media (max-width: 720px) {{ .remote-panel {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>作业提醒网站 <span class="badge">{escape(settings.app_version)}</span></h1>
      <span>{escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</span>
    </header>
    {body}
    <footer>commit {escape(git_commit())} · database {escape(str(settings.database_path))}</footer>
  </main>
</body>
</html>"""


app = create_app()


def main() -> None:
    settings = load_settings()
    uvicorn.run("homework_watcher.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
