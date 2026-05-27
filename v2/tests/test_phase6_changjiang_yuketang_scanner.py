from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from homework_watcher.scan_service import ScanService
from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.scanners import ScannerContext
from homework_watcher.scanners.changjiang_yuketang import (
    CHANGJIANG_PLATFORM_LABEL,
    CHANGJIANG_PLATFORM_KEY,
    ChangjiangYuketangScanner,
    find_yuketang_datetime,
    parse_yuketang_log_text,
)
from homework_watcher.settings import Settings


def service_settings(tmpdir: str) -> Settings:
    root = Path(tmpdir)
    return Settings(
        app_version="V2-test",
        database_url=f"sqlite:///{root / 'homework.sqlite3'}",
        config_path=Path("missing-platforms.yaml"),
        debug_dump_dir=root / "debug",
        logs_dir=root / "logs",
        playwright_user_data_dir=root / "profiles",
        host="127.0.0.1",
        port=8080,
    )


class Phase6ChangjiangYuketangScannerTests(unittest.TestCase):
    def test_parse_yuketang_log_entries(self) -> None:
        text = """
        大学物理学基础 II
        09:48
        第6章 第 6 次作业(1)
        满分：10分 共1题 截止时间：2026-05-26/08:00/周二
        未作答
        08:50
        第6章 第 5 次作业(1)
        满分：30分 共1题 截止时间：2026-05-21/23:59/周四
        0
        得分
        09:00
        第7章 预告作业(1)
        满分：10分 共1题 截止时间：2026-05-30/08:00/周六
        未开始
        """

        items = parse_yuketang_log_text(
            text,
            course="大学物理学基础 II",
            platform=CHANGJIANG_PLATFORM_LABEL,
            url="https://example.test/course",
        )

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].title, "第6章 第 6 次作业(1)")
        self.assertEqual(items[0].status_raw, "未提交")
        self.assertTrue(items[0].is_todo)
        self.assertEqual(items[0].due_at, datetime(2026, 5, 26, 8, 0))
        self.assertEqual(items[1].status_raw, "已完成")
        self.assertFalse(items[1].is_todo)
        self.assertEqual(items[2].status_raw, "不可完成的作业")
        self.assertFalse(items[2].is_todo)

    def test_unknown_yuketang_status_is_treated_as_unfinished(self) -> None:
        text = """
        大学物理学基础 II
        第8章 第 1 次作业(1)
        满分：10分 共1题 截止时间：2026-06-26/08:00/周五
        未知
        """

        items = parse_yuketang_log_text(
            text,
            course="大学物理学基础 II",
            platform=CHANGJIANG_PLATFORM_LABEL,
            url="https://example.test/course",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status_raw, "未完成")
        self.assertTrue(items[0].is_todo)

    def test_find_yuketang_datetime_handles_slash_time(self) -> None:
        self.assertEqual(
            find_yuketang_datetime("截止时间：2026-05-26/08:00/周二"),
            datetime(2026, 5, 26, 8, 0),
        )

    def test_scan_service_registers_yuketang_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ScanService(service_settings(tmpdir))

        self.assertIn("changjiang-yuketang", service.scanners)

    def test_yuketang_scanner_records_summary_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = service_settings(tmpdir)
            scanner = ChangjiangYuketangScanner(settings)
            due_at = datetime(2026, 6, 9, 8, 0)
            expected = [
                AssignmentCandidate(
                    platform=CHANGJIANG_PLATFORM_LABEL,
                    course="大学物理学基础 II",
                    title="热学 第 3 次作业(1)",
                    status_raw="未提交",
                    due_at=due_at,
                    url="https://example.test",
                    source_key="test",
                    raw_snapshot="",
                )
            ]

            def fake_scan_course_list(_page, *, start_url, scan_id, emit, summary):
                summary["discovered_courses_count"] = 1
                summary["scanned_courses_count"] = 1
                summary["failed_courses_count"] = 0
                summary["parsed_assignments_count"] = 1
                return expected

            scanner.scan_course_list = fake_scan_course_list
            context = ScannerContext(
                scan_id="scan-test",
                platform_key=CHANGJIANG_PLATFORM_KEY,
                platform_config=None,
                user_key="default",
            )

            class FakeBrowserContext:
                pages = []

                def new_page(self):
                    return object()

                def close(self):
                    pass

            class FakeChromium:
                def launch_persistent_context(self, **_kwargs):
                    return FakeBrowserContext()

            class FakePlaywright:
                chromium = FakeChromium()

                def __enter__(self):
                    return self

                def __exit__(self, _exc_type, _exc, _traceback):
                    return False

            with patch(
                "homework_watcher.scanners.changjiang_yuketang.sync_playwright",
                return_value=FakePlaywright(),
            ), patch(
                "homework_watcher.scanners.changjiang_yuketang.prefer_student_entry"
            ):
                result = scanner.scan(context)

        summary = context.metadata[CHANGJIANG_PLATFORM_KEY]
        self.assertEqual(result, expected)
        self.assertEqual(summary["platform_label"], CHANGJIANG_PLATFORM_LABEL)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["discovered_courses_count"], 1)
        self.assertEqual(summary["scanned_courses_count"], 1)
        self.assertEqual(summary["failed_courses_count"], 0)
        self.assertEqual(summary["parsed_assignments_count"], 1)


if __name__ == "__main__":
    unittest.main()
