from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from homework_watcher.scan_service import ScanService
from homework_watcher.scanners.changjiang_yuketang import (
    CHANGJIANG_PLATFORM_LABEL,
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

    def test_find_yuketang_datetime_handles_slash_time(self) -> None:
        self.assertEqual(
            find_yuketang_datetime("截止时间：2026-05-26/08:00/周二"),
            datetime(2026, 5, 26, 8, 0),
        )

    def test_scan_service_registers_yuketang_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ScanService(service_settings(tmpdir))

        self.assertIn("changjiang-yuketang", service.scanners)


if __name__ == "__main__":
    unittest.main()
