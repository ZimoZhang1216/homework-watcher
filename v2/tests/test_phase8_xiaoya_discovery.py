from __future__ import annotations

import unittest

from homework_watcher.config_loader import KnownCourseConfig, parse_platform_config
from homework_watcher.scanners.xiaoya_discovery import (
    build_xiaoya_task_url,
    extract_course_id_from_raw,
    extract_course_name_from_raw,
    merge_xiaoya_courses,
)


class Phase8XiaoyaDiscoveryTests(unittest.TestCase):
    def test_extract_course_from_link_and_card_text(self) -> None:
        raw = {
            "href": "/app/jx-web/mycourse/6902426124991620398",
            "absolute_href": "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398",
            "text": "进入课程",
            "attrs": "",
            "ancestor_texts": ["结构化学\n进入课程\n任务"],
            "ancestor_attrs": [],
            "title_texts": [],
        }

        course_id = extract_course_id_from_raw(raw)
        course = extract_course_name_from_raw(raw, course_id=course_id)
        task_url = build_xiaoya_task_url(
            "https://nankai.ai-augmented.com/app/jx-web/mycourse",
            course_id,
            href=raw["href"],
        )

        self.assertEqual(course_id, "6902426124991620398")
        self.assertEqual(course, "结构化学")
        self.assertEqual(
            task_url,
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )

    def test_merge_prefers_known_course_id(self) -> None:
        known = [
            KnownCourseConfig(
                course="结构化学",
                course_id="6902426124991620398",
                task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
            )
        ]
        discovered = [
            KnownCourseConfig(
                course="结构化学",
                course_id="6902426124991620398",
                task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
                source="discovered",
            ),
            KnownCourseConfig(
                course="高等数学（B类）II",
                course_id="1234567890123456789",
                task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/1234567890123456789/task",
                source="discovered",
            ),
        ]

        result = merge_xiaoya_courses(known, discovered)

        self.assertEqual(result.duplicates_count, 1)
        self.assertEqual([course.course for course in result.courses], ["结构化学", "高等数学（B类）II"])
        self.assertEqual(result.courses[0].source, "known")
        self.assertEqual(result.courses[1].source, "discovered")

    def test_extract_course_id_from_data_attribute(self) -> None:
        raw = {
            "href": "",
            "absolute_href": "",
            "text": "高等数学（B类）II 查看",
            "attrs": "class=course-card data-id=1234567890123456789",
            "ancestor_texts": [],
            "ancestor_attrs": [],
            "title_texts": ["高等数学（B类）II"],
        }

        course_id = extract_course_id_from_raw(raw)
        course = extract_course_name_from_raw(raw, course_id=course_id)

        self.assertEqual(course_id, "1234567890123456789")
        self.assertEqual(course, "高等数学（B类）II")

    def test_xiaoya_config_defaults_auto_discover_true(self) -> None:
        config = parse_platform_config(
            "xiaoya",
            {
                "enabled": True,
                "base_url": "https://nankai.ai-augmented.com",
                "known_courses": [
                    {
                        "course": "结构化学",
                        "course_id": "6902426124991620398",
                        "task_url": "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
                    }
                ],
            },
        )

        self.assertTrue(config.auto_discover_courses)
        self.assertEqual(config.mycourse_url, "https://nankai.ai-augmented.com/app/jx-web/mycourse")
        self.assertEqual(config.known_courses[0].source, "known")


if __name__ == "__main__":
    unittest.main()
