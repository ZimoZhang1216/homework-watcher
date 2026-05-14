from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from homework_watcher.db import HomeworkDB
from homework_watcher.cli import platform_status_is_done, platform_status_is_unavailable, status_for
from homework_watcher.ics import export_ics
from homework_watcher.notifier import Notifier
from homework_watcher.recurring_assignments import materialize_recurring_assignments
from homework_watcher.reminders import run_due_reminders
from homework_watcher.summary import build_daily_summary


class DBAndReminderTests(unittest.TestCase):
    def test_add_is_idempotent_and_done_marks_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = HomeworkDB(Path(tmp) / "homework.db")
            try:
                first, created_first = db.add_assignment(
                    title="大学物理实验报告",
                    course="大学物理",
                    platform="长江雨课堂",
                    due_at=datetime(2026, 5, 15, 23, 59),
                )
                second, created_second = db.add_assignment(
                    title="大学物理实验报告",
                    course="大学物理",
                    platform="长江雨课堂",
                    due_at=datetime(2026, 5, 15, 23, 59),
                    status="未提交",
                    url="https://example.test/work/1",
                )

                self.assertTrue(created_first)
                self.assertFalse(created_second)
                self.assertEqual(first.id, second.id)
                self.assertEqual(second.status, "未提交")
                self.assertEqual(second.url, "https://example.test/work/1")

                done = db.mark_done(first.id)
                self.assertTrue(done.is_done)
            finally:
                db.close()

    def test_existing_database_is_migrated_for_platform_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "homework.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    course TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    due_at TEXT NOT NULL,
                    completed_at TEXT,
                    source_text TEXT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.close()

            db = HomeworkDB(db_path)
            try:
                assignment, _ = db.add_assignment(
                    title="小雅作业",
                    platform="小雅",
                    due_at=datetime(2026, 5, 15, 23, 59),
                    status="未提交",
                    url="https://example.test/work/1",
                )

                self.assertEqual(assignment.status, "未提交")
                self.assertEqual(assignment.url, "https://example.test/work/1")
            finally:
                db.close()

    def test_unavailable_assignments_are_not_listed_as_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = HomeworkDB(Path(tmp) / "homework.db")
            try:
                db.add_assignment(
                    title="未开始作业",
                    platform="长江雨课堂",
                    due_at=datetime(2026, 5, 20, 8, 0),
                    status="不可完成的作业",
                )
                active, _ = db.add_assignment(
                    title="可提交作业",
                    platform="长江雨课堂",
                    due_at=datetime(2026, 5, 21, 8, 0),
                    status="未提交",
                )

                self.assertEqual([item.id for item in db.list_assignments()], [active.id])
                all_assignments = db.list_assignments(include_done=True)
                self.assertEqual(len(all_assignments), 2)
                self.assertEqual(status_for(all_assignments[0], datetime(2026, 5, 13, 12, 0)), "不可完成")
            finally:
                db.close()

    def test_due_reminder_uses_most_urgent_pending_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = HomeworkDB(Path(tmp) / "homework.db")
            try:
                now = datetime(2026, 5, 15, 18, 0)
                assignment, _ = db.add_assignment(
                    title="线性代数作业",
                    due_at=now + timedelta(hours=5),
                )

                events = run_due_reminders(db, Notifier(enabled=False), now=now)

                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].assignment.id, assignment.id)
                self.assertEqual(events[0].rule_key, "due_6h")
            finally:
                db.close()

    def test_platform_done_status_detection(self):
        self.assertTrue(platform_status_is_done("已提交"))
        self.assertTrue(platform_status_is_done("已完成"))
        self.assertFalse(platform_status_is_done("未提交"))
        self.assertFalse(platform_status_is_done("进行中"))
        self.assertTrue(platform_status_is_unavailable("不可完成的作业"))
        self.assertFalse(platform_status_is_unavailable("未提交"))

    def test_recurring_assignments_are_materialized_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = HomeworkDB(Path(tmp) / "homework.db")
            try:
                generated = materialize_recurring_assignments(
                    db,
                    now=datetime(2026, 5, 13, 12, 0),
                    horizon_days=7,
                )
                repeated = materialize_recurring_assignments(
                    db,
                    now=datetime(2026, 5, 13, 12, 0),
                    horizon_days=7,
                )

                self.assertEqual(len(generated), 2)
                self.assertEqual(len(repeated), 0)
                by_course = {item.course: item for item in db.list_assignments()}
                self.assertEqual(by_course["有机化学"].due_at, datetime(2026, 5, 17, 23, 59))
                self.assertEqual(by_course["有机化学"].platform, "线下")
                self.assertEqual(by_course["定量化学分析"].due_at, datetime(2026, 5, 19, 23, 59))
                self.assertEqual(by_course["定量化学分析"].platform, "飞书私信助教")
            finally:
                db.close()

    def test_recurring_assignments_do_not_duplicate_legacy_fixed_platform_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = HomeworkDB(Path(tmp) / "homework.db")
            try:
                db.add_assignment(
                    title="有机化学作业",
                    course="有机化学",
                    platform="固定作业",
                    due_at=datetime(2026, 5, 17, 23, 59),
                    status="未提交",
                    source_text="recurring:organic-chemistry-weekly:2026-05-17",
                )

                generated = materialize_recurring_assignments(
                    db,
                    now=datetime(2026, 5, 13, 12, 0),
                    horizon_days=7,
                )

                assignments = db.list_assignments(include_done=True)
                organic_rows = [
                    item
                    for item in assignments
                    if item.title == "有机化学作业"
                    and item.course == "有机化学"
                    and item.due_at == datetime(2026, 5, 17, 23, 59)
                ]
                self.assertEqual(len(generated), 1)
                self.assertEqual(generated[0].course, "定量化学分析")
                self.assertEqual(len(assignments), 2)
                self.assertEqual(len(organic_rows), 1)
            finally:
                db.close()

    def test_summary_and_ics_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = HomeworkDB(Path(tmp) / "homework.db")
            try:
                now = datetime(2026, 5, 15, 12, 0)
                db.add_assignment(title="今日作业", due_at=datetime(2026, 5, 15, 23, 59))
                db.add_assignment(title="明日作业", due_at=datetime(2026, 5, 16, 23, 59))
                db.add_assignment(title="逾期作业", due_at=datetime(2026, 5, 14, 23, 59))

                summary = build_daily_summary(db.list_assignments(), now=now)
                self.assertIn("今日作业", summary)
                self.assertIn("明日作业", summary)
                self.assertIn("逾期作业", summary)

                output = export_ics(db.list_assignments(), Path(tmp) / "homework.ics")
                content = output.read_text(encoding="utf-8")
                self.assertIn("BEGIN:VCALENDAR", content)
                self.assertIn("作业截止", content)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
