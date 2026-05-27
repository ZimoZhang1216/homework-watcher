from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .settings import PROJECT_ROOT, Settings


@dataclass
class ServerScanCommandError(RuntimeError):
    message: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    result: dict[str, Any] | None = None

    def __str__(self) -> str:
        if self.returncode is None:
            return self.message
        details = compact_process_output(self.stderr or self.stdout)
        suffix = f": {details}" if details else ""
        return f"{self.message} exit={self.returncode}{suffix}"


def server_scan_command_args(owner_key: str, *, progress_jsonl: bool = False) -> list[str]:
    args = [sys.executable, "-m", "homework_watcher.cli", "scan", "--user", owner_key]
    if progress_jsonl:
        args.append("--progress-jsonl")
    return args


def run_server_scan_command(
    settings: Settings,
    *,
    owner_key: str,
    emit: Callable[[int, str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    args = server_scan_command_args(owner_key, progress_jsonl=True)
    env = scan_command_env(settings)
    process = subprocess.Popen(
        args,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_lines: list[str] = []
    stderr_parts: list[str] = []
    result: dict[str, Any] | None = None
    stdout_queue: queue.Queue[str | None] = queue.Queue()
    stdout_thread = threading.Thread(
        target=read_stream_lines,
        args=(process.stdout, stdout_queue),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_stream_text,
        args=(process.stderr, stderr_parts),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        while True:
            if check_cancelled:
                check_cancelled()
            result = drain_stdout_queue(stdout_queue, stdout_lines, emit=emit) or result
            if process.poll() is not None:
                break
            time.sleep(0.2)
    except BaseException:
        terminate_process(process)
        raise

    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    result = drain_stdout_queue(stdout_queue, stdout_lines, emit=emit) or result
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_parts)
    result = result or extract_scan_result_from_stdout(stdout)
    if process.returncode != 0:
        raise ServerScanCommandError(
            "服务器扫描命令失败",
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            result=result,
        )
    if result is None:
        raise ServerScanCommandError(
            "服务器扫描命令没有返回可解析结果",
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return result


def scan_command_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_VERSION": settings.app_version,
            "DATABASE_URL": settings.database_url,
            "CONFIG_PATH": str(settings.config_path),
            "DEBUG_DUMP_DIR": str(settings.debug_dump_dir),
            "LOGS_DIR": str(settings.logs_dir),
            "PLAYWRIGHT_USER_DATA_DIR": str(settings.playwright_user_data_dir),
            "HOST": settings.host,
            "PORT": str(settings.port),
            "APP_SECRET_KEY": settings.session_secret,
        }
    )
    return env


def extract_scan_result_from_stdout(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            result = parsed.get("result")
            return result if is_scan_result_dict(result) else None
        return parsed if is_scan_result_dict(parsed) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            result = parsed.get("result")
            if is_scan_result_dict(result):
                return result
        if is_scan_result_dict(parsed):
            return parsed
    return None


def is_scan_result_dict(value: object) -> bool:
    return isinstance(value, dict) and "scan_id" in value and "todos" in value


def parse_progress_jsonl_line(line: str) -> tuple[int, str] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "progress":
        return None
    try:
        percent = int(payload.get("percent") or 0)
    except (TypeError, ValueError):
        percent = 0
    message = str(payload.get("message") or "").strip()
    return percent, message


def parse_result_jsonl_line(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return None
    result = payload.get("result")
    return result if is_scan_result_dict(result) else None


def drain_stdout_queue(
    stdout_queue: queue.Queue[str | None],
    stdout_lines: list[str],
    *,
    emit: Callable[[int, str], None] | None,
) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    while True:
        try:
            line = stdout_queue.get_nowait()
        except queue.Empty:
            return result
        if line is None:
            continue
        stdout_lines.append(line)
        progress = parse_progress_jsonl_line(line)
        if progress and emit:
            emit(*progress)
        parsed_result = parse_result_jsonl_line(line)
        if parsed_result:
            result = parsed_result


def read_stream_lines(stream, output_queue: queue.Queue[str | None]) -> None:
    if stream is None:
        output_queue.put(None)
        return
    try:
        for line in stream:
            output_queue.put(line)
    finally:
        output_queue.put(None)


def read_stream_text(stream, output: list[str]) -> None:
    if stream is None:
        return
    output.append(stream.read())


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def compact_process_output(value: str, *, limit: int = 500) -> str:
    compact = " ".join((value or "").split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."
