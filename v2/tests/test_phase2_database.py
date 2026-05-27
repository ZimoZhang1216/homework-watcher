from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.config_loader import KnownCourseConfig
from homework_watcher.database import (
    create_session_factory,
    init_db,
    list_assignments,
    list_platform_courses,
    list_todos,
    platform_course_to_known_course,
    upsert_assignments,
    upsert_platform_courses,
)
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
        self.assertEqual(normalize_status("待提交"), "in_progress")
        self.assertEqual(normalize_status("未作答"), "in_progress")
        self.assertEqual(normalize_status("已结束"), "expired")
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

    def test_assignments_are_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = test_settings(f"sqlite:///{Path(tmpdir) / 'homework.sqlite3'}")
            init_db(settings)
            session_factory = create_session_factory(settings)
            candidate = AssignmentCandidate(
                platform="长江雨课堂",
                course="大学物理",
                title="第6章作业",
                status_raw="未提交",
                due_at=datetime(2026, 5, 26, 8, 0),
                url="https://example.test/task",
            )

            with session_factory() as session:
                first = upsert_assignments(session, [candidate], owner_key="alice")
                second = upsert_assignments(session, [candidate], owner_key="bob")

            self.assertEqual((first.inserted, second.inserted), (1, 1))
            with session_factory() as session:
                self.assertEqual([item.owner_key for item in list_todos(session, owner_key="alice")], ["alice"])
                self.assertEqual([item.owner_key for item in list_todos(session, owner_key="bob")], ["bob"])
                self.assertEqual(list_todos(session, owner_key="carol"), [])

    def test_todo_query_requires_current_status_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "homework.sqlite3"
            settings = test_settings(f"sqlite:///{db_path}")
            init_db(settings)
            session_factory = create_session_factory(settings)
            candidate = AssignmentCandidate(
                platform="小雅",
                course="结构化学",
                title="结构化学",
                status_raw="未知",
                due_at=datetime(2026, 7, 31, 0, 0),
                url="https://example.test/task",
            )
            with session_factory() as session:
                upsert_assignments(session, [candidate])
            engine = create_engine(f"sqlite:///{db_path}", future=True)
            with engine.begin() as connection:
                connection.execute(text("UPDATE assignments SET is_todo = 1 WHERE title = '结构化学'"))

            with session_factory() as session:
                self.assertEqual(list_todos(session), [])

    def test_old_assignment_table_migrates_to_default_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "homework.sqlite3"
            engine = create_engine(f"sqlite:///{db_path}", future=True)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE assignments (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          platform VARCHAR(80) NOT NULL,
                          course VARCHAR(200) NOT NULL,
                          title VARCHAR(300) NOT NULL,
                          status_raw VARCHAR(80) NOT NULL,
                          status_normalized VARCHAR(40) NOT NULL,
                          due_at DATETIME NOT NULL,
                          url TEXT NOT NULL,
                          source_key VARCHAR(300) NOT NULL,
                          fingerprint VARCHAR(64) NOT NULL,
                          is_todo BOOLEAN NOT NULL,
                          first_seen_at DATETIME NOT NULL,
                          last_seen_at DATETIME NOT NULL,
                          created_at DATETIME NOT NULL,
                          updated_at DATETIME NOT NULL,
                          raw_snapshot TEXT NOT NULL,
                          CONSTRAINT uq_assignment_identity UNIQUE (platform, course, title, due_at)
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO assignments (
                          platform, course, title, status_raw, status_normalized, due_at,
                          url, source_key, fingerprint, is_todo,
                          first_seen_at, last_seen_at, created_at, updated_at, raw_snapshot
                        ) VALUES (
                          '小雅', '结构化学', '作业-08', '进行中', 'in_progress', '2026-05-22 23:59:59',
                          'https://example.test', '', 'fp', 1,
                          '2026-05-20 10:00:00', '2026-05-20 10:00:00',
                          '2026-05-20 10:00:00', '2026-05-20 10:00:00', ''
                        )
                        """
                    )
                )

            settings = test_settings(f"sqlite:///{db_path}")
            init_db(settings)
            session_factory = create_session_factory(settings)
            with session_factory() as session:
                self.assertEqual([item.owner_key for item in list_assignments(session)], ["default"])

    def test_platform_courses_are_owner_scoped_and_upserted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = test_settings(f"sqlite:///{Path(tmpdir) / 'homework.sqlite3'}")
            init_db(settings)
            session_factory = create_session_factory(settings)
            course = KnownCourseConfig(
                course="结构化学",
                course_id="6902426124991620398",
                task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
                source="click_url",
            )

            with session_factory() as session:
                alice = upsert_platform_courses(
                    session,
                    [course],
                    owner_key="alice",
                    platform_key="xiaoya",
                    platform_label="小雅",
                )
                bob = upsert_platform_courses(
                    session,
                    [course],
                    owner_key="bob",
                    platform_key="xiaoya",
                    platform_label="小雅",
                )

            self.assertEqual((alice.inserted, bob.inserted), (1, 1))
            updated_course = KnownCourseConfig(
                course="结构化学 II",
                course_id="6902426124991620398",
                task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
                source="cached",
            )
            with session_factory() as session:
                updated = upsert_platform_courses(
                    session,
                    [updated_course],
                    owner_key="alice",
                    platform_key="xiaoya",
                    platform_label="小雅",
                )
                alice_courses = list_platform_courses(session, owner_key="alice", platform_key="xiaoya")
                bob_courses = list_platform_courses(session, owner_key="bob", platform_key="xiaoya")

            self.assertEqual(updated.updated, 1)
            self.assertEqual([item.course for item in alice_courses], ["结构化学 II"])
            self.assertEqual([item.course for item in bob_courses], ["结构化学"])
            self.assertEqual(platform_course_to_known_course(alice_courses[0]).course_id, "6902426124991620398")


if __name__ == "__main__":
    unittest.main()
