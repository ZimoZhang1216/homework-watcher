from __future__ import annotations

import unittest
from datetime import datetime

from homework_watcher.calendar_sync import applescript_quote, build_calendar_sync_script
from homework_watcher.icloud_calendar_sync import build_caldav_event
from homework_watcher.launchd import build_launchd_plist, parse_daily_at
from homework_watcher.models import Assignment
from homework_watcher.reminders_sync import build_reminders_sync_script, name_for_reminder


class CalendarAndLaunchdTests(unittest.TestCase):
    def test_calendar_script_contains_dedup_marker_and_event(self):
        assignment = Assignment(
            id=42,
            title="课程论文",
            course="高等数学",
            platform="小雅",
            due_at=datetime(2026, 5, 18, 23, 59),
            status="进行中",
            url="https://example.test/task",
        )

        script = build_calendar_sync_script([assignment], calendar_name="作业提醒-iCloud")

        self.assertIn("homework-watcher-id:42", script)
        self.assertIn("作业截止：课程论文", script)
        self.assertIn("make new display alarm", script)
        self.assertIn("请先在 Calendar app 的 iCloud 下新建这个日历", script)
        self.assertNotIn("make new calendar", script)
        self.assertIn("repeat with eventIndex from (count of events of targetCalendar) to 1 by -1", script)

    def test_caldav_event_contains_marker_alarm_and_url(self):
        assignment = Assignment(
            id=42,
            title="课程论文",
            course="高等数学",
            platform="小雅",
            due_at=datetime(2026, 5, 18, 23, 59),
            status="进行中",
            url="https://example.test/task",
        )

        event = build_caldav_event(assignment)

        self.assertIn("BEGIN:VCALENDAR", event)
        self.assertIn("UID:homework-42@homework-watcher", event)
        self.assertIn("homework-watcher-id:42", event)
        self.assertIn("SUMMARY:作业截止：课程论文", event)
        self.assertIn("URL:https://example.test/task", event)
        self.assertIn("TRIGGER:-PT24H", event)
        self.assertIn("TRIGGER:-PT6H", event)
        self.assertIn("TRIGGER:-PT1H", event)

    def test_daily_launchd_uses_calendar_interval_and_calendar_sync_args(self):
        plist = build_launchd_plist(
            hw_command="/tmp/hw",
            hw_args="check --scan --calendar-sync --calendar-name '作业提醒-iCloud' --reminders-sync --reminders-list Reminders",
            daily_at="08:05",
        )

        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 8, "Minute": 5})
        self.assertNotIn("StartInterval", plist)
        self.assertIn("--calendar-sync", plist["ProgramArguments"][2])
        self.assertIn("--reminders-sync", plist["ProgramArguments"][2])

    def test_reminders_script_contains_list_marker_and_due_date(self):
        assignment = Assignment(
            id=7,
            title="作业-07",
            course="结构化学",
            platform="小雅",
            due_at=datetime(2026, 5, 15, 23, 59),
            status="进行中",
            url="https://example.test/task",
        )

        script = build_reminders_sync_script(
            [assignment],
            list_name="Reminders",
            now=datetime(2026, 5, 13, 12, 0),
        )

        self.assertIn('set targetListName to "Reminders"', script)
        self.assertIn("make new list", script)
        self.assertIn("homework-watcher-id:7", script)
        self.assertIn("⚠️ 结构化学：作业-07", script)
        self.assertIn("due date:reminderDue", script)
        self.assertIn("remind me date:reminderDue", script)
        self.assertIn('delete (reminders of targetList whose body contains "homework-watcher-id:")', script)

    def test_reminder_name_uses_course_prefix_when_available(self):
        with_course = Assignment(
            id=7,
            title="作业-07",
            course="结构化学",
            platform="小雅",
            due_at=datetime(2026, 5, 15, 23, 59),
        )
        without_course = Assignment(
            id=8,
            title="大学物理实验报告",
            course="",
            platform="",
            due_at=datetime(2026, 5, 16, 23, 59),
        )

        self.assertEqual(
            name_for_reminder(with_course, now=datetime(2026, 5, 12, 23, 59)),
            "⚠️ 结构化学：作业-07",
        )
        self.assertEqual(
            name_for_reminder(with_course, now=datetime(2026, 5, 1, 12, 0)),
            "结构化学：作业-07",
        )
        self.assertEqual(
            name_for_reminder(without_course, now=datetime(2026, 5, 1, 12, 0)),
            "作业：大学物理实验报告",
        )

    def test_parse_daily_at_validates_time(self):
        self.assertEqual(parse_daily_at("08:00"), (8, 0))
        with self.assertRaises(ValueError):
            parse_daily_at("25:00")

    def test_applescript_quote_joins_multiline_text_with_return(self):
        self.assertEqual(applescript_quote("a\nb"), '"a" & return & "b"')


if __name__ == "__main__":
    unittest.main()
