from __future__ import annotations

import unittest

from homework_watcher.config_loader import KnownCourseConfig, parse_platform_config
from homework_watcher.scanners.xiaoya_discovery import (
    build_xiaoya_task_url,
    course_name_candidates,
    course_name_from_card_text,
    dedupe_course_card_candidates,
    extract_course_id_from_raw,
    extract_course_name_from_raw,
    expected_course_count_from_text,
    merge_xiaoya_courses,
    raw_course_candidates_from_network_payloads,
    should_resolve_course_cards_by_click,
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

    def test_extract_course_from_resource_url(self) -> None:
        raw = {
            "href": "/app/jx-web/mycourse/6902425978165806721/resource",
            "absolute_href": "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902425978165806721/resource",
            "text": "大学物理学基础 II 学院：大学物理及实验 张连众 238次 91人 2026年春",
            "attrs": "",
            "ancestor_texts": [],
            "ancestor_attrs": [],
            "title_texts": [],
        }

        course_id = extract_course_id_from_raw(raw)
        course = extract_course_name_from_raw(raw, course_id=course_id)
        task_url = build_xiaoya_task_url(
            "https://nankai.ai-augmented.com/app/jx-web/mycourse",
            course_id,
            href=raw["absolute_href"],
        )

        self.assertEqual(course_id, "6902425978165806721")
        self.assertEqual(course, "大学物理学基础 II")
        self.assertEqual(
            task_url,
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902425978165806721/task",
        )

    def test_non_numeric_mycourse_segment_is_not_course_id(self) -> None:
        raw = {
            "href": "/app/jx-web/mycourse/teachingvideos",
            "absolute_href": "https://nankai.ai-augmented.com/app/jx-web/mycourse/teachingvideos",
            "text": "所有的课",
            "attrs": "",
            "ancestor_texts": [],
            "ancestor_attrs": [],
            "title_texts": [],
        }

        self.assertEqual(extract_course_id_from_raw(raw), "")

    def test_help_course_title_is_not_used_as_course_name(self) -> None:
        raw = {
            "href": "/app/jx-web/mycourse/010267695964/resource",
            "absolute_href": "https://nankai.ai-augmented.com/app/jx-web/mycourse/010267695964/resource",
            "text": "如何使用本系统",
            "attrs": "",
            "ancestor_texts": [],
            "ancestor_attrs": [],
            "title_texts": ["如何使用本系统"],
        }

        self.assertEqual(extract_course_name_from_raw(raw, course_id="010267695964"), "")

    def test_expected_active_course_count_from_mycourse_text(self) -> None:
        self.assertEqual(expected_course_count_from_text("正在进行（14） 即将开课（0） 已结束（14）"), 14)

    def test_network_payload_course_candidates(self) -> None:
        payloads = [
            {
                "url": "https://nankai.ai-augmented.com/api/course/list",
                "body": """
                {
                  "data": [
                    {
                      "id": "6902425978165806721",
                      "courseName": "大学物理学基础 II",
                      "resourceUrl": "/app/jx-web/mycourse/6902425978165806721/resource"
                    }
                  ]
                }
                """,
            }
        ]

        candidates = raw_course_candidates_from_network_payloads(
            payloads,
            mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(extract_course_id_from_raw(candidates[0]), "6902425978165806721")
        self.assertEqual(
            extract_course_name_from_raw(candidates[0], course_id="6902425978165806721"),
            "大学物理学基础 II",
        )

    def test_network_payload_ignores_generic_id_name_objects(self) -> None:
        payloads = [
            {
                "url": "https://nankai.ai-augmented.com/api/user/list",
                "body": '{"data": [{"id": "6902425978165806721", "name": "张连众"}]}',
            }
        ]

        candidates = raw_course_candidates_from_network_payloads(
            payloads,
            mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )

        self.assertEqual(candidates, [])

    def test_network_payload_accepts_course_like_id_name_objects(self) -> None:
        payloads = [
            {
                "url": "https://nankai.ai-augmented.com/api/jx-web/list",
                "body": """
                {
                  "data": [
                    {
                      "id": "6902425978165806721",
                      "name": "大学物理学基础 II",
                      "collegeName": "大学物理及实验",
                      "semesterName": "2026年春",
                      "teacherName": "张连众"
                    }
                  ]
                }
                """,
            }
        ]

        candidates = raw_course_candidates_from_network_payloads(
            payloads,
            mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(extract_course_id_from_raw(candidates[0]), "6902425978165806721")
        self.assertEqual(
            extract_course_name_from_raw(candidates[0], course_id="6902425978165806721"),
            "大学物理学基础 II",
        )

    def test_course_name_from_card_summary_text(self) -> None:
        names = course_name_candidates(
            "定量化学分析 学院：化学学院 陈明星 152次 44人 2026年春 校内公开 教务开课",
            course_id="6902425978165806721",
        )

        self.assertIn("定量化学分析", names)

    def test_course_name_from_prefixed_card_summary_text(self) -> None:
        self.assertEqual(
            course_name_from_card_text(
                "1 2 2026年春 校内公开 教务开课 LPOC 国家安全教育 "
                "学院：教务部 许晓,王存刚 52232次 4181人 2026年春 校内公开 教务开课"
            ),
            "LPOC 国家安全教育",
        )
        self.assertEqual(
            course_name_from_card_text(
                "279次 90人 2026年春 校内公开 教务开课 大学物理学基础 I "
                "学院：大学物理及实验 张连众 279次 90人 2026年春 校内公开 教务开课"
            ),
            "大学物理学基础 I",
        )

    def test_course_name_from_dashboard_noise_is_ignored(self) -> None:
        self.assertEqual(
            course_name_from_card_text(
                "所有的课 所有的课 正在进行 (14) 即将开课 (0) 已结束 (14) "
                "张子墨 南开大学 学习课程 28 待完成任务 0 未读消息"
            ),
            "",
        )

    def test_dedupe_course_card_candidates_keeps_named_course_cards(self) -> None:
        cards = dedupe_course_card_candidates(
            [
                {
                    "href": "",
                    "absolute_href": "",
                    "text": "所有的课 正在进行 (14) 张子墨 南开大学 学习课程 28 待完成任务 0 未读消息",
                    "attrs": "",
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [],
                },
                {
                    "href": "",
                    "absolute_href": "",
                    "text": "2026年春 校内公开 教务开课 大学物理学基础 I 学院：大学物理及实验 张连众",
                    "attrs": "",
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [],
                },
                {
                    "href": "",
                    "absolute_href": "",
                    "text": "大学物理学基础 I 学院：大学物理及实验 张连众",
                    "attrs": "",
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [],
                },
            ]
        )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title_texts"][0], "大学物理学基础 I")

    def test_click_fallback_runs_when_expected_count_exceeds_discovered(self) -> None:
        raw_candidates = [
            {
                "href": "",
                "absolute_href": "",
                "text": "大学物理学基础 I 学院：大学物理及实验 张连众 279次 90人 2026年春",
                "attrs": "",
                "ancestor_texts": [],
                "ancestor_attrs": [],
                "title_texts": [],
            }
        ]

        self.assertTrue(
            should_resolve_course_cards_by_click(
                raw_candidates,
                discovered_count=0,
                expected_count=14,
            )
        )
        self.assertFalse(
            should_resolve_course_cards_by_click(
                raw_candidates,
                discovered_count=14,
                expected_count=14,
            )
        )

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

    def test_xiaoya_config_allows_explicit_auto_discover(self) -> None:
        config = parse_platform_config(
            "xiaoya",
            {
                "enabled": True,
                "base_url": "https://nankai.ai-augmented.com",
                "auto_discover_courses": True,
            },
        )

        self.assertTrue(config.auto_discover_courses)


if __name__ == "__main__":
    unittest.main()
