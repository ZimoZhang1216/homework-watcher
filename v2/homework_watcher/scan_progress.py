from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ScanCancelled(RuntimeError):
    pass


@dataclass
class ScanProgressSnapshot:
    scan_id: str
    owner_key: str
    status: str
    percent: int
    message: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "owner_key": self.owner_key,
            "status": self.status,
            "percent": self.percent,
            "message": self.message,
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "error": self.error,
            "result": self.result,
        }


class ScanProgressStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, ScanProgressSnapshot] = {}
        self._cancelled: set[tuple[str, str]] = set()

    def start(self, owner_key: str) -> tuple[ScanProgressSnapshot, bool]:
        with self._lock:
            current = self._items.get(owner_key)
            if current is not None and current.status == "running":
                return copy_snapshot(current), False
            snapshot = ScanProgressSnapshot(
                scan_id=f"web-scan-{uuid.uuid4().hex[:12]}",
                owner_key=owner_key,
                status="running",
                percent=1,
                message="准备扫描",
                started_at=datetime.now(),
            )
            self._items[owner_key] = snapshot
            self._cancelled.discard((owner_key, snapshot.scan_id))
            return copy_snapshot(snapshot), True

    def get(self, owner_key: str) -> ScanProgressSnapshot:
        with self._lock:
            snapshot = self._items.get(owner_key)
            if snapshot is None:
                return idle_snapshot(owner_key)
            return copy_snapshot(snapshot)

    def update(self, owner_key: str, scan_id: str, percent: int, message: str) -> None:
        with self._lock:
            snapshot = self._items.get(owner_key)
            if snapshot is None or snapshot.scan_id != scan_id or snapshot.status != "running":
                return
            snapshot.percent = clamp_percent(percent)
            snapshot.message = message.strip() or snapshot.message

    def cancel(self, owner_key: str) -> tuple[ScanProgressSnapshot, bool]:
        with self._lock:
            snapshot = self._items.get(owner_key)
            if snapshot is None:
                return idle_snapshot(owner_key), False
            if snapshot.status != "running":
                return copy_snapshot(snapshot), False
            self._cancelled.add((owner_key, snapshot.scan_id))
            snapshot.status = "cancelled"
            snapshot.percent = 100
            snapshot.message = "扫描已强制结束"
            snapshot.finished_at = datetime.now()
            snapshot.error = ""
            return copy_snapshot(snapshot), True

    def raise_if_cancelled(self, owner_key: str, scan_id: str) -> None:
        with self._lock:
            if (owner_key, scan_id) in self._cancelled:
                raise ScanCancelled("scan cancelled by user")

    def finish_success(self, owner_key: str, scan_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            snapshot = self._items.get(owner_key)
            if snapshot is None or snapshot.scan_id != scan_id or snapshot.status != "running":
                return
            if result.get("mode") == "courses" or ("courses" in result and "todos" not in result):
                course_count = len(result.get("courses") or [])
                message = f"扫描课程完成，保存 {course_count} 门课程"
            else:
                todo_count = len(result.get("todos") or [])
                message = f"扫描完成，当前待办 {todo_count} 条"
            snapshot.status = "succeeded"
            snapshot.percent = 100
            snapshot.message = message
            snapshot.finished_at = datetime.now()
            snapshot.result = result
            snapshot.error = ""

    def finish_failed(self, owner_key: str, scan_id: str, error: str) -> None:
        with self._lock:
            snapshot = self._items.get(owner_key)
            if snapshot is None or snapshot.scan_id != scan_id or snapshot.status != "running":
                return
            snapshot.status = "failed"
            snapshot.percent = 100
            snapshot.message = "扫描失败"
            snapshot.finished_at = datetime.now()
            snapshot.error = error

    def finish_cancelled(self, owner_key: str, scan_id: str) -> None:
        with self._lock:
            snapshot = self._items.get(owner_key)
            if snapshot is None or snapshot.scan_id != scan_id:
                return
            self._cancelled.add((owner_key, scan_id))
            snapshot.status = "cancelled"
            snapshot.percent = 100
            snapshot.message = "扫描已强制结束"
            snapshot.finished_at = snapshot.finished_at or datetime.now()
            snapshot.error = ""


def clamp_percent(percent: int) -> int:
    return min(100, max(0, int(percent)))


def idle_snapshot(owner_key: str) -> ScanProgressSnapshot:
    return ScanProgressSnapshot(
        scan_id="",
        owner_key=owner_key,
        status="idle",
        percent=0,
        message="等待扫描",
    )


def copy_snapshot(snapshot: ScanProgressSnapshot) -> ScanProgressSnapshot:
    return ScanProgressSnapshot(
        scan_id=snapshot.scan_id,
        owner_key=snapshot.owner_key,
        status=snapshot.status,
        percent=snapshot.percent,
        message=snapshot.message,
        started_at=snapshot.started_at,
        finished_at=snapshot.finished_at,
        error=snapshot.error,
        result=dict(snapshot.result) if snapshot.result is not None else None,
    )
