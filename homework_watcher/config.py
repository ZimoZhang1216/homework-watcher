from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path.home() / ".homework-watcher"
DEFAULT_DB_PATH = APP_DIR / "homework.db"
DEFAULT_ICS_PATH = APP_DIR / "homework-watcher.ics"
DEFAULT_LOG_DIR = APP_DIR / "logs"
DEFAULT_BROWSER_PROFILE_ROOT = APP_DIR / "browser-profiles"
DEFAULT_LAUNCHD_LABEL = "com.local.homework-watcher"


def db_path() -> Path:
    return Path(os.environ.get("HW_DB_PATH", DEFAULT_DB_PATH)).expanduser()


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_BROWSER_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
