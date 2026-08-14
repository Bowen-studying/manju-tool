"""Project state loading, validation, and projection persistence."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from manju.production.events import EventStore, HmacKeyProvider
from manju.production.models import (
    PROJECT_SCHEMA_VERSION,
    ProductionError,
    ReasonCode,
    fingerprint,
)
from manju.production.paths import ProjectPaths
from manju.production.reducer import reduce_events
from manju.utils.runtime import atomic_write_json, read_json


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProjectStore:
    def __init__(self, project_file: str, *, key_provider: HmacKeyProvider | None = None):
        self.paths = ProjectPaths.from_project_file(project_file)
        self.events = EventStore(self.paths.events_file, key_provider=key_provider)

    def load_project(self) -> dict[str, Any]:
        project = read_json(self.paths.project_file)
        if not isinstance(project, dict):
            raise ProductionError(ReasonCode.SOURCE_MISSING.value, "project.json 不存在或无法读取")
        if project.get("schema_version") != PROJECT_SCHEMA_VERSION:
            raise ProductionError(
                ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value,
                f"不支持 project schema: {project.get('schema_version')!r}",
            )
        source = project.get("source")
        production = project.get("production")
        storyboard = production.get("storyboard") if isinstance(production, dict) else None
        visual = production.get("visual") if isinstance(production, dict) else None
        if not isinstance(source, dict) or source.get("type") not in {"novel", "script", "storyboard"}:
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "source.type 不受支持")
        if not isinstance(source.get("path"), str) or not isinstance(source.get("sha256"), str):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "source 合同不完整")
        if not isinstance(storyboard, dict) or storyboard.get("enabled") is not True:
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.storyboard 合同不完整")
        if storyboard.get("engine") not in {"legacy", "workflow", "agent"}:
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "storyboard engine 不受支持")
        if not isinstance(visual, dict) or not isinstance(visual.get("enabled"), bool):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual 合同不完整")
        if visual.get("enabled"):
            engine = visual.get("engine")
            profile = visual.get("provider_profile")
            request = visual.get("provider_request")
            kind = visual.get("operation_kind")
            settlement_mode = visual.get("settlement_mode", "provider_evidence")
            tariff = visual.get("contractual_tariff")
            if engine not in {"mock", "production"} or not isinstance(visual.get("maximum_paid_calls"), int) or visual["maximum_paid_calls"] < 1:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual 合同无效")
            if not isinstance(visual.get("maximum_amount"), str) or not visual["maximum_amount"].isdigit():
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual 预算无效")
            if engine == "mock" and (profile != "mock" or kind != "mock_image" or request is not None):
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual mock 绑定无效")
            if (
                engine == "production" and (
                    not isinstance(profile, str) or not profile or profile == "mock"
                    or not isinstance(kind, str) or not kind or kind == "mock_image"
                    or not isinstance(request, dict) or not request
                )
            ):
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual production 绑定无效")
            if settlement_mode not in {"provider_evidence", "contractual_tariff"}:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual settlement mode is invalid")
            if settlement_mode == "contractual_tariff":
                from manju.production.approvals import _contractual_tariff
                _contractual_tariff(tariff, maximum_amount=visual["maximum_amount"], currency="USD", code=ReasonCode.UNSUPPORTED_SCHEMA_VERSION)
            elif tariff is not None:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.visual tariff requires contractual settlement mode")
        for key in ("max_steps", "max_revisions"):
            value = storyboard.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, f"storyboard.{key} 无效")
        max_calls = storyboard.get("max_calls")
        if max_calls is not None and (
            not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 1
        ):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "storyboard.max_calls 无效")
        if not str(project.get("project_id", "")).strip():
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "project_id 缺失")
        return project

    def source_path(self, project: dict[str, Any]) -> str:
        path = str(project["source"].get("path", ""))
        if not os.path.isabs(path):
            path = os.path.join(self.paths.root, path)
        return os.path.abspath(path)

    def validate_source(self, project: dict[str, Any]) -> str:
        path = self.source_path(project)
        if not os.path.isfile(path):
            raise ProductionError(ReasonCode.SOURCE_MISSING.value, f"源文件不存在: {path}")
        actual = sha256_file(path)
        expected = str(project["source"].get("sha256", ""))
        if actual != expected:
            raise ProductionError(ReasonCode.SOURCE_HASH_MISMATCH.value)
        return path

    def validate_contract(self, project: dict[str, Any], run_id: str) -> dict[str, Any]:
        contract = read_json(self.paths.contract_file(run_id))
        if not isinstance(contract, dict):
            raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "活动 run 合同缺失")
        if contract.get("project_spec_fingerprint") != fingerprint(project):
            raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value)
        recorded = str(contract.get("contract_fingerprint", ""))
        unsigned = {key: value for key, value in contract.items() if key != "contract_fingerprint"}
        if recorded != fingerprint(unsigned):
            raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "run 合同指纹无效")
        return contract

    def snapshot(self):
        return reduce_events(self.events.read())

    def write_projection(self) -> None:
        atomic_write_json(self.paths.state_file, self.snapshot().to_dict())
