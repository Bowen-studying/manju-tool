"""Common stage adapter result contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StageResult:
    status: str
    stage_run_id: str
    reason_code: str = ""
    message: str = ""
    artifacts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    authority_path: str = ""
    authority_hash: str = ""
    authority_files: tuple[dict[str, str], ...] = field(default_factory=tuple)
