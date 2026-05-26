from __future__ import annotations

import json
import re
import time as monotonic_time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from homework_watcher.config_loader import KnownCourseConfig
from homework_watcher.debug_dump import safe_name, sanitize_debug_text
from homework_watcher.settings import Settings
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


XIAOYA_DEFAULT_MYCOURSE_URL = "https://nankai.ai-augmented.com/app/jx-web/mycourse"
COURSE_ID_PATTERNS = (
    re.compile(r"/mycourse/(?P<course_id>\d{12,})(?:[/?#\s\"'>]|$)"),
    re.compile(r"\bdata-(?:course-?)?id=[\"']?(?P<course_id>\d{12,})", re.IGNORECASE),
    re.compile(r"course[_-]?id[\"'\s:=]+[\"']?(?P<course_id>\d{12,})", re.IGNORECASE),
    re.compile(r"courseId[\"'\s:=]+[\"']?(?P<course_id>\d{12,})"),
    re.compile(r"\b(?P<course_id>\d{12,})\b"),
)
COURSE_ID_KEYWORDS = (
    "courseid",
    "course_id",
    "course-id",
    "course_idstr",
    "courseids",
    "course",
    "id",
)
COURSE_NAME_KEYS = (
    "coursename",
    "course_name",
    "course-name",
    "coursetitle",
    "course_title",
    "course-title",
    "name",
    "title",
    "displayname",
    "display_name",
)
COURSE_URL_KEYS = (
    "url",
    "href",
    "link",
    "path",
    "route",
    "resourceurl",
    "resource_url",
    "taskurl",
    "task_url",
)
COURSE_CONTEXT_KEYS = (
    "college",
    "school",
    "academy",
    "department",
    "teacher",
    "term",
    "semester",
    "teaching",
    "class",
    "open",
)
MAX_NETWORK_JSON_BYTES = 2_000_000
MAX_NETWORK_PAYLOADS = 40
DOM_COURSE_WAIT_MS = 6000
NETWORK_SETTLE_MS = 2000
COURSE_ACTION_PHRASES = (
    "进入课程",
    "查看详情",
    "查看课程",
    "开始学习",
    "继续学习",
    "进入",
)
COURSE_ACTION_WORDS = {"查看", "任务", "作业", "学习"}
COURSE_NAME_NOISE = {
    "我的课程",
    "课程列表",
    "全部课程",
    "所有的课",
    "课程",
    "暂无课程",
    "没有课程",
    "更多",
    "如何使用本系统",
}
SENSITIVE_QUERY_KEYS = ("token", "authorization", "auth", "password", "secret", "session", "cookie")


@dataclass(frozen=True)
class CourseMergeResult:
    courses: list[KnownCourseConfig]
    known_count: int
    discovered_count: int
    duplicates_count: int


class XiaoyaCourseDiscoverer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def discover(
        self,
        page: Page,
        *,
        mycourse_url: str = XIAOYA_DEFAULT_MYCOURSE_URL,
        scan_id: str = "manual",
        emit: Callable[[str], None] | None = None,
    ) -> list[KnownCourseConfig]:
        url = mycourse_url or XIAOYA_DEFAULT_MYCOURSE_URL
        self._emit(emit, f"[xiaoya-discover] start url={sanitize_url_for_log(url)}")
        network_payloads: list[dict[str, str]] = []

        def collect_response(response: Any) -> None:
            collect_xiaoya_network_payload(response, mycourse_url=url, payloads=network_payloads)

        page.on("response", collect_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            self._emit(emit, "[xiaoya-discover] timeout loading mycourse page")
            dump_xiaoya_discovery_debug(page, self.settings, scan_id=scan_id, reason="load-timeout")
            return []

        self._emit(emit, f"[xiaoya-discover] page loaded url={sanitize_url_for_log(page.url)}")
        page.wait_for_timeout(NETWORK_SETTLE_MS)
        network_candidates = raw_course_candidates_from_network_payloads(network_payloads, mycourse_url=url)
        if network_candidates:
            self._emit(emit, f"[xiaoya-discover] network course candidates count={len(network_candidates)}")
        state_candidates = evaluate_course_state_candidates(page)
        if state_candidates:
            self._emit(emit, f"[xiaoya-discover] state course candidates count={len(state_candidates)}")
        if network_candidates or state_candidates:
            dom_candidates = evaluate_course_candidates(page)
        else:
            dom_candidates = wait_for_xiaoya_course_candidates(page, timeout_ms=DOM_COURSE_WAIT_MS)
        raw_candidates = dedupe_raw_course_candidates(dom_candidates + network_candidates + state_candidates)
        self._emit(emit, f"[xiaoya-discover] raw course candidates count={len(raw_candidates)}")

        discovered: list[KnownCourseConfig] = []
        seen_course_ids: set[str] = set()
        skipped_no_id = 0

        def append_candidates(candidates: list[dict[str, Any]]) -> None:
            nonlocal skipped_no_id
            for raw in dedupe_raw_course_candidates(candidates):
                course_id = extract_course_id_from_raw(raw)
                text_hint = raw_text_hint(raw)
                if not course_id:
                    skipped_no_id += 1
                    if skipped_no_id <= 20:
                        self._emit(
                            emit,
                            f"[xiaoya-discover] skipped reason=no_course_id text={sanitize_log_value(text_hint)}",
                        )
                    continue

                course = extract_course_name_from_raw(raw, course_id=course_id)
                if not course:
                    self._emit(
                        emit,
                        f"[xiaoya-discover] skipped reason=no_course_name course_id={course_id} "
                        f"text={sanitize_log_value(text_hint)}",
                    )
                    continue
                if len(course) > 80:
                    self._emit(
                        emit,
                        f"[xiaoya-discover] skipped reason=suspicious_course_name course_id={course_id} "
                        f"text={sanitize_log_value(course)}",
                    )
                    continue
                if course_id in seen_course_ids:
                    self._emit(emit, f"[xiaoya-discover] duplicate course_id skipped course_id={course_id}")
                    continue

                task_url = build_xiaoya_task_url(url, course_id, href=raw_href(raw))
                candidate = KnownCourseConfig(
                    course=course,
                    course_id=course_id,
                    task_url=task_url,
                    source="discovered",
                )
                discovered.append(candidate)
                seen_course_ids.add(course_id)
                self._emit(
                    emit,
                    f"[xiaoya-discover] candidate course={sanitize_log_value(course)} "
                    f"course_id={course_id} task_url={sanitize_url_for_log(task_url)}",
                )

        append_candidates(raw_candidates)

        if not discovered:
            dump_xiaoya_discovery_debug(page, self.settings, scan_id=scan_id, reason="no-courses")
        self._emit(emit, f"[xiaoya-discover] discovered count={len(discovered)}")
        return discovered

    def _emit(self, emit: Callable[[str], None] | None, message: str) -> None:
        if emit is not None:
            emit(message)


def merge_xiaoya_courses(
    known_courses: list[KnownCourseConfig],
    discovered_courses: list[KnownCourseConfig],
    *,
    emit: Callable[[str], None] | None = None,
) -> CourseMergeResult:
    courses: list[KnownCourseConfig] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    duplicates = 0

    for course in known_courses:
        normalized = replace(course, source=course.source or "known")
        courses.append(normalized)
        if normalized.course_id:
            seen_ids.add(normalized.course_id)
        seen_names.add(normalize_course_name(normalized.course))

    for course in discovered_courses:
        name_key = normalize_course_name(course.course)
        if course.course_id and course.course_id in seen_ids:
            duplicates += 1
            if emit is not None:
                emit(f"[xiaoya-discover] duplicate course_id skipped course_id={course.course_id}")
            continue
        if not course.course_id and name_key in seen_names:
            duplicates += 1
            if emit is not None:
                emit(f"[xiaoya-discover] duplicate course skipped course={sanitize_log_value(course.course)}")
            continue
        normalized = replace(course, source=course.source or "discovered")
        courses.append(normalized)
        if normalized.course_id:
            seen_ids.add(normalized.course_id)
        seen_names.add(name_key)

    return CourseMergeResult(
        courses=courses,
        known_count=len(known_courses),
        discovered_count=len(discovered_courses),
        duplicates_count=duplicates,
    )


def xiaoya_course_to_dict(course: KnownCourseConfig) -> dict[str, str]:
    return {
        "course": course.course,
        "course_id": course.course_id,
        "task_url": course.task_url,
        "source": course.source,
    }


def wait_for_xiaoya_course_candidates(page: Page, *, timeout_ms: int) -> list[dict[str, Any]]:
    deadline = monotonic_time.monotonic() + max(timeout_ms, 1000) / 1000
    last_candidates: list[dict[str, Any]] = []
    while monotonic_time.monotonic() < deadline:
        last_candidates = evaluate_course_candidates(page)
        if any(extract_course_id_from_raw(candidate) for candidate in last_candidates):
            return last_candidates
        if any(looks_like_course_card_text(raw_text_hint(candidate)) for candidate in last_candidates):
            return last_candidates
        page.wait_for_timeout(500)
    return last_candidates


def collect_xiaoya_network_payload(response: Any, *, mycourse_url: str, payloads: list[dict[str, str]]) -> None:
    if len(payloads) >= MAX_NETWORK_PAYLOADS:
        return
    try:
        response_url = str(getattr(response, "url", "") or "")
        if not response_url:
            return
        if not same_origin_or_xiaoya(response_url, mycourse_url):
            return
        content_type = str((getattr(response, "headers", {}) or {}).get("content-type") or "").lower()
        lower_url = response_url.lower()
        if "json" not in content_type and not any(
            marker in lower_url for marker in ("course", "mycourse", "jx-web", "teaching")
        ):
            return
        body = response.text()
        if not body or len(body) > MAX_NETWORK_JSON_BYTES:
            return
        if body.lstrip()[:1] not in ("{", "["):
            return
        payloads.append({"url": response_url, "body": body})
    except Exception:
        return


def raw_course_candidates_from_network_payloads(
    payloads: list[dict[str, str]], *, mycourse_url: str
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        try:
            data = json.loads(payload.get("body") or "")
        except (TypeError, json.JSONDecodeError):
            continue
        for mapping in iter_json_mappings(data):
            course_id = extract_course_id_from_mapping(mapping)
            if not course_id:
                continue
            course_name = extract_course_name_from_mapping(mapping, course_id=course_id)
            if not course_name:
                continue
            href = extract_course_href_from_mapping(mapping, mycourse_url=mycourse_url, course_id=course_id)
            candidates.append(
                {
                    "href": href,
                    "absolute_href": urljoin(mycourse_url, href) if href else "",
                    "text": course_name,
                    "attrs": f"network_url={payload.get('url', '')} course_id={course_id}",
                    "ancestor_texts": [],
                    "ancestor_attrs": [],
                    "title_texts": [course_name],
                }
            )
    return dedupe_raw_course_candidates(candidates)


def iter_json_mappings(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    mappings: list[dict[str, Any]] = []
    if isinstance(value, dict):
        mappings.append(value)
        for child in value.values():
            mappings.extend(iter_json_mappings(child, depth=depth + 1))
    elif isinstance(value, list):
        for child in value[:1000]:
            mappings.extend(iter_json_mappings(child, depth=depth + 1))
    return mappings


def extract_course_id_from_mapping(mapping: dict[str, Any]) -> str:
    direct_values: list[str] = []
    has_course_hint = mapping_has_course_hint(mapping)
    for key, value in mapping.items():
        key_norm = normalize_json_key(key)
        value_text = stringify_json_scalar(value)
        if not value_text:
            continue
        if key_norm == "id" and not has_course_hint:
            continue
        if key_norm in COURSE_ID_KEYWORDS or ("course" in key_norm and "id" in key_norm):
            direct_values.append(value_text)
    course_id = extract_course_id(*direct_values)
    if course_id:
        return course_id

    url_values = []
    for key, value in mapping.items():
        key_norm = normalize_json_key(key)
        value_text = stringify_json_scalar(value)
        if key_norm in COURSE_URL_KEYS or "url" in key_norm or "href" in key_norm or "link" in key_norm:
            url_values.append(value_text)
    course_id = extract_course_id(*url_values)
    if course_id:
        return course_id

    if has_course_hint:
        return extract_course_id(json.dumps(mapping, ensure_ascii=False)[:4000])
    return ""


def extract_course_name_from_mapping(mapping: dict[str, Any], *, course_id: str) -> str:
    candidates: list[str] = []
    for key, value in mapping.items():
        key_norm = normalize_json_key(key)
        value_text = stringify_json_scalar(value)
        if not value_text:
            continue
        if key_norm in COURSE_NAME_KEYS or (
            "course" in key_norm and ("name" in key_norm or "title" in key_norm)
        ):
            candidates.extend(course_name_candidates(value_text, course_id=course_id))
    if not candidates:
        return ""
    return max(candidates, key=lambda name: course_name_score(name, 0))


def extract_course_href_from_mapping(mapping: dict[str, Any], *, mycourse_url: str, course_id: str) -> str:
    for key, value in mapping.items():
        key_norm = normalize_json_key(key)
        value_text = stringify_json_scalar(value)
        if not value_text:
            continue
        if key_norm in COURSE_URL_KEYS or "url" in key_norm or "href" in key_norm or "link" in key_norm:
            if extract_course_id(value_text) == course_id:
                return urljoin(mycourse_url, value_text)
    return build_xiaoya_task_url(mycourse_url, course_id)


def stringify_json_scalar(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    if value is None:
        return ""
    return str(value).strip()


def normalize_json_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", str(key).strip().lower())


def mapping_has_course_hint(mapping: dict[str, Any]) -> bool:
    context_key_hits = 0
    for key, value in mapping.items():
        key_norm = normalize_json_key(key)
        value_text = stringify_json_scalar(value)
        if "course" in key_norm:
            return True
        if any(marker in key_norm for marker in COURSE_CONTEXT_KEYS):
            context_key_hits += 1
        if "/mycourse/" in value_text or "教务开课" in value_text or "校内公开" in value_text:
            return True
        if looks_like_course_card_text(value_text):
            return True
    if context_key_hits >= 2 and extract_course_id(json.dumps(mapping, ensure_ascii=False)[:4000]):
        return True
    return False


def same_origin_or_xiaoya(response_url: str, mycourse_url: str) -> bool:
    response_host = urlparse(response_url).netloc
    mycourse_host = urlparse(mycourse_url or XIAOYA_DEFAULT_MYCOURSE_URL).netloc
    return not response_host or response_host == mycourse_host or response_host.endswith(".ai-augmented.com")


def expected_course_count_from_text(text: str) -> int:
    match = re.search(r"正在进行\s*[（(]\s*(?P<count>\d{1,3})\s*[）)]", text)
    if not match:
        return 0
    return int(match.group("count"))


def evaluate_course_candidates(page: Page) -> list[dict[str, Any]]:
    try:
        raw = page.evaluate(
            """
            () => {
              const compact = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const attrSummary = element => Array.from(element.attributes || [])
                .map(attr => `${attr.name}=${attr.value}`)
                .join(' ');
              const readableText = element => compact(
                element.innerText || element.textContent || element.getAttribute('title') || ''
              );
              const titleTexts = element => Array.from(
                element.querySelectorAll('h1,h2,h3,h4,[class*="title"],[class*="Title"],[class*="name"],[class*="Name"],[title]')
              )
                .slice(0, 16)
                .map(node => compact(node.innerText || node.textContent || node.getAttribute('title') || ''))
                .filter(Boolean);
              const hasCourseIdHint = value => (
                /\\/mycourse\\/\\d{12,}|course[_-]?id|data-(?:course-?)?id=|\\b\\d{12,}\\b/i
              ).test(value || '');
              const isCourseish = value => /课程|course/i.test(value || '');
              const elements = new Set();
              const selectors = [
                'a[href*="/mycourse"]',
                '[href*="/mycourse"]',
                '[onclick*="mycourse"]',
                '[data-course-id]',
                '[data-courseid]',
                '[data-id]',
                '[data-url]',
                '[data-href]',
                '[role="button"]',
                'button',
                'a',
                '[class*="course"]',
                '[class*="Course"]'
              ];
              for (const selector of selectors) {
                for (const element of document.querySelectorAll(selector)) {
                  const combined = [
                    element.getAttribute('href') || '',
                    element.href || '',
                    attrSummary(element),
                    readableText(element)
                  ].join(' ');
                  if (hasCourseIdHint(combined) || isCourseish(combined)) {
                    elements.add(element);
                  }
                }
              }
              return Array.from(elements)
                .slice(0, 400)
                .map(element => {
                  const ancestors = [];
                  let current = element;
                  for (let depth = 0; current && depth < 5; depth += 1) {
                    ancestors.push({
                      text: readableText(current).slice(0, 1200),
                      attrs: attrSummary(current).slice(0, 1200),
                      title_texts: titleTexts(current),
                    });
                    current = current.parentElement;
                  }
                  return {
                    href: element.getAttribute('href') || '',
                    absolute_href: element.href || '',
                    text: readableText(element).slice(0, 800),
                    attrs: attrSummary(element).slice(0, 1200),
                    ancestor_texts: ancestors.map(item => item.text).filter(Boolean),
                    ancestor_attrs: ancestors.map(item => item.attrs).filter(Boolean),
                    title_texts: ancestors.flatMap(item => item.title_texts).slice(0, 30),
                  };
                });
            }
            """
        )
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def evaluate_course_state_candidates(page: Page) -> list[dict[str, Any]]:
    try:
        raw = page.evaluate(
            """
            () => {
              const compact = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const sensitiveKey = key => /token|authorization|auth|password|secret|session|cookie/i.test(String(key || ''));
              const courseId = value => {
                const match = String(value || '').match(/\\b\\d{12,}\\b/);
                return match ? match[0] : '';
              };
              const looksCourseName = value => {
                const text = compact(value);
                if (!text || text.length < 2 || text.length > 100) return false;
                if (!/[\\u4e00-\\u9fff]/.test(text)) return false;
                if (/登录|密码|验证码|用户协议|隐私政策|收藏的课|访问的课|加入课程|创建课程/.test(text)) return false;
                return true;
              };
              const looksCourseContext = value => /学院[:：]|校内公开|教务开课|202\\d年|课程|course|mycourse|resource|teaching/i.test(value || '');
              const candidates = [];
              const seen = new WeakSet();
              let visited = 0;
              const pushCandidate = (id, scalars) => {
                if (!id) return;
                const entries = Object.entries(scalars).filter(([key]) => !sensitiveKey(key));
                const nameEntry = entries.find(([key, value]) => {
                  const keyText = String(key).toLowerCase();
                  return /course.*name|course.*title|name|title/.test(keyText) && looksCourseName(value);
                }) || entries.find(([, value]) => looksCourseName(value));
                const textParts = entries
                  .map(([, value]) => compact(value))
                  .filter(value => looksCourseName(value) || /学院[:：]|校内公开|教务开课|202\\d年/.test(value))
                  .slice(0, 8);
                const text = compact([nameEntry ? nameEntry[1] : '', ...textParts].filter(Boolean).join(' '));
                if (!text) return;
                candidates.push({
                  href: '',
                  absolute_href: '',
                  text: text.slice(0, 800),
                  attrs: `state course_id=${id}`,
                  ancestor_texts: textParts.slice(0, 4),
                  ancestor_attrs: [],
                  title_texts: nameEntry ? [compact(nameEntry[1])] : textParts.slice(0, 2),
                });
              };
              const walk = (value, depth = 0) => {
                if (!value || typeof value !== 'object' || depth > 5 || visited > 6000) return;
                if (seen.has(value)) return;
                seen.add(value);
                visited += 1;
                const scalars = {};
                let combined = '';
                for (const key of Object.keys(value).slice(0, 80)) {
                  if (sensitiveKey(key)) continue;
                  let child;
                  try { child = value[key]; } catch { continue; }
                  if (child == null) continue;
                  if (typeof child === 'string' || typeof child === 'number' || typeof child === 'boolean') {
                    const text = compact(child);
                    if (!text || text.length > 500) continue;
                    scalars[key] = text;
                    combined += ` ${key}=${text}`;
                  }
                }
                const id = courseId(combined);
                if (id && (looksCourseContext(combined) || Object.keys(scalars).some(key => /course|teaching|class|semester|term|college|school/i.test(key)))) {
                  pushCandidate(id, scalars);
                }
                for (const key of Object.keys(value).slice(0, 80)) {
                  if (sensitiveKey(key)) continue;
                  let child;
                  try { child = value[key]; } catch { continue; }
                  if (child && typeof child === 'object') walk(child, depth + 1);
                }
              };
              const roots = [];
              for (const element of Array.from(document.querySelectorAll('#app, [class*="course"], [class*="Course"], article, li, section, div')).slice(0, 260)) {
                for (const key of Object.keys(element)) {
                  if (/^__vue|^__react|reactFiber|reactProps|vue/i.test(key)) {
                    try { roots.push(element[key]); } catch {}
                  }
                }
              }
              for (const key of Object.keys(window).slice(0, 3000)) {
                if (/^__INITIAL|^__NUXT|^__NEXT|^__APOLLO|store|pinia|redux|app/i.test(key) && !sensitiveKey(key)) {
                  try {
                    const value = window[key];
                    if (value && typeof value === 'object') roots.push(value);
                  } catch {}
                }
              }
              for (const root of roots.slice(0, 300)) walk(root);
              return candidates.slice(0, 200);
            }
            """
        )
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def extract_course_id_from_raw(raw: dict[str, Any]) -> str:
    values: list[str] = [
        str(raw.get("href") or ""),
        str(raw.get("absolute_href") or ""),
        str(raw.get("attrs") or ""),
        str(raw.get("text") or ""),
    ]
    values.extend(str(value) for value in raw.get("ancestor_attrs") or [])
    values.extend(str(value) for value in raw.get("ancestor_texts") or [])
    return extract_course_id(*values)


def extract_course_id(*values: str) -> str:
    for value in values:
        for pattern in COURSE_ID_PATTERNS:
            match = pattern.search(value or "")
            if match:
                return match.group("course_id").strip()
    return ""


def extract_course_name_from_raw(raw: dict[str, Any], *, course_id: str) -> str:
    blocks: list[str] = []
    blocks.extend(str(value) for value in raw.get("title_texts") or [])
    blocks.append(str(raw.get("text") or ""))
    blocks.extend(str(value) for value in raw.get("ancestor_texts") or [])

    best_name = ""
    best_score = -1
    for block_index, block in enumerate(blocks):
        for name in course_name_candidates(block, course_id=course_id):
            score = course_name_score(name, block_index)
            if score > best_score:
                best_name = name
                best_score = score
    return best_name


def course_name_candidates(text: str, *, course_id: str) -> list[str]:
    if not text:
        return []
    raw_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    if not raw_lines:
        raw_lines = [text]
    candidates: list[str] = []
    for line in raw_lines:
        for fragment in course_name_fragments(line):
            name = normalize_course_name(fragment)
            if valid_course_name(name, course_id=course_id):
                candidates.append(name)
    compact_block = normalize_course_name(text)
    if valid_course_name(compact_block, course_id=course_id):
        candidates.append(compact_block)
    return dedupe_names(candidates)


def course_name_fragments(line: str) -> list[str]:
    fragments = [line]
    for marker in (
        " 学院：",
        " 学院:",
        " 院系：",
        " 院系:",
        " 教师：",
        " 教师:",
        " 老师：",
        " 老师:",
        " 校内公开",
        " 教务开课",
        " 访问量",
    ):
        if marker in line:
            fragments.append(line.split(marker, 1)[0])
    stats_match = re.search(r"(?P<name>.+?)\s+\d+次\s+\d+人\b", line)
    if stats_match:
        fragments.append(stats_match.group("name"))
    term_match = re.search(r"(?P<name>.+?)\s+202\d年(?:春|夏|秋|冬)?\b", line)
    if term_match:
        fragments.append(term_match.group("name"))
    return fragments


def normalize_course_name(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" /\\|·-_:：")
    for phrase in COURSE_ACTION_PHRASES:
        text = text.replace(phrase, " ")
    for word in COURSE_ACTION_WORDS:
        text = re.sub(rf"(^|\s){re.escape(word)}(\s|$)", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" /\\|·-_:：")
    return text


def valid_course_name(name: str, *, course_id: str) -> bool:
    if not name or name in COURSE_ACTION_WORDS or name in COURSE_NAME_NOISE:
        return False
    if len(name) < 2 or len(name) > 200:
        return False
    if course_id and course_id in name:
        return False
    if "http://" in name or "https://" in name or "/mycourse/" in name:
        return False
    if re.search(r"\d{4}-\d{2}-\d{2}", name):
        return False
    if re.fullmatch(r"[\d\s./_-]+", name):
        return False
    if looks_like_course_nav_text(name):
        return False
    return True


def looks_like_course_nav_text(text: str) -> bool:
    return bool(re.search(r"我的课程.*收藏的课.*访问的课|加入课程.*创建课程.*正在进行", text))


def looks_like_course_card_text(text: str) -> bool:
    compact = normalize_course_name(text)
    if not compact or len(compact) > 1000:
        return False
    if looks_like_course_nav_text(compact):
        return False
    return bool(re.search(r"学院[:：]|校内公开|教务开课|\d+次\s+\d+人|202\d年", compact))


def course_name_score(name: str, block_index: int) -> int:
    score = 100
    if re.search(r"[\u4e00-\u9fff]", name):
        score += 30
    if 4 <= len(name) <= 32:
        score += 20
    if block_index == 0:
        score += 35
    elif block_index <= 2:
        score += 15
    score -= min(len(name), 80) // 2
    return score


def build_xiaoya_task_url(mycourse_url: str, course_id: str, *, href: str = "") -> str:
    source_url = urljoin(mycourse_url or XIAOYA_DEFAULT_MYCOURSE_URL, href) if href else (
        mycourse_url or XIAOYA_DEFAULT_MYCOURSE_URL
    )
    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        parsed = urlparse(urljoin(XIAOYA_DEFAULT_MYCOURSE_URL, source_url))
    prefix_match = re.search(r"(?P<prefix>.*?/mycourse)(?:/|$)", parsed.path)
    prefix = prefix_match.group("prefix") if prefix_match else "/app/jx-web/mycourse"
    return urlunparse((parsed.scheme, parsed.netloc, f"{prefix.rstrip('/')}/{course_id}/task", "", "", ""))


def raw_href(raw: dict[str, Any]) -> str:
    return str(raw.get("absolute_href") or raw.get("href") or "")


def raw_text_hint(raw: dict[str, Any]) -> str:
    pieces = [str(raw.get("text") or "")]
    pieces.extend(str(value) for value in raw.get("ancestor_texts") or [])
    return " ".join(piece for piece in pieces if piece).strip()


def dedupe_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def dedupe_raw_course_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for candidate in candidates:
        course_id = extract_course_id_from_raw(candidate)
        key = (
            course_id,
            sanitize_url_for_log(raw_href(candidate)),
            sanitize_log_value(raw_text_hint(candidate), limit=120),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def dump_xiaoya_discovery_debug(page: Page, settings: Settings, *, scan_id: str, reason: str) -> Path:
    directory = Path(settings.debug_dump_dir) / scan_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"xiaoya-discover-{safe_name(reason)}.json"
    try:
        title = page.title(timeout=2000)
    except Exception:
        title = ""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        text = ""
    try:
        links = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href]'))
              .slice(0, 240)
              .map(link => ({
                href: link.href || link.getAttribute('href') || '',
                text: (link.innerText || link.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300)
              }))
            """
        )
    except Exception:
        links = []
    safe_links = []
    if isinstance(links, list):
        for item in links:
            if not isinstance(item, dict):
                continue
            safe_links.append(
                {
                    "href": sanitize_url_for_log(str(item.get("href") or "")),
                    "text": sanitize_debug_text(str(item.get("text") or ""))[:300],
                }
            )
    payload = {
        "current_url": sanitize_url_for_log(getattr(page, "url", "")),
        "title": sanitize_debug_text(title),
        "visible_text": sanitize_debug_text(text[:5000]),
        "links": safe_links,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def sanitize_log_value(value: str, *, limit: int = 160) -> str:
    return sanitize_debug_text(re.sub(r"\s+", " ", value).strip())[:limit]


def sanitize_url_for_log(url: str) -> str:
    parsed = urlparse(sanitize_debug_text(url))
    fragment = parsed.fragment
    if any(sensitive in fragment.lower() for sensitive in SENSITIVE_QUERY_KEYS):
        fragment = "<redacted>"
    if not parsed.query:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", fragment))
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(sensitive in key.lower() for sensitive in SENSITIVE_QUERY_KEYS):
            query.append((key, "<redacted>"))
        else:
            query.append((key, value))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), fragment))
