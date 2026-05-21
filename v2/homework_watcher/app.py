from __future__ import annotations

from datetime import datetime
from html import escape

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from .database import assignment_to_dict, create_session_factory, init_db, list_assignments, list_todos
from .git_utils import git_commit
from .logging_utils import read_latest_scan_log
from .scan_service import ScanService, latest_scan_result
from .settings import load_settings


def create_app() -> FastAPI:
    settings = load_settings()
    init_db(settings)
    session_factory = create_session_factory(settings)
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

    return app


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
    .button-link {{ background: #fff; color: #1f6b4b; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 20px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f2f4ef; border-radius: 6px; padding: 16px; max-height: 560px; overflow: auto; }}
    footer {{ margin-top: 32px; color: #66736d; font-size: 14px; }}
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
