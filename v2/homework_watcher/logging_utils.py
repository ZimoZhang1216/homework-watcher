from __future__ import annotations

import logging
import re
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


SCAN_ID_RE = re.compile(r"\[scan:([^\]]+)\]")


def read_latest_scan_log(settings: Settings, *, max_lines: int = 160, owner_key: str | None = None) -> list[str]:
    path = scan_log_path(settings)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if owner_key is not None:
        owned_scan_ids = {
            match.group(1)
            for line in lines
            if f"scan owner={owner_key}" in line
            for match in [SCAN_ID_RE.search(line)]
            if match is not None
        }
        lines = [
            line
            for line in lines
            if (match := SCAN_ID_RE.search(line)) is not None and match.group(1) in owned_scan_ids
        ]
    return lines[-max_lines:]
