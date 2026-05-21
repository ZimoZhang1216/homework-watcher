from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from homework_watcher.platforms import canonical_slugs, get_adapter, iter_adapters
from homework_watcher.platforms.changjiang_yuketang import parse_yuketang_log_text
from homework_watcher.platforms.base import CandidateBlock, looks_like_empty_state
from homework_watcher.platforms.xiaoya import (
    XiaoyaAdapter,
    collect_current_page_course_names,
    parse_xiaoya_row,
    task_url_for,
    xiaoya_text_is_loading,
)


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
        self.assertLessEqual(adapter.max_task_pages, 5)
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
            def evaluate(self, script):
                return [
                    "2026年春\n校内公开\n教务开课\n结构化学\n学院：化学学院",
                    "2026年春\n大学物理学基础 II\n学院：大学物理及实验",
                ]

        self.assertEqual(
            collect_current_page_course_names(FakePage()),
            ["结构化学", "大学物理学基础 II"],
        )

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
