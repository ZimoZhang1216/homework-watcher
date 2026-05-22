from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    app_version: str
    database_url: str
    config_path: Path
    debug_dump_dir: Path
    logs_dir: Path
    playwright_user_data_dir: Path
    host: str
    port: int
    session_secret: str = "dev-insecure-session-secret"

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Only sqlite:/// DATABASE_URL is supported in v2")
        raw_path = self.database_url[len(prefix) :]
        path = Path(raw_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path


def load_settings() -> Settings:
    file_env = read_env_file(PROJECT_ROOT / ".env")

    def get_value(key: str, default: str) -> str:
        return os.environ.get(key) or file_env.get(key) or default

    return Settings(
        app_version=get_value("APP_VERSION", APP_VERSION),
        database_url=get_value("DATABASE_URL", "sqlite:///data/homework_watcher.sqlite3"),
        config_path=Path(get_value("CONFIG_PATH", "config/platforms.yaml")),
        debug_dump_dir=Path(get_value("DEBUG_DUMP_DIR", "/tmp/hw-v2-debug")),
        logs_dir=Path(get_value("LOGS_DIR", "logs")),
        playwright_user_data_dir=Path(
            get_value("PLAYWRIGHT_USER_DATA_DIR", "data/playwright-user-data")
        ),
        host=get_value("HOST", "127.0.0.1"),
        port=int(get_value("PORT", "8080")),
        session_secret=get_value("APP_SECRET_KEY", "dev-insecure-session-secret"),
    )


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
