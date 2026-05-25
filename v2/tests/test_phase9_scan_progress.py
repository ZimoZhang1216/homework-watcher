from __future__ import annotations

import unittest
from datetime import datetime

from homework_watcher.app import format_due_distance, render_assignment_table, render_page_script
from homework_watcher.scan_progress import ScanCancelled, ScanProgressStore


class Phase9ScanProgressTests(unittest.TestCase):
    def test_progress_store_tracks_running_and_success(self) -> None:
        store = ScanProgressStore()

        initial = store.get("alice")
        self.assertEqual(initial.status, "idle")
        self.assertEqual(initial.percent, 0)

        started, created = store.start("alice")
        self.assertTrue(created)
        self.assertEqual(started.status, "running")
        self.assertEqual(started.percent, 1)

        duplicate, duplicate_created = store.start("alice")
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.scan_id, started.scan_id)

        store.update("alice", started.scan_id, 47, "小雅：扫描课程")
        running = store.get("alice")
        self.assertEqual(running.percent, 47)
        self.assertEqual(running.message, "小雅：扫描课程")

        store.finish_success("alice", started.scan_id, {"todos": [{"title": "作业-08"}]})
        finished = store.get("alice")
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.percent, 100)
        self.assertIn("当前待办 1 条", finished.message)

    def test_progress_store_is_owner_scoped(self) -> None:
        store = ScanProgressStore()
        alice, _ = store.start("alice")
        bob, _ = store.start("bob")

        store.update("alice", alice.scan_id, 80, "alice scan")

        self.assertEqual(store.get("alice").percent, 80)
        self.assertEqual(store.get("bob").scan_id, bob.scan_id)
        self.assertEqual(store.get("bob").percent, 1)

    def test_success_snapshot_does_not_reload_without_running_scan(self) -> None:
        script = render_page_script()

        self.assertIn("let sawRunningScan = false;", script)
        self.assertIn('if (snapshot.status === "running")', script)
        self.assertIn('sawRunningScan = true;', script)
        self.assertIn('snapshot.status === "succeeded" && sawRunningScan && !reloadTimer', script)

    def test_progress_store_can_cancel_running_scan(self) -> None:
        store = ScanProgressStore()
        started, _created = store.start("alice")

        cancelled, did_cancel = store.cancel("alice")

        self.assertTrue(did_cancel)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.percent, 100)
        with self.assertRaises(ScanCancelled):
            store.raise_if_cancelled("alice", started.scan_id)

        store.finish_success("alice", started.scan_id, {"todos": [{"title": "late result"}]})
        self.assertEqual(store.get("alice").status, "cancelled")

    def test_render_page_script_supports_cancel_button(self) -> None:
        script = render_page_script()

        self.assertIn('document.getElementById("scan-cancel-button")', script)
        self.assertIn('fetch(panel.dataset.cancelUrl', script)
        self.assertIn('cancelled: "已强制结束"', script)
        self.assertIn("if (!panel) return;", script)

    def test_assignment_table_uses_due_distance_column(self) -> None:
        table = render_assignment_table(
            [
                {
                    "platform": "小雅",
                    "course": "结构化学",
                    "title": "作业-08",
                    "status_raw": "进行中",
                    "due_at": "2099-05-27T12:00:00",
                    "url": "",
                    "last_seen_at": "2026-05-25T10:00:00",
                }
            ]
        )

        self.assertIn("距今时间", table)
        self.assertNotIn("最后发现", table)

    def test_format_due_distance(self) -> None:
        self.assertEqual(
            format_due_distance("2026-05-27T12:30:00", now=datetime(2026, 5, 25, 12, 0, 0)),
            "还有2天30分钟",
        )
        self.assertEqual(
            format_due_distance("2026-05-25T09:00:00", now=datetime(2026, 5, 25, 12, 0, 0)),
            "已过3小时",
        )


if __name__ == "__main__":
    unittest.main()
