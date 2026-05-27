from __future__ import annotations

import unittest
from unittest.mock import patch

import homework_watcher.scanners.xiaoya_discovery as xiaoya_discovery
from homework_watcher.config_loader import KnownCourseConfig, parse_platform_config
from homework_watcher.scanners.xiaoya_discovery import (
    CLICK_DISCOVERY_STEP_TIMEOUT_MS,
    build_xiaoya_task_url,
    click_step_deadline,
    click_url_course_to_raw_candidate,
    collect_xiaoya_network_payload,
    course_name_candidates,
    course_name_from_card_candidate,
    course_name_from_card_text,
    dedupe_course_card_candidates,
    extract_course_id,
    extract_course_id_from_raw,
    extract_course_name_from_raw,
    expected_course_count_from_text,
    merge_xiaoya_courses,
    normalize_known_xiaoya_course,
    normalize_discovered_xiaoya_course,
    raw_course_candidates_from_network_payloads,
    has_clickable_course_signal,
    resolve_course_cards_by_clicking,
    should_resolve_course_cards_by_click,
)


class FakeResponse:
    def __init__(self, *, url: str, body: str, content_type: str = "application/json") -> None:
        self.url = url
        self.headers = {"content-type": content_type}
        self._body = body

    def text(self) -> str:
        return self._body


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

    def test_known_course_url_is_normalized_to_task_url(self) -> None:
        course = normalize_known_xiaoya_course(
            KnownCourseConfig(
                course="结构化学",
                course_id="6902426124991620398",
                task_url="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398",
            )
        )

        self.assertEqual(
            course.task_url,
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )

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

    def test_click_url_candidate_uses_internal_course_url_id(self) -> None:
        task_url = build_xiaoya_task_url(
            "https://nankai.ai-augmented.com/app/jx-web/mycourse",
            "6902426104825404425",
            href="https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426104825404425/resource",
        )
        raw = click_url_course_to_raw_candidate(
            course_name="国家安全教育",
            course_id="6902426104825404425",
            task_url=task_url,
            card_text="国家安全教育 课程内容 作业任务 课程工具",
        )
        normalized = normalize_discovered_xiaoya_course(
            KnownCourseConfig(
                course=extract_course_name_from_raw(raw, course_id="6902426104825404425"),
                course_id=extract_course_id_from_raw(raw),
                task_url=raw["href"],
                source=raw["source"],
            )
        )

        self.assertEqual(extract_course_id_from_raw(raw), "6902426104825404425")
        self.assertEqual(extract_course_name_from_raw(raw, course_id="6902426104825404425"), "国家安全教育")
        self.assertEqual(task_url, "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426104825404425/task")
        self.assertEqual(normalized.source, "click_url")

    def test_extract_course_id_from_any_internal_mycourse_page(self) -> None:
        for suffix in ["resource", "task", "tool", "overview", "notice", "learning"]:
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    extract_course_id(
                        f"https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426104825404425/{suffix}"
                    ),
                    "6902426104825404425",
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

    def test_network_payload_recurses_all_json_for_course_objects(self) -> None:
        payloads = [
            {
                "url": "https://nankai.ai-augmented.com/api/jw-starcmooc/query",
                "body": """
                {
                  "payload": {
                    "tabs": [
                      {
                        "items": [
                          {
                            "title": "结构化学",
                            "teachCourseId": "6902426124991620398"
                          },
                          {
                            "course_name": "高等数学（B类）II",
                            "classroomId": 6902426104825404425
                          }
                        ]
                      }
                    ]
                  }
                }
                """,
            }
        ]

        candidates = raw_course_candidates_from_network_payloads(
            payloads,
            mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )

        self.assertEqual(
            [extract_course_id_from_raw(candidate) for candidate in candidates],
            ["6902426124991620398", "6902426104825404425"],
        )
        self.assertEqual(candidates[0]["text"], "结构化学")
        self.assertEqual(
            candidates[0]["href"],
            "https://nankai.ai-augmented.com/app/jx-web/mycourse/6902426124991620398/task",
        )

    def test_network_payload_sanitizes_sensitive_query_in_candidate_attrs(self) -> None:
        payloads = [
            {
                "url": "https://nankai.ai-augmented.com/api/list?token=secret&ok=1",
                "body": '{"data": [{"courseName": "大学物理学基础 II", "courseId": "6902425978165806721"}]}',
            }
        ]

        candidates = raw_course_candidates_from_network_payloads(
            payloads,
            mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
        )

        self.assertEqual(len(candidates), 1)
        self.assertIn("token=%3Credacted%3E", candidates[0]["attrs"])
        self.assertNotIn("secret", candidates[0]["attrs"])

    def test_collect_network_payload_accepts_unmarked_json_response(self) -> None:
        payloads: list[dict[str, str]] = []

        collect_xiaoya_network_payload(
            FakeResponse(
                url="https://nankai.ai-augmented.com/api/common/list",
                body='{"data": [{"courseName": "结构化学", "courseId": "6902426124991620398"}]}',
            ),
            mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
            payloads=payloads,
        )

        self.assertEqual(len(payloads), 1)

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
            "国家安全教育",
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

    def test_xiaoya_shell_navigation_is_not_click_candidate(self) -> None:
        text = (
            "我的课程 我的课程 我的课程 我的文档 基础库 发现 我的课程 我的文档 基础库 发现 "
            "我的课程 我的文档 基础库 发现 登录 收起菜单"
        )
        raw = {
            "href": "",
            "absolute_href": "",
            "text": text,
            "attrs": "",
            "ancestor_texts": [],
            "ancestor_attrs": [],
            "title_texts": [],
        }

        self.assertEqual(course_name_from_card_text(text), "")
        self.assertFalse(has_clickable_course_signal(raw))
        self.assertEqual(dedupe_course_card_candidates([raw]), [])

    def test_course_name_uses_xiaoya_click_tracking_name(self) -> None:
        self.assertEqual(
            course_name_from_card_candidate(
                {
                    "href": "",
                    "absolute_href": "",
                    "text": "2026年春 校内公开 教务开课 大学物理学基础 Ⅰ 学院：大学物理及实验 张连众",
                    "attrs": (
                        "class=aia_course_card xy_animation__hover_upward_floating "
                        "data-xy-click-pt-name=大学物理学基础 Ⅰ "
                        "data-xy-click-pt=enter-course data-xy-click-pt-element-type=3"
                    ),
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [],
                }
            ),
            "大学物理学基础 Ⅰ",
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

    def test_dedupe_course_card_candidates_prefers_real_xiaoya_card(self) -> None:
        cards = dedupe_course_card_candidates(
            [
                {
                    "href": "",
                    "absolute_href": "",
                    "text": (
                        "我的课程 我学的课 正在进行 (14) 2026年春 校内公开 教务开课 "
                        "大学物理学基础 Ⅰ 学院：大学物理及实验 张连众 279次 90人"
                    ),
                    "attrs": "id=root",
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [],
                },
                {
                    "href": "",
                    "absolute_href": "",
                    "text": "2026年春 校内公开 教务开课 大学物理学基础 Ⅰ 学院：大学物理及实验 张连众 279次 90人",
                    "attrs": (
                        "class=aia_course_card xy_animation__hover_upward_floating "
                        "data-xy-click-pt-name=大学物理学基础 Ⅰ "
                        "data-xy-click-pt=enter-course data-xy-click-pt-element-type=3"
                    ),
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [],
                },
            ]
        )

        self.assertEqual(len(cards), 1)
        self.assertIn("data-xy-click-pt=enter-course", cards[0]["attrs"])

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

    def test_click_step_deadline_uses_one_minute_window(self) -> None:
        self.assertEqual(CLICK_DISCOVERY_STEP_TIMEOUT_MS, 60_000)
        with patch.object(xiaoya_discovery.monotonic_time, "monotonic", return_value=100.0):
            self.assertEqual(click_step_deadline(), 160.0)

    def test_click_discovery_refreshes_deadline_between_page_actions(self) -> None:
        page = object()
        deadlines: list[float] = []
        emitted: list[str] = []
        card = {
            "href": "",
            "absolute_href": "",
            "text": "2026年春 校内公开 教务开课 结构化学 学院：化学学院",
            "attrs": "data-id=6902426124991620398",
            "ancestor_texts": [],
            "ancestor_attrs": [],
            "title_texts": ["结构化学"],
        }

        def fake_open(_page, _url, *, page_no: int, deadline: float | None = None) -> bool:
            self.assertEqual(page_no, 1)
            self.assertIsNotNone(deadline)
            deadlines.append(float(deadline))
            return True

        with (
            patch.object(xiaoya_discovery, "click_step_deadline", side_effect=[101.0, 202.0, 303.0, 404.0]),
            patch.object(xiaoya_discovery, "open_xiaoya_course_list_page", side_effect=fake_open),
            patch.object(xiaoya_discovery, "read_xiaoya_body_text", return_value="page-one"),
            patch.object(xiaoya_discovery, "wait_for_xiaoya_click_course_candidates", return_value=[card]),
            patch.object(xiaoya_discovery, "click_next_xiaoya_course_list_page", return_value=False),
            patch.object(xiaoya_discovery, "xiaoya_course_pagination_snapshot", return_value="no next"),
        ):
            courses = resolve_course_cards_by_clicking(
                page,
                mycourse_url="https://nankai.ai-augmented.com/app/jx-web/mycourse",
                existing_course_ids=set(),
                emit=emitted.append,
            )

        self.assertEqual(deadlines, [101.0, 202.0, 303.0, 404.0])
        self.assertEqual(len(courses), 1)
        self.assertEqual(extract_course_id_from_raw(courses[0]), "6902426124991620398")
        self.assertFalse(any("reason=timeout" in message for message in emitted))

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
