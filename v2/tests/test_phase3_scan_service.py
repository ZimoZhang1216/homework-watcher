from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.database import create_session_factory, init_db, list_todos
from homework_watcher.scan_progress import ScanCancelled
from homework_watcher.scan_service import ScanService
from homework_watcher.scanners.base import ScannerContext
from homework_watcher.scanners.fake import FakeScanner
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


class Phase3ScanServiceTests(unittest.TestCase):
    def test_fake_scanner_uses_shared_service_and_writes_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = service_settings(tmpdir)
            result = ScanService(settings, scanners=[FakeScanner()]).run_scan(platforms=["fake"])

            self.assertEqual(result.errors, [])
            self.assertEqual(result.candidates_count, 2)
            self.assertEqual(result.filtered_count, 2)
            self.assertEqual(result.upsert_stats.inserted, 2)
            self.assertEqual([item["title"] for item in result.todos], ["共享链路待办"])

            init_db(settings)
            session_factory = create_session_factory(settings)
            with session_factory() as session:
                self.assertEqual([item.title for item in list_todos(session)], ["共享链路待办"])

            log_text = (Path(tmpdir) / "logs" / "scan.log").read_text(encoding="utf-8")
            self.assertIn("scan started", log_text)
            self.assertIn("platform start fake", log_text)
            self.assertIn("final todo count=1", log_text)

    def test_xiaoya_scan_writes_current_assignments_to_todos(self) -> None:
        class StubXiaoyaScanner:
            platform_key = "xiaoya"

            def scan(self, context: ScannerContext) -> list[AssignmentCandidate]:
                context.emit(50, "小雅：测试扫描")
                return [
                    AssignmentCandidate(
                        platform="小雅",
                        course="结构化学",
                        title="作业-08",
                        status_raw="进行中",
                        due_at=datetime(2026, 5, 22, 23, 59, 59),
                        url="https://nankai.ai-augmented.com/app/jx-web/mycourse/1/task",
                        source_key="xiaoya:structure:homework-08",
                    ),
                    AssignmentCandidate(
                        platform="小雅",
                        course="结构化学",
                        title="实习1 分子对称性",
                        status_raw="已完成",
                        due_at=datetime(2026, 5, 21, 23, 59, 59),
                        url="https://nankai.ai-augmented.com/app/jx-web/mycourse/1/task",
                        source_key="xiaoya:structure:symmetry",
                    ),
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = service_settings(tmpdir)
            result = ScanService(settings, scanners=[StubXiaoyaScanner()]).run_scan(platforms=["xiaoya"])

            self.assertEqual(result.errors, [])
            self.assertEqual(result.candidates_count, 2)
            self.assertEqual(result.upsert_stats.inserted, 2)
            self.assertEqual(
                [(item["platform"], item["course"], item["title"]) for item in result.todos],
                [("小雅", "结构化学", "作业-08")],
            )

    def test_scan_cancelled_propagates_out_of_service(self) -> None:
        class CancellableScanner:
            platform_key = "fake"

            def scan(self, context: ScannerContext) -> list[AssignmentCandidate]:
                context.emit(50, "正在扫描")
                return []

        with tempfile.TemporaryDirectory() as tmpdir:
            settings = service_settings(tmpdir)
            service = ScanService(settings, scanners=[CancellableScanner()])

            def cancel_progress(_percent: int, _message: str) -> None:
                raise ScanCancelled()

            with self.assertRaises(ScanCancelled):
                service.run_scan(platforms=["fake"], progress=cancel_progress)


if __name__ == "__main__":
    unittest.main()
