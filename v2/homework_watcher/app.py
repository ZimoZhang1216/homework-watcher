from __future__ import annotations

from datetime import datetime
from html import escape

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .database import assignment_to_dict, create_session_factory, init_db, list_todos
from .git_utils import git_commit
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
