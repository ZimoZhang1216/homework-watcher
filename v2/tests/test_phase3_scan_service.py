from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from homework_watcher.database import create_session_factory, init_db, list_todos
from homework_watcher.scan_service import ScanService
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


if __name__ == "__main__":
    unittest.main()
