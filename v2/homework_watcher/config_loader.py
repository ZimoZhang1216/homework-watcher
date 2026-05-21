from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .settings import resolve_path


@dataclass(frozen=True)
class KnownCourseConfig:
    course: str
    course_id: str
    task_url: str


@dataclass(frozen=True)
class PlatformConfig:
    name: str
    enabled: bool
    base_url: str
    known_courses: list[KnownCourseConfig]


def load_platform_configs(path: Path) -> dict[str, PlatformConfig]:
    config_path = resolve_path(path)
    if not config_path.exists():
        return {}
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid platform config root: {config_path}")
    return {
        str(name): parse_platform_config(str(name), value or {})
        for name, value in raw.items()
        if isinstance(value, dict)
    }


def parse_platform_config(name: str, raw: dict[str, Any]) -> PlatformConfig:
    courses = []
    for item in raw.get("known_courses", []) or []:
        if not isinstance(item, dict):
            continue
        course = str(item.get("course") or "").strip()
        course_id = str(item.get("course_id") or "").strip()
        task_url = str(item.get("task_url") or "").strip()
        if course and task_url:
            courses.append(KnownCourseConfig(course=course, course_id=course_id, task_url=task_url))
    return PlatformConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url") or "").strip(),
        known_courses=courses,
    )
