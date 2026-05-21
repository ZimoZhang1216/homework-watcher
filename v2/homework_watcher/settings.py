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
    return Settings(
        app_version=os.environ.get("APP_VERSION", APP_VERSION),
        database_url=os.environ.get("DATABASE_URL", "sqlite:///data/homework_watcher.sqlite3"),
        config_path=Path(os.environ.get("CONFIG_PATH", "config/platforms.yaml")),
        debug_dump_dir=Path(os.environ.get("DEBUG_DUMP_DIR", "/tmp/hw-v2-debug")),
        logs_dir=Path(os.environ.get("LOGS_DIR", "logs")),
        playwright_user_data_dir=Path(
            os.environ.get("PLAYWRIGHT_USER_DATA_DIR", "data/playwright-user-data")
        ),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path
