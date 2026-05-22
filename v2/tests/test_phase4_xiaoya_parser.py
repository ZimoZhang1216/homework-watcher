from __future__ import annotations

import unittest

from homework_watcher.scanners.xiaoya import (
    MAX_XIAOYA_PAGES,
    parse_xiaoya_due_at,
    parse_xiaoya_task_rows,
    parse_xiaoya_task_text,
)


STRUCTURE_TEXT = """
结构化学
作业任务
全部任务
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
作业-08 \\ 作业 进行中 全体 全体 2026-05-06 10:05:48 2026-05-06 10:10:14 2026-05-22 23:59:59
进入任务
实习1 分子对称性 \\ 作业 已完成 全体 全体 2026-05-06 10:06:48 2026-05-08 00:06:00 2026-05-22 23:59:59
进入任务
实习2 点阵理论 \\ 作业 进行中 全体 全体 2026-05-06 10:07:10 2026-05-15 00:06:53 2026-05-22 23:59:59
进入任务
"""


class Phase4XiaoyaParserTests(unittest.TestCase):
    def test_parse_structure_tasks_from_visible_text(self) -> None:
        assignments = parse_xiaoya_task_text(
            STRUCTURE_TEXT,
            course="结构化学",
            task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
            course_id="6902426124991620398",
        )
        by_title = {item.title: item for item in assignments}

        self.assertIn("作业-08", by_title)
        self.assertIn("实习1 分子对称性", by_title)
        self.assertIn("实习2 点阵理论", by_title)
        self.assertEqual(by_title["作业-08"].status_normalized, "in_progress")
        self.assertTrue(by_title["作业-08"].is_todo)
        self.assertEqual(by_title["实习1 分子对称性"].status_normalized, "completed")
        self.assertFalse(by_title["实习1 分子对称性"].is_todo)
        self.assertEqual(by_title["实习2 点阵理论"].due_at.isoformat(sep=" "), "2026-05-22 23:59:59")

    def test_filter_course_summary_fake_assignment(self) -> None:
        assignments = parse_xiaoya_task_text(
            "结构化学 \\ 作业 未知 全体 全体 2026-07-31 00:00:00",
            course="结构化学",
            task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398",
        )
        self.assertEqual(assignments, [])

    def test_parse_generic_course_table_rows(self) -> None:
        assignments = parse_xiaoya_task_rows(
            [
                [
                    "章节测验 3",
                    "线上测验",
                    "测验",
                    "未提交",
                    "全体",
                    "2026-06-01 09:00:00",
                    "2026-06-03 22:00:00",
                ],
                [
                    "阅读反馈",
                    "讨论",
                    "已完成",
                    "全体",
                    "2026-06-02 09:00:00",
                    "2026-06-04 18:30:00",
                ],
            ],
            course="大学英语",
            task_url="https://example.test/course/english/task",
            course_id="english",
        )
        by_title = {item.title: item for item in assignments}

        self.assertEqual(set(by_title), {"章节测验 3", "阅读反馈"})
        self.assertTrue(by_title["章节测验 3"].is_todo)
        self.assertFalse(by_title["阅读反馈"].is_todo)

    def test_parse_generic_course_split_visible_text(self) -> None:
        assignments = parse_xiaoya_task_text(
            """
            大学英语
            作业任务
            期末阅读报告
            作业
            进行中
            全体
            2026-06-02 09:00:00
            2026-06-09 23:59:59
            进入任务
            """,
            course="大学英语",
            task_url="https://example.test/course/english/task",
            course_id="english",
        )
        self.assertEqual([(item.title, item.status_raw) for item in assignments], [("期末阅读报告", "进行中")])

    def test_parse_generic_course_single_line_visible_row(self) -> None:
        assignments = parse_xiaoya_task_text(
            """
            大学物理
            作业任务
            光学实验报告 \\ 作业 进行中 全体 全体 2026-06-02 09:00:00 2026-06-09 23:59:59
            进入任务
            """,
            course="大学物理",
            task_url="https://example.test/course/physics/task",
            course_id="physics",
        )
        self.assertEqual([(item.title, item.status_raw) for item in assignments], [("光学实验报告", "进行中")])

    def test_date_only_defaults_to_end_of_day(self) -> None:
        self.assertEqual(parse_xiaoya_due_at("2026-05-22").isoformat(sep=" "), "2026-05-22 23:59:59")

    def test_pagination_has_hard_upper_bound(self) -> None:
        self.assertEqual(MAX_XIAOYA_PAGES, 20)


if __name__ == "__main__":
    unittest.main()
