from __future__ import annotations

import re
from pathlib import Path

from .settings import Settings


SENSITIVE_RE = re.compile(
    r"(?i)(cookie|authorization|token|password|secret|session)\s*([:=])\s*([\"']?)[^\"'\s<>&]+",
)


def sanitize_debug_text(value: str) -> str:
    sanitized = SENSITIVE_RE.sub(r"\1\2\3<redacted>", value)
    sanitized = re.sub(r"(?is)<script\b.*?</script>", "<script><redacted></script>", sanitized)
    return sanitized


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", value.strip())
    return cleaned[:80].strip("-") or "unknown"


def dump_debug_page(page, settings: Settings, *, scan_id: str, stage: str, course: str, page_no: int) -> Path:
    directory = Path(settings.debug_dump_dir) / scan_id
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / f"{page_no:03d}-{safe_name(course)}-{safe_name(stage)}"

    url = getattr(page, "url", "")
    try:
        title = page.title(timeout=2000)
    except Exception:  # noqa: BLE001 - debug dump should never break scanning.
        title = ""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        text = ""
    try:
        html = page.content()
    except Exception:  # noqa: BLE001
        html = ""

    (prefix.with_suffix(".txt")).write_text(
        sanitize_debug_text(f"title={title}\nurl={url}\n\n{text[:8000]}"),
        encoding="utf-8",
    )
    (prefix.with_suffix(".html")).write_text(sanitize_debug_text(html[:200000]), encoding="utf-8")
    try:
        page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True, timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    return directory
