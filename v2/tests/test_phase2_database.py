from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.database import create_session_factory, init_db, list_assignments, list_todos, upsert_assignments
from homework_watcher.settings import Settings
from homework_watcher.status import normalize_status


def test_settings(database_url: str) -> Settings:
    return Settings(
        app_version="V2-test",
        database_url=database_url,
        config_path=Path("config/platforms.yaml"),
        debug_dump_dir=Path("/tmp/hw-v2-debug-tests"),
        logs_dir=Path("logs"),
        playwright_user_data_dir=Path("data/playwright-user-data"),
        host="127.0.0.1",
        port=8080,
    )


class Phase2DatabaseTests(unittest.TestCase):
    def test_status_normalization(self) -> None:
        self.assertEqual(normalize_status("进行中"), "in_progress")
        self.assertEqual(normalize_status("未开始"), "pending")
        self.assertEqual(normalize_status("已完成"), "completed")
        self.assertEqual(normalize_status("已截止"), "expired")
        self.assertEqual(normalize_status("未完成"), "in_progress")
        self.assertEqual(normalize_status(""), "unknown")

    def test_upsert_and_todo_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = test_settings(f"sqlite:///{Path(tmpdir) / 'homework.sqlite3'}")
            init_db(settings)
            session_factory = create_session_factory(settings)
            due_at = datetime(2026, 5, 22, 23, 59, 59)
            candidates = [
                AssignmentCandidate(
                    platform="小雅",
                    course="结构化学",
                    title="作业-08",
                    status_raw="进行中",
                    due_at=due_at,
                    url="https://example.test/task",
                ),
                AssignmentCandidate(
                    platform="小雅",
                    course="结构化学",
                    title="实习1 分子对称性",
                    status_raw="已完成",
                    due_at=due_at,
                    url="https://example.test/task",
                ),
            ]

            with session_factory() as session:
                stats = upsert_assignments(session, candidates)
                self.assertEqual(stats.inserted, 2)
                self.assertEqual(stats.updated, 0)
                todos = list_todos(session)
                self.assertEqual([item.title for item in todos], ["作业-08"])

            updated_candidate = AssignmentCandidate(
                platform="小雅",
                course="结构化学",
                title="作业-08",
                status_raw="已完成",
                due_at=due_at,
                url="https://example.test/task",
            )
            with session_factory() as session:
                stats = upsert_assignments(session, [updated_candidate])
                self.assertEqual(stats.inserted, 0)
                self.assertEqual(stats.updated, 1)
                self.assertEqual(len(list_assignments(session)), 2)
                self.assertEqual(list_todos(session), [])


if __name__ == "__main__":
    unittest.main()
