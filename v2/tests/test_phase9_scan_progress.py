from __future__ import annotations

import unittest
from datetime import datetime

from homework_watcher.app import (
    format_due_distance,
    render_assignment_table,
    render_auth_panel,
    render_scan_guide,
    render_scan_summary,
    render_scan_summaries,
    render_page_script,
)
from homework_watcher.scan_progress import ScanCancelled, ScanProgressStore
from homework_watcher.web_scan import (
    extract_scan_result_from_stdout,
    parse_progress_jsonl_line,
    server_scan_command_args,
)


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
        self.assertIn('data-label="课程"', table)
        self.assertNotIn("最后发现", table)

    def test_auth_panel_uses_student_id_wording(self) -> None:
        panel = render_auth_panel()

        self.assertIn("学号", panel)
        self.assertIn("显示名", panel)
        self.assertNotIn("用户名", panel)

    def test_scan_guide_includes_zero_start_and_mobile_qr_hint(self) -> None:
        guide = render_scan_guide()

        self.assertIn("登录本站", guide)
        self.assertIn("授权平台", guide)
        self.assertIn("立即扫描", guide)
        self.assertIn("移动端", guide)
        self.assertIn("扫码登录", guide)

    def test_scan_summary_surfaces_xiaoya_status_message(self) -> None:
        class Result:
            platform_summaries = {
                "xiaoya": {
                    "discovered_courses_count": 0,
                    "merged_courses_count": 0,
                    "scanned_courses_count": 0,
                    "failed_courses_count": 0,
                    "parsed_assignments_count": 0,
                    "todo_count": 9,
                    "message": "小雅本轮未发现可扫描课程，请确认登录态和课程页",
                }
            }

        summary = render_scan_summary(Result())

        self.assertIn("小雅最近扫描摘要", summary)
        self.assertIn("小雅本轮未发现可扫描课程", summary)
        self.assertIn("当前待办", summary)
        self.assertIn(">9<", summary)

    def test_scan_summary_accepts_cli_result_dict(self) -> None:
        summary = render_scan_summary(
            {
                "scan_id": "scan-test",
                "todos": [{"title": "作业-08"}],
                "platform_summaries": {
                    "xiaoya": {
                        "discovered_courses_count": 6,
                        "merged_courses_count": 8,
                        "scanned_courses_count": 8,
                        "failed_courses_count": 0,
                        "parsed_assignments_count": 12,
                        "todo_count": 9,
                        "message": "小雅：扫描完成",
                    }
                },
            }
        )

        self.assertIn("小雅最近扫描摘要", summary)
        self.assertIn("小雅：扫描完成", summary)
        self.assertIn(">8<", summary)
        self.assertIn(">12<", summary)

    def test_scan_summaries_render_changjiang_yuketang_summary(self) -> None:
        html = render_scan_summaries(
            {
                "scan_id": "scan-test",
                "todos": [{"title": "热学 第 3 次作业(1)"}],
                "platform_summaries": {
                    "changjiang-yuketang": {
                        "discovered_courses_count": 3,
                        "scanned_courses_count": 3,
                        "failed_courses_count": 0,
                        "parsed_assignments_count": 5,
                        "todo_count": 2,
                        "message": "长江雨课堂：扫描完成，识别 5 条作业",
                    }
                },
            }
        )

        self.assertIn("长江雨课堂最近扫描摘要", html)
        self.assertIn("长江雨课堂：扫描完成", html)
        self.assertIn("发现课程", html)
        self.assertIn("当前待办", html)
        self.assertIn(">3<", html)
        self.assertIn(">5<", html)
        self.assertIn(">2<", html)

    def test_web_scan_parses_cli_progress_jsonl(self) -> None:
        line = '{"type":"progress","percent":37,"message":"小雅：扫描课程 结构化学"}'

        self.assertEqual(parse_progress_jsonl_line(line), (37, "小雅：扫描课程 结构化学"))

    def test_web_scan_extracts_cli_result_jsonl(self) -> None:
        stdout = "\n".join(
            [
                '{"type":"progress","percent":20,"message":"小雅：任务页"}',
                (
                    '{"type":"result","result":{"scan_id":"scan-test",'
                    '"todos":[{"title":"作业-08"}],"platform_summaries":{}}}'
                ),
            ]
        )

        result = extract_scan_result_from_stdout(stdout)

        self.assertIsNotNone(result)
        self.assertEqual(result["scan_id"], "scan-test")
        self.assertEqual(result["todos"][0]["title"], "作业-08")

    def test_server_scan_command_uses_cli_scan_with_progress_jsonl(self) -> None:
        args = server_scan_command_args("default", progress_jsonl=True)

        self.assertIn("homework_watcher.cli", args)
        self.assertIn("scan", args)
        self.assertIn("--user", args)
        self.assertIn("default", args)
        self.assertIn("--progress-jsonl", args)

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
