from __future__ import annotations

import shutil
import subprocess
import sys


class Notifier:
    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    def notify(self, title: str, message: str, *, subtitle: str = "") -> bool:
        if not self.enabled:
            return False
        if sys.platform != "darwin" or shutil.which("osascript") is None:
            print(f"[提醒] {title} {subtitle} {message}".strip())
            return False
        script = (
            f'display notification {applescript_quote(message)} '
            f'with title {applescript_quote(title)} '
            f'subtitle {applescript_quote(subtitle)} '
            'sound name "Glass"'
        )
        try:
            result = subprocess.run(["osascript", "-e", script], check=False, timeout=10)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            print(f"[提醒] {title} {subtitle} {message}".strip())
            return False


def applescript_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
