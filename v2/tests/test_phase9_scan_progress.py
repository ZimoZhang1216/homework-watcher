from __future__ import annotations

import unittest

from homework_watcher.scan_progress import ScanProgressStore


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


if __name__ == "__main__":
    unittest.main()
