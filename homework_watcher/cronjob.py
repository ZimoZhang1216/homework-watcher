from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import APP_DIR, DEFAULT_LOG_DIR, ensure_app_dirs
from .launchd import parse_daily_at


DEFAULT_CRON_NAME = "homework-watcher-email-report"
DEFAULT_CRON_ENV_PATH = APP_DIR / "email.env"
DEFAULT_CRON_SCRIPT_PATH = APP_DIR / f"{DEFAULT_CRON_NAME}.sh"
DEFAULT_CRON_LOG_PATH = DEFAULT_LOG_DIR / f"{DEFAULT_CRON_NAME}.log"


@dataclass(frozen=True)
class CronInstallResult:
    name: str
    script_path: Path
    log_path: Path
    env_file: Path
    entry: str


def install_cron(
    *,
    name: str = DEFAULT_CRON_NAME,
    daily_at: str = "08:00",
    env_file: Path = DEFAULT_CRON_ENV_PATH,
    script_path: Path = DEFAULT_CRON_SCRIPT_PATH,
    log_path: Path = DEFAULT_CRON_LOG_PATH,
    project_dir: Path | None = None,
    python_path: Path | None = None,
    scan: bool = True,
) -> CronInstallResult:
    ensure_app_dirs()
    project_dir = absolute_no_resolve(project_dir or Path.cwd())
    python_path = absolute_no_resolve(python_path or Path(sys.executable))
    env_file = absolute_no_resolve(env_file)
    script_path = absolute_no_resolve(script_path)
    log_path = absolute_no_resolve(log_path)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    script = build_email_report_script(
        project_dir=project_dir,
        python_path=python_path,
        env_file=env_file,
        scan=scan,
    )
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)

    entry = build_cron_entry(daily_at=daily_at, script_path=script_path, log_path=log_path)
    current = read_crontab()
    write_crontab(replace_managed_block(current, name=name, entry=entry))
    return CronInstallResult(name=name, script_path=script_path, log_path=log_path, env_file=env_file, entry=entry)


def uninstall_cron(*, name: str = DEFAULT_CRON_NAME) -> bool:
    current = read_crontab()
    updated = remove_managed_block(current, name=name)
    if updated == current:
        return False
    write_crontab(updated)
    return True


def build_email_report_script(
    *,
    project_dir: Path,
    python_path: Path,
    env_file: Path,
    scan: bool = True,
) -> str:
    lines = [
        "#!/bin/zsh",
        "set -u",
        'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
        f"cd {shlex.quote(str(project_dir))}",
        f"if [ -f {shlex.quote(str(env_file))} ]; then",
        "  set -a",
        f"  source {shlex.quote(str(env_file))}",
        "  set +a",
        "else",
        f"  echo {shlex.quote('Missing env file: ' + str(env_file))} >&2",
        "fi",
    ]
    python = shlex.quote(str(python_path))
    if scan:
        lines.extend(
            [
                f"{python} -m homework_watcher scan --no-notify || {{",
                "  echo 'Platform scan failed; continuing with cached database and fixed weekly assignments.' >&2",
                "}",
            ]
        )
    lines.append(f"{python} -m homework_watcher email-report")
    return "\n".join(lines) + "\n"


def build_cron_entry(*, daily_at: str, script_path: Path, log_path: Path) -> str:
    hour, minute = parse_daily_at(daily_at)
    return (
        f"{minute} {hour} * * * "
        f"/bin/zsh {shlex.quote(str(script_path))} >> {shlex.quote(str(log_path))} 2>&1"
    )


def absolute_no_resolve(path: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def replace_managed_block(crontab: str, *, name: str, entry: str) -> str:
    cleaned = remove_managed_block(crontab, name=name).rstrip()
    block = f"{start_marker(name)}\n{entry}\n{end_marker(name)}"
    return f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"


def remove_managed_block(crontab: str, *, name: str) -> str:
    start = start_marker(name)
    end = end_marker(name)
    lines = crontab.splitlines()
    kept: list[str] = []
    skipping = False
    changed = False
    for line in lines:
        if line.strip() == start:
            skipping = True
            changed = True
            continue
        if skipping and line.strip() == end:
            skipping = False
            continue
        if not skipping:
            kept.append(line)
    if not changed:
        return crontab
    return "\n".join(kept).rstrip() + ("\n" if kept else "")


def start_marker(name: str) -> str:
    return f"# BEGIN {name} managed by homework-watcher"


def end_marker(name: str) -> str:
    return f"# END {name} managed by homework-watcher"


def read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout
    detail = (result.stderr or result.stdout or "").strip()
    if "no crontab" in detail.lower():
        return ""
    raise RuntimeError(f"读取 crontab 失败：{detail or result.returncode}")


def write_crontab(content: str) -> None:
    result = subprocess.run(["crontab", "-"], input=content, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"写入 crontab 失败：{detail or result.returncode}")
