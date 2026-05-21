from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from homework_watcher.candidates import AssignmentCandidate
from homework_watcher.config_loader import PlatformConfig


ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class ScannerContext:
    scan_id: str
    platform_key: str
    platform_config: PlatformConfig | None
    progress: ProgressCallback | None = None

    def emit(self, percent: int, message: str) -> None:
        if self.progress:
            self.progress(percent, message)


class PlatformScanner(Protocol):
    platform_key: str

    def scan(self, context: ScannerContext) -> list[AssignmentCandidate]:
        ...
