from __future__ import annotations

import unittest
from datetime import datetime

from homework_watcher.datetime_utils import parse_datetime
from homework_watcher.parser import parse_assignments


class ParserTests(unittest.TestCase):
    def test_parse_structured_chinese_text(self):
        text = """
        课程：大学物理
        平台：长江雨课堂
        作业：大学物理实验报告
        截止时间：2026-05-15 23:59
        """

        items = parse_assignments(text)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "大学物理实验报告")
        self.assertEqual(items[0].course, "大学物理")
        self.assertEqual(items[0].platform, "长江雨课堂")
        self.assertEqual(items[0].due_at, datetime(2026, 5, 15, 23, 59))

    def test_parse_relative_yearless_date(self):
        due = parse_datetime("5月15日 23:59", now=datetime(2026, 5, 1, 9, 0))

        self.assertEqual(due, datetime(2026, 5, 15, 23, 59))

    def test_parse_tomorrow(self):
        due = parse_datetime("明天 23:59", now=datetime(2026, 5, 1, 9, 0))

        self.assertEqual(due, datetime(2026, 5, 2, 23, 59))

    def test_parse_yuketang_slash_time(self):
        due = parse_datetime("截止时间：2026-05-26/08:00/周二")

        self.assertEqual(due, datetime(2026, 5, 26, 8, 0))


if __name__ == "__main__":
    unittest.main()
