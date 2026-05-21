from __future__ import annotations

from datetime import datetime
from html import escape

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .git_utils import git_commit
from .settings import load_settings


def create_app() -> FastAPI:
    app = FastAPI(title="homework-watcher-v2")

    @app.get("/health")
    def health():
        settings = load_settings()
        return {
            "ok": True,
            "version": settings.app_version,
            "git_commit": git_commit(),
            "database_path": str(settings.database_path),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/", response_class=HTMLResponse)
    def index():
        settings = load_settings()
        return HTMLResponse(
            render_page(
                "当前待办",
                """
                <section class="panel">
                  <h2>当前待办</h2>
                  <p class="muted">v2 骨架已启动，待办列表会在数据库和扫描服务完成后接入。</p>
                </section>
                """,
                settings=settings,
            )
        )

    return app


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
    .muted {{ color: #66736d; }}
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
