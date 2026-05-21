from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .candidates import AssignmentCandidate
from .config_loader import load_platform_configs
from .database import (
    UpsertStats,
    assignment_to_dict,
    create_session_factory,
    init_db,
    list_todos,
    upsert_assignments,
)
from .git_utils import git_commit
from .logging_utils import get_scan_logger
from .scanners import FakeScanner, PlatformScanner, ScannerContext
from .settings import Settings, load_settings


@dataclass(frozen=True)
class ScanResult:
    scan_id: str
    started_at: datetime
    finished_at: datetime
    candidates_count: int
    normalized_count: int
    filtered_count: int
    upsert_stats: UpsertStats
    errors: list[str]
    todos: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "scan_id": self.scan_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "candidates_count": self.candidates_count,
            "normalized_count": self.normalized_count,
            "filtered_count": self.filtered_count,
            "db": {
                "inserted": self.upsert_stats.inserted,
                "updated": self.upsert_stats.updated,
                "skipped": self.upsert_stats.skipped,
            },
            "errors": self.errors,
            "todos": self.todos,
        }


class ScanService:
    def __init__(
        self,
        settings: Settings | None = None,
        scanners: Iterable[PlatformScanner] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.configs = load_platform_configs(self.settings.config_path)
        self.logger = get_scan_logger(self.settings)
        self.session_factory = create_session_factory(self.settings)
        self.scanners = {scanner.platform_key: scanner for scanner in (scanners or [FakeScanner()])}

    def run_scan(self, platforms: list[str] | None = None, progress=None) -> ScanResult:
        init_db(self.settings)
        scan_id = f"scan-{uuid.uuid4().hex[:12]}"
        started_at = datetime.now()
        self._log(
            scan_id,
            "scan started version=%s commit=%s platforms=%s",
            self.settings.app_version,
            git_commit(),
            platforms or "enabled",
        )

        platform_keys = self._platform_keys(platforms)
        all_candidates: list[AssignmentCandidate] = []
        errors: list[str] = []

        for platform_key in platform_keys:
            scanner = self.scanners.get(platform_key)
            if scanner is None:
                message = f"platform {platform_key} has no scanner"
                errors.append(message)
                self._log(scan_id, message)
                continue

            self._log(scan_id, "platform start %s scanner=%s", platform_key, type(scanner).__name__)
            try:
                context = ScannerContext(
                    scan_id=scan_id,
                    platform_key=platform_key,
                    platform_config=self.configs.get(platform_key),
                    progress=progress,
                )
                candidates = scanner.scan(context)
                self._log(
                    scan_id,
                    "platform end %s count=%s titles=%s",
                    platform_key,
                    len(candidates),
                    _safe_titles(candidates),
                )
                all_candidates.extend(candidates)
            except Exception as exc:  # noqa: BLE001 - platform isolation is intentional.
                message = f"{platform_key}: {type(exc).__name__}: {exc}"
                errors.append(message)
                self._log(scan_id, "platform failed %s", message)

        normalized = list(all_candidates)
        filtered = [candidate for candidate in normalized if not is_fake_course_summary(candidate)]
        self._log(
            scan_id,
            "before db upsert count=%s titles=%s",
            len(filtered),
            _safe_titles(filtered),
        )

        with self.session_factory() as session:
            stats = upsert_assignments(session, filtered)
            todos = [assignment_to_dict(item) for item in list_todos(session)]

        self._log(
            scan_id,
            "after db upsert inserted=%s updated=%s skipped=%s",
            stats.inserted,
            stats.updated,
            stats.skipped,
        )
        self._log(scan_id, "final todo count=%s titles=%s", len(todos), _safe_dict_titles(todos))

        result = ScanResult(
            scan_id=scan_id,
            started_at=started_at,
            finished_at=datetime.now(),
            candidates_count=len(all_candidates),
            normalized_count=len(normalized),
            filtered_count=len(filtered),
            upsert_stats=stats,
            errors=errors,
            todos=todos,
        )
        LAST_SCAN_RESULTS.append(result)
        del LAST_SCAN_RESULTS[:-5]
        return result

    def _platform_keys(self, platforms: list[str] | None) -> list[str]:
        if platforms:
            return platforms
        configured = [
            key
            for key, value in self.configs.items()
            if value.enabled and key in self.scanners
        ]
        return configured or ["fake"]

    def _log(self, scan_id: str, message: str, *args) -> None:
        self.logger.info("[scan:%s] " + message, scan_id, *args)


LAST_SCAN_RESULTS: list[ScanResult] = []


def latest_scan_result() -> ScanResult | None:
    return LAST_SCAN_RESULTS[-1] if LAST_SCAN_RESULTS else None


def is_fake_course_summary(candidate: AssignmentCandidate) -> bool:
    if candidate.platform != "小雅":
        return False
    if candidate.course.strip() != candidate.title.strip():
        return False
    if candidate.status_normalized != "unknown":
        return False
    return "mycourse" in candidate.url or not candidate.url


def _safe_titles(candidates: Iterable[AssignmentCandidate]) -> str:
    return json.dumps([candidate.sanitized_title() for candidate in candidates], ensure_ascii=False)


def _safe_dict_titles(items: Iterable[dict[str, object]]) -> str:
    keys = ("platform", "course", "title", "status_raw", "due_at")
    return json.dumps([{key: item.get(key) for key in keys} for item in items], ensure_ascii=False)
