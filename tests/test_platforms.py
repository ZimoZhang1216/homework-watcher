from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from homework_watcher.platforms import canonical_slugs, get_adapter, iter_adapters
import homework_watcher.platforms.xiaoya as xiaoya_module
from homework_watcher.platforms.changjiang_yuketang import parse_yuketang_log_text
from homework_watcher.platforms.base import (
    CandidateBlock,
    PlatformAssignment,
    PlaywrightUnavailableError,
    looks_like_empty_state,
)
from homework_watcher.platforms.xiaoya import (
    CourseEntry,
    XiaoyaAdapter,
    browser_process_uses_profile,
    collect_current_page_course_names,
    course_id_from_task_url,
    ensure_profile_available,
    has_real_course_assignments,
    looks_like_course_summary_assignment,
    normalize_course_task_url,
    parse_xiaoya_json_assignments,
    parse_xiaoya_task_page,
    parse_xiaoya_task_text,
    parse_xiaoya_row,
    scan_course_tasks,
    task_url_for,
    task_url_for_course_id,
    xiaoya_text_is_loading,
)
from homework_watcher.statuses import platform_status_is_done


class PlatformAdapterTests(unittest.TestCase):
    def test_registry_has_two_platforms(self):
        self.assertEqual(canonical_slugs(), ["changjiang-yuketang", "xiaoya"])
        self.assertEqual(get_adapter("长江雨课堂").platform_name, "长江雨课堂")
        self.assertEqual(
            get_adapter("xiaoya").url,
            "https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )
        self.assertEqual([adapter.slug for adapter in iter_adapters(["all"])], ["changjiang-yuketang", "xiaoya"])

    def test_xiaoya_scan_has_bounded_defaults(self):
        adapter = XiaoyaAdapter()

        self.assertLessEqual(adapter.course_timeout_seconds, 60)
        self.assertGreaterEqual(adapter.max_task_pages, 17)
        self.assertGreaterEqual(adapter.max_courses, 14)

    def test_xiaoya_loading_text_is_detected(self):
        self.assertTrue(xiaoya_text_is_loading("39%\n正在加载应用，请稍候。。。"))
        self.assertFalse(xiaoya_text_is_loading("我的课程\n结构化学\n作业任务"))

    def test_adapter_parses_unified_assignment_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = get_adapter("xiaoya")
            adapter.profile_root = Path(tmp)
            block = CandidateBlock(
                text="\n".join(
                    [
                        "课程：高等数学",
                        "作业：习题册第 3 章",
                        "截止时间：2026-05-15 23:59",
                        "状态：未提交",
                    ]
                ),
                url="https://example.test/homework/1",
            )

            items = adapter.parse_candidate_blocks([block], fallback_url="https://example.test/")

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].to_dict(), {
                "title": "习题册第 3 章",
                "course": "高等数学",
                "platform": "小雅",
                "due_at": "2026-05-15T23:59:00",
                "status": "未提交",
                "url": "https://example.test/homework/1",
            })

    def test_adapter_deduplicates_blocks(self):
        adapter = get_adapter("changjiang-yuketang")
        block = CandidateBlock(
            text="课程：大学物理\n作业：实验报告\n截止时间：2026-05-15 23:59\n已提交",
            url="https://example.test/work/1",
        )

        items = adapter.parse_candidate_blocks([block, block], fallback_url="https://example.test/")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].due_at, datetime(2026, 5, 15, 23, 59))
        self.assertEqual(items[0].status, "已提交")

    def test_empty_state_detection(self):
        self.assertTrue(looks_like_empty_state("暂无作业"))
        self.assertTrue(looks_like_empty_state("No assignments"))
        self.assertFalse(looks_like_empty_state("作业：实验报告 截止时间：2026-05-15 23:59"))

    def test_parse_yuketang_log_entries(self):
        text = """
        大学物理学基础 Ⅱ
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
            course="大学物理学基础 Ⅱ",
            platform="长江雨课堂",
            url="https://example.test/course",
        )

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].title, "第6章 第 6 次作业(1)")
        self.assertEqual(items[0].status, "未提交")
        self.assertEqual(items[0].due_at, datetime(2026, 5, 26, 8, 0))
        self.assertEqual(items[1].status, "已完成")
        self.assertEqual(items[2].status, "不可完成的作业")

    def test_parse_xiaoya_task_row(self):
        item = parse_xiaoya_row(
            [
                "实验预习",
                "第 1 章",
                "作业",
                "未提交",
                "自动发布",
                "全部学生",
                "2026-05-01 08:00",
                "2026-05-01 08:00",
                "2026-05-15 23:59",
                "查看",
            ],
            course="分析化学实验",
            platform="小雅",
            url="https://example.test/task",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.title, "实验预习")
        self.assertEqual(item.status, "未提交")
        self.assertEqual(item.due_at, datetime(2026, 5, 15, 23, 59))
        self.assertEqual(task_url_for("https://example.test/mycourse/1/resource"), "https://example.test/mycourse/1/task")
        self.assertEqual(task_url_for("https://example.test/mycourse/1/resource/last"), "https://example.test/mycourse/1/task")
        self.assertEqual(task_url_for("https://example.test/mycourse/1/task/last"), "https://example.test/mycourse/1/task")

    def test_parse_xiaoya_structure_chemistry_running_row(self):
        item = parse_xiaoya_row(
            [
                "实习2 点阵理论",
                "\\",
                "作业",
                "进行中",
                "全体",
                "全体",
                "2026-05-06 10:07:10",
                "2026-05-15 00:06:53",
                "2026-05-22 23:59:59",
                "进入任务",
            ],
            course="结构化学",
            platform="小雅",
            url="https://example.test/task",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.title, "实习2 点阵理论")
        self.assertEqual(item.status, "未提交")
        self.assertEqual(item.due_at, datetime(2026, 5, 22, 23, 59))

    def test_parse_xiaoya_structure_chemistry_text_snapshot(self):
        items = parse_xiaoya_task_text(
            """
            标题
            位置
            任务类型
            状态
            作业-08 \\ 作业 进行中 全体 全体 2026-05-06 10:05:48 2026-05-06 10:10:14
            2026-05-22 23:59:59
            进入任务
            实习2 点阵理论 \\ 作业 进行中 全体 全体 2026-05-06 10:07:10 2026-05-15 00:06:53
            2026-05-22 23:59:59
            进入任务
            """,
            course="结构化学",
            platform="小雅",
            url="https://example.test/task",
        )

        self.assertEqual([item.title for item in items], ["作业-08", "实习2 点阵理论"])
        self.assertTrue(all(item.status == "未提交" for item in items))

    def test_parse_xiaoya_structure_chemistry_inner_text_marks_completed_done(self):
        items = parse_xiaoya_task_text(
            """
            标题
            位置
            任务类型
            状态
            发布方式
            分配对象
            发布时间
            开始时间
            截止时间
            操作
            作业-08 \\ 作业 进行中 全体 全体 2026-05-06 10:05:48 2026-05-06 10:10:14
            2026-05-22 23:59:59
            进入任务
            实习1 分子对称性 \\ 作业 已完成 全体 全体 2026-05-06 10:06:48 2026-05-08 00:06:00
            2026-05-22 23:59:59
            进入任务
            实习2 点阵理论 \\ 作业 进行中 全体 全体 2026-05-06 10:07:10 2026-05-15 00:06:53
            2026-05-22 23:59:59
            进入任务
            """,
            course="结构化学",
            platform="小雅",
            url="https://example.test/task",
        )

        by_title = {item.title: item for item in items}
        self.assertEqual(set(by_title), {"作业-08", "实习1 分子对称性", "实习2 点阵理论"})
        self.assertEqual(by_title["作业-08"].status, "未提交")
        self.assertEqual(by_title["实习2 点阵理论"].status, "未提交")
        self.assertTrue(platform_status_is_done(by_title["实习1 分子对称性"].status))
        pending_titles = [item.title for item in items if not platform_status_is_done(item.status)]
        self.assertEqual(pending_titles, ["作业-08", "实习2 点阵理论"])

    def test_parse_xiaoya_json_assignments_recursively(self):
        items = parse_xiaoya_json_assignments(
            {
                "data": {
                    "records": [
                        {
                            "taskName": "作业-08",
                            "taskStatus": "进行中",
                            "endTime": "2026-05-22 23:59:59",
                            "taskUrl": "/app/jx-web/mycourse/6902426124991620398/task/1?token=secret",
                        },
                        {
                            "taskName": "实习1 分子对称性",
                            "taskStatus": "已完成",
                            "endTime": "2026-05-22 23:59:59",
                        },
                        {
                            "taskName": "实习2 点阵理论",
                            "taskStatus": "进行中",
                            "endTime": "2026-05-22 23:59:59",
                        },
                    ]
                }
            },
            course="结构化学",
            platform="小雅",
            fallback_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )

        by_title = {item.title: item for item in items}
        self.assertEqual(set(by_title), {"作业-08", "实习1 分子对称性", "实习2 点阵理论"})
        self.assertEqual(by_title["作业-08"].status, "未提交")
        self.assertTrue(platform_status_is_done(by_title["实习1 分子对称性"].status))
        self.assertIn("token=%3Credacted%3E", by_title["作业-08"].url)

    def test_xiaoya_course_summary_is_not_real_assignment(self):
        summary = PlatformAssignment(
            title="结构化学",
            course="结构化学",
            platform="小雅",
            due_at=datetime(2026, 7, 31),
            status="未知",
            url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )
        task = PlatformAssignment(
            title="实习2 点阵理论",
            course="结构化学",
            platform="小雅",
            due_at=datetime(2026, 5, 22, 23, 59, 59),
            status="未提交",
            url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )

        self.assertTrue(looks_like_course_summary_assignment(summary))
        self.assertFalse(looks_like_course_summary_assignment(task))
        self.assertFalse(has_real_course_assignments([summary], "结构化学"))
        self.assertTrue(has_real_course_assignments([summary, task], "结构化学"))

    def test_parse_xiaoya_row_uses_last_datetime_when_columns_are_split(self):
        item = parse_xiaoya_row(
            [
                "作业-08",
                "\\",
                "作业",
                "进行中",
                "全体",
                "全体",
                "2026-05-06 10:05:48",
                "2026-05-06 10:10:14",
                "进入任务",
                "2026-05-22 23:59:59",
            ],
            course="结构化学",
            platform="小雅",
            url="https://example.test/task",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.title, "作业-08")
        self.assertEqual(item.due_at, datetime(2026, 5, 22, 23, 59))

    def test_xiaoya_course_names_are_collected_with_page_evaluate(self):
        class FakePage:
            url = "https://nankai.ai-augmented.com/app/jx-web/mycourse"

            def evaluate(self, script):
                return [
                    "2026年春\n校内公开\n教务开课\n结构化学\n学院：化学学院",
                    "2026年春\n大学物理学基础 II\n学院：大学物理及实验",
                ]

        self.assertEqual(
            collect_current_page_course_names(FakePage()),
            ["结构化学", "大学物理学基础 II"],
        )

    def test_xiaoya_task_url_for_known_course_id(self):
        self.assertEqual(
            task_url_for_course_id(
                "https://nankai.ai-augmented.com/app/jx-web/mycourse",
                "6902426124991620398",
            ),
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )
        self.assertEqual(
            course_id_from_task_url(
                "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task?x=1"
            ),
            "6902426124991620398",
        )

    def test_xiaoya_course_task_url_can_be_derived_from_card_id(self):
        self.assertEqual(
            normalize_course_task_url(
                "",
                html='<div class="aia_course_card" data-course-id="6902426124991620398">结构化学</div>',
                base_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
            ),
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )
        self.assertEqual(
            normalize_course_task_url(
                "",
                html='{"courseId":"6902426124991620398","courseName":"结构化学"}',
                base_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
            ),
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )

    def test_xiaoya_scan_skips_course_without_task_url_instead_of_returning_to_list(self):
        class FakePage:
            def __init__(self):
                self.goto_calls = []

            def goto(self, *args, **kwargs):
                self.goto_calls.append((args, kwargs))

        page = FakePage()
        with self.assertRaises(PlaywrightUnavailableError) as ctx:
            scan_course_tasks(
                page,
                course=CourseEntry(name="LPOC", page_number=1, task_url=""),
                start_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
                platform="小雅",
                deadline=10**12,
                max_pages=1,
                progress=None,
            )

        self.assertIn("未能定位课程任务页 URL", str(ctx.exception))
        self.assertEqual(page.goto_calls, [])

    def test_xiaoya_task_pagination_is_bounded(self):
        class FakePage:
            url = "https://example.test/task"

        calls = {"clicks": 0}
        original_collect = xiaoya_module.collect_visible_task_rows
        original_text_parse = xiaoya_module.parse_xiaoya_task_text
        original_body_text = xiaoya_module.safe_body_text
        original_click = xiaoya_module.click_next_task_page
        original_wait = xiaoya_module.wait_for_xiaoya_shell
        try:
            xiaoya_module.collect_visible_task_rows = lambda *args, **kwargs: []
            xiaoya_module.parse_xiaoya_task_text = lambda *args, **kwargs: []
            xiaoya_module.safe_body_text = lambda page: ""
            xiaoya_module.wait_for_xiaoya_shell = lambda *args, **kwargs: None

            def fake_click(*args, **kwargs):
                calls["clicks"] += 1
                return True

            xiaoya_module.click_next_task_page = fake_click

            items = parse_xiaoya_task_page(
                FakePage(),
                course="结构化学",
                platform="小雅",
                deadline=None,
                max_pages=3,
            )
        finally:
            xiaoya_module.collect_visible_task_rows = original_collect
            xiaoya_module.parse_xiaoya_task_text = original_text_parse
            xiaoya_module.safe_body_text = original_body_text
            xiaoya_module.click_next_task_page = original_click
            xiaoya_module.wait_for_xiaoya_shell = original_wait

        self.assertEqual(items, [])
        self.assertEqual(calls["clicks"], 2)

    def test_xiaoya_profile_lock_detection_does_not_delete_active_profile_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "xiaoya"
            profile_dir.mkdir()
            lock_path = profile_dir / "SingletonLock"
            lock_path.write_text("active", encoding="utf-8")
            original_finder = xiaoya_module.find_active_profile_processes
            try:
                xiaoya_module.find_active_profile_processes = lambda profile: [4321]
                with self.assertRaises(PlaywrightUnavailableError):
                    ensure_profile_available(profile_dir)
                self.assertTrue(lock_path.exists())

                xiaoya_module.find_active_profile_processes = lambda profile: []
                ensure_profile_available(profile_dir)
                self.assertFalse(lock_path.exists())
            finally:
                xiaoya_module.find_active_profile_processes = original_finder

    def test_xiaoya_browser_profile_matcher_is_exact_to_user_data_dir(self):
        profile = "/var/lib/homework-watcher/web/users/1/browser-profiles/xiaoya"
        self.assertTrue(browser_process_uses_profile(f"/opt/chrome --user-data-dir={profile} about:blank", profile))
        self.assertFalse(browser_process_uses_profile(f"/opt/chrome --other={profile} about:blank", profile))

    def test_xiaoya_unavailable_status(self):
        item = parse_xiaoya_row(
            [
                "高等数学第十一次作业",
                "\\作业",
                "作业",
                "未开始",
                "班级",
                "0476",
                "2026-05-12 08:00",
                "2026-05-15 08:00",
                "2026-05-18 23:59",
                "进入任务",
            ],
            course="高等数学（B类）II",
            platform="小雅",
            url="https://example.test/task",
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.status, "不可完成的作业")


if __name__ == "__main__":
    unittest.main()
