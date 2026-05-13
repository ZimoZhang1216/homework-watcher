from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .calendar_sync import DEFAULT_CALENDAR_NAME
from .config import DEFAULT_LAUNCHD_LABEL, DEFAULT_LOG_DIR, ensure_app_dirs
from .reminders_sync import DEFAULT_REMINDERS_LIST_NAME


def build_launchd_plist(
    *,
    label: str = DEFAULT_LAUNCHD_LABEL,
    hw_command: str | None = None,
    hw_args: str = "check",
    interval_minutes: int = 60,
    daily_at: str | None = None,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> dict:
    command = hw_command or resolve_hw_command()
    plist = {
        "Label": label,
        "ProgramArguments": [
            "/bin/zsh",
            "-lc",
            f"{command} {hw_args}",
        ],
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "homework-watcher.out.log"),
        "StandardErrorPath": str(log_dir / "homework-watcher.err.log"),
    }
    if daily_at:
        hour, minute = parse_daily_at(daily_at)
        plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    else:
        plist["StartInterval"] = max(1, interval_minutes) * 60
    return plist


def install_launchd(
    *,
    label: str = DEFAULT_LAUNCHD_LABEL,
    interval_minutes: int = 60,
    scan: bool = False,
    calendar_sync: bool = False,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    reminders_sync: bool = False,
    reminders_list: str = DEFAULT_REMINDERS_LIST_NAME,
    daily_at: str | None = None,
    load: bool = True,
) -> Path:
    ensure_app_dirs()
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{label}.plist"
    hw_args = "check"
    if scan:
        hw_args += " --scan"
    if calendar_sync:
        hw_args += f" --calendar-sync --calendar-name {shlex.quote(calendar_name)}"
    if reminders_sync:
        hw_args += f" --reminders-sync --reminders-list {shlex.quote(reminders_list)}"
    plist = build_launchd_plist(
        label=label,
        interval_minutes=interval_minutes,
        daily_at=daily_at,
        hw_args=hw_args,
    )
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    if load:
        load_launch_agent(label, plist_path)
    return plist_path


def load_launch_agent(label: str, plist_path: Path) -> None:
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        return
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"launchd 配置已写入，但加载失败：{detail or result.returncode}")
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=False)


def resolve_hw_command() -> str:
    executable = shutil.which("hw")
    if executable:
        return shlex.quote(executable)
    return f"{shlex.quote(sys.executable)} -m homework_watcher"


def parse_daily_at(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("daily_at must use HH:MM format")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("daily_at must be a valid 24-hour time")
    return hour, minute
