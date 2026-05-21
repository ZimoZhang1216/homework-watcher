from __future__ import annotations

from datetime import datetime

from homework_watcher.candidates import AssignmentCandidate

from .base import ScannerContext


class FakeScanner:
    platform_key = "fake"

    def scan(self, context: ScannerContext) -> list[AssignmentCandidate]:
        context.emit(20, "fake: start")
        context.emit(70, "fake: parsed 2 sample assignments")
        return [
            AssignmentCandidate(
                platform="测试平台",
                course="测试课程",
                title="共享链路待办",
                status_raw="进行中",
                due_at=datetime(2026, 5, 22, 23, 59, 59),
                url="https://example.test/todo",
                source_key="fake:todo",
                raw_snapshot="fake scanner sample",
            ),
            AssignmentCandidate(
                platform="测试平台",
                course="测试课程",
                title="共享链路已完成",
                status_raw="已完成",
                due_at=datetime(2026, 5, 21, 23, 59, 59),
                url="https://example.test/done",
                source_key="fake:done",
                raw_snapshot="fake scanner sample",
            ),
        ]
