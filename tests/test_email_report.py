from __future__ import annotations

import unittest
from datetime import datetime

from homework_watcher.email_report import build_email_report, build_email_subject, parse_recipients
from homework_watcher.models import Assignment


class EmailReportTests(unittest.TestCase):
    def test_report_groups_all_pending_homework(self):
        now = datetime(2026, 5, 15, 12, 0)
        assignments = [
            Assignment(
                id=1,
                title="逾期作业",
                course="结构化学",
                platform="小雅",
                due_at=datetime(2026, 5, 14, 23, 59),
                status="进行中",
            ),
            Assignment(
                id=2,
                title="今日作业",
                course="大学物理",
                platform="长江雨课堂",
                due_at=datetime(2026, 5, 15, 23, 59),
                status="未提交",
            ),
            Assignment(
                id=3,
                title="有机化学作业",
                course="有机化学",
                platform="固定作业",
                due_at=datetime(2026, 5, 17, 23, 59),
                status="未提交",
            ),
            Assignment(
                id=5,
                title="下周固定作业",
                course="定量化学分析",
                platform="固定作业",
                due_at=datetime(2026, 5, 19, 23, 59),
                status="未提交",
            ),
            Assignment(
                id=4,
                title="已完成作业",
                course="高等数学",
                platform="小雅",
                due_at=datetime(2026, 5, 16, 23, 59),
                completed_at=datetime(2026, 5, 15, 9, 0),
            ),
            Assignment(
                id=6,
                title="平台显示已完成",
                course="分析化学",
                platform="小雅",
                due_at=datetime(2026, 5, 14, 23, 59),
                status="已完成",
            ),
        ]

        subject = build_email_subject(assignments, now=now)
        body = build_email_report(assignments, now=now)

        self.assertEqual(subject, "作业日报 2026-05-15：待办 3，今日 1，明日 0，逾期 1")
        self.assertIn("逾期未提交：", body)
        overdue_line = "1. 课程：结构化学 | 作业：逾期作业 | 平台：小雅 | 截止日期：2026-05-14 23:59 [距今：已逾期12小时1分钟]"
        today_line = "2. 课程：大学物理 | 作业：今日作业 | 平台：长江雨课堂 | 截止日期：2026-05-15 23:59 [距今：11小时59分钟]"
        future_line = "3. 课程：有机化学 | 作业：有机化学作业 | 平台：线下 | 截止日期：2026-05-17 23:59 [距今：2天11小时59分钟]"
        self.assertIn(overdue_line, body)
        self.assertIn("今日截止：", body)
        self.assertIn(today_line, body)
        self.assertIn("未来待办：", body)
        self.assertIn(future_line, body)
        self.assertLess(body.index(overdue_line), body.index(today_line))
        self.assertLess(body.index(today_line), body.index(future_line))
        self.assertNotIn("下周固定作业", body)
        self.assertNotIn("已完成作业", body)
        self.assertNotIn("平台显示已完成", body)
        self.assertNotIn("链接：", body)
        self.assertNotIn("状态：", body)
        self.assertNotIn("⚠️", body)

    def test_parse_recipients_accepts_commas_and_semicolons(self):
        self.assertEqual(parse_recipients("a@example.com, b@example.com;c@example.com"), ["a@example.com", "b@example.com", "c@example.com"])


if __name__ == "__main__":
    unittest.main()
