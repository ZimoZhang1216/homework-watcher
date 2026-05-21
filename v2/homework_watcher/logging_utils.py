from __future__ import annotations

import logging
from pathlib import Path

from .settings import Settings, resolve_path


def scan_log_path(settings: Settings) -> Path:
    path = resolve_path(settings.logs_dir) / "scan.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_scan_logger(settings: Settings) -> logging.Logger:
    logger = logging.getLogger("homework_watcher.scan")
    logger.setLevel(logging.INFO)
    path = scan_log_path(settings)
    marker = str(path)
    for handler in logger.handlers:
        if getattr(handler, "_hw_log_path", None) == marker:
            return logger

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler._hw_log_path = marker  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def read_latest_scan_log(settings: Settings, *, max_lines: int = 160) -> list[str]:
    path = scan_log_path(settings)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]
