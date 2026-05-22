from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .settings import PROJECT_ROOT


def git_commit() -> str:
    env_commit = os.environ.get("GIT_COMMIT")
    if env_commit:
        return env_commit
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return "unknown"
