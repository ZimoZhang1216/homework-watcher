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
                title="未来作业",
                course="有机化学",
                platform="固定作业",
                due_at=datetime(2026, 5, 18, 23, 59),
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
        ]

        subject = build_email_subject(assignments, now=now)
        body = build_email_report(assignments, now=now)

        self.assertEqual(subject, "作业日报 2026-05-15：待办 3，今日 1，明日 0，逾期 1")
        self.assertIn("逾期未提交：", body)
        self.assertIn("#1 2026-05-14 23:59 [结构化学 / 小雅] 逾期作业", body)
        self.assertIn("今日截止：", body)
        self.assertIn("#2 2026-05-15 23:59 [大学物理 / 长江雨课堂] 今日作业", body)
        self.assertIn("未来待办：", body)
        self.assertIn("#3 2026-05-18 23:59 [有机化学 / 固定作业] 未来作业", body)
        self.assertNotIn("已完成作业", body)

    def test_parse_recipients_accepts_commas_and_semicolons(self):
        self.assertEqual(parse_recipients("a@example.com, b@example.com;c@example.com"), ["a@example.com", "b@example.com", "c@example.com"])


if __name__ == "__main__":
    unittest.main()
