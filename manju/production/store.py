"""Project state loading, validation, and projection persistence."""

from __future__ import annotations

import hashlib
import os
import stat
from typing import Any

from manju.production.events import EventStore, HmacKeyProvider
from manju.production.artifacts import ArtifactGraph, ArtifactRef
from manju.production.revisions import RevisionProjection
from manju.production.models import (
    PROJECT_SCHEMA_VERSION,
    ProductionError,
    ReasonCode,
    VIDEO_PROMPT_MAX_DURATION_SECONDS,
    VIDEO_PROMPT_MAX_INPUT_BYTES,
    VIDEO_PROMPT_MAX_OUTPUT_BYTES,
    VIDEO_PROMPT_MAX_PROMPT_CHARS,
    VIDEO_PROMPT_MAX_SHOTS,
    VIDEO_PROMPT_MAX_TEXT_CHARS,
    VIDEO_PROMPT_MAX_TOTAL_DURATION_SECONDS,
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
        voice_script = production.get("voice_script", {"enabled": False}) if isinstance(production, dict) else None
        voice_director = production.get("voice_director", {"enabled": False}) if isinstance(production, dict) else None
        voice_tts = production.get("voice_tts", {"enabled": False}) if isinstance(production, dict) else None
        video_prompt = production.get("video_prompt", {"enabled": False}) if isinstance(production, dict) else None
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
        if not isinstance(voice_script, dict) or not isinstance(voice_script.get("enabled"), bool):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_script contract is invalid")
        if voice_script.get("enabled") and (
            voice_script.get("mode") != "deterministic_offline"
            or voice_script.get("schema_version") != "voice-script-v1"
        ):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_script binding is invalid")
        if not isinstance(voice_director, dict) or not isinstance(voice_director.get("enabled"), bool):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_director contract is invalid")
        if voice_director.get("enabled") and (
            not voice_script.get("enabled")
            or voice_director.get("mode") != "offline_langgraph_mock"
            or voice_director.get("schema_version") != "voice-direction-v1"
            or voice_director.get("policy_version") != "voice-director-policy-v1"
            or voice_director.get("model_profile") != "deterministic-mock"
            or not isinstance(voice_director.get("max_model_calls"), int)
            or voice_director.get("max_model_calls") < 1
            or not isinstance(voice_director.get("max_steps"), int)
            or voice_director.get("max_steps") < 4
        ):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_director binding is invalid")
        if not isinstance(voice_tts, dict) or not isinstance(voice_tts.get("enabled"), bool):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_tts contract is invalid")
        if voice_tts.get("enabled") and (
            not voice_director.get("enabled")
            or voice_tts.get("mode") not in {"offline_mock", "paid_siliconflow"}
            or voice_tts.get("schema_version") != "voice-audio-v1"
            or not isinstance(voice_tts.get("model_profile"), str)
            or not voice_tts.get("model_profile")
        ):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_tts binding is invalid")
        if voice_tts.get("enabled") and voice_tts.get("mode") == "paid_siliconflow":
            if not isinstance(voice_tts.get("maximum_amount"), str) or not voice_tts["maximum_amount"].isdigit():
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_tts budget is invalid")
            if not isinstance(voice_tts.get("provider_profile"), str) or not voice_tts["provider_profile"]:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_tts provider profile is invalid")
            if voice_tts.get("provider_request") is not None and not isinstance(voice_tts.get("provider_request"), dict):
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.voice_tts provider request is invalid")
        if not isinstance(video_prompt, dict) or not isinstance(video_prompt.get("enabled"), bool):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video_prompt contract is invalid")
        if video_prompt.get("enabled"):
            if video_prompt.get("mode") != "deterministic_offline" or video_prompt.get("schema_version") != "video-prompt-v1":
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video_prompt binding is invalid")
            integer_limits = {
                "max_input_bytes": VIDEO_PROMPT_MAX_INPUT_BYTES,
                "max_output_bytes": VIDEO_PROMPT_MAX_OUTPUT_BYTES,
                "max_shots": VIDEO_PROMPT_MAX_SHOTS,
                "max_text_chars": VIDEO_PROMPT_MAX_TEXT_CHARS,
                "max_prompt_chars": VIDEO_PROMPT_MAX_PROMPT_CHARS,
            }
            for key, upper in integer_limits.items():
                value = video_prompt.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > upper:
                    raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, f"production.video_prompt {key} is invalid")
            float_limits = {
                "max_duration_seconds": VIDEO_PROMPT_MAX_DURATION_SECONDS,
                "max_total_duration_seconds": VIDEO_PROMPT_MAX_TOTAL_DURATION_SECONDS,
            }
            for key, upper in float_limits.items():
                value = video_prompt.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or float(value) > upper:
                    raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, f"production.video_prompt {key} is invalid")
        video = production.get("video", {"enabled": False}) if isinstance(production, dict) else None
        if not isinstance(video, dict) or not isinstance(video.get("enabled"), bool):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video contract is invalid")
        if video.get("enabled"):
            if (
                video.get("mode") not in {"mock", "paid_agnes"}
                or video.get("schema_version") != "video-run-v1"
                or not isinstance(video.get("model_profile"), str)
                or not video.get("model_profile")
            ):
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video binding is invalid")
            if not isinstance(video.get("maximum_amount"), str) or not video["maximum_amount"].isdigit():
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video budget is invalid")
            if not isinstance(video.get("provider_profile"), str) or not video["provider_profile"]:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video provider profile is invalid")
            if video.get("provider_request") is not None and not isinstance(video.get("provider_request"), dict):
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "production.video provider request is invalid")
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
        created = [event for event in self.events.read()
                   if event.get("event_type") == "run_created" and event.get("run_id") == run_id]
        if len(created) != 1 or (created[0].get("payload") or {}).get("contract_fingerprint") != recorded:
            raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "run 合同未绑定到事件账本")
        event_payload = created[0].get("payload") or {}
        if contract.get("stage_sequence") is not None and event_payload.get("stage_sequence") != contract.get("stage_sequence"):
            raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "run stage sequence is not event-bound")
        revision = event_payload.get("revision")
        if revision is not None:
            if not isinstance(revision, dict) or (
                contract.get("predecessor_run_id") != revision.get("predecessor_run_id")
                or contract.get("revision_id") != revision.get("revision_id")
                or contract.get("reuse_manifest") != revision.get("reuse_manifest")
                or contract.get("predecessor_selection", []) != revision.get("predecessor_selection", [])
                or contract.get("successor_selection", []) != revision.get("successor_selection", [])
                or contract.get("execution_plan", []) != revision.get("execution_plan", [])
            ):
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "successor 合同与 revision 账本不一致")
        return contract

    def validate_runtime_inputs(self, project: dict[str, Any], contract: dict[str, Any]) -> dict[str, str]:
        """Verify every immutable run input and return its resolved snapshot path."""
        inputs = contract.get("runtime_inputs")
        if not isinstance(inputs, dict):
            return {"source.script": self.validate_source(project)}
        if "source.script" not in inputs:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "runtime source input is absent")
        resolved: dict[str, str] = {}
        for logical_id, value in sorted(inputs.items()):
            if not isinstance(value, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input is invalid: {logical_id}")
            ref = ArtifactRef.from_dict(value)
            if ref.logical_id != logical_id:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input logical_id mismatch: {logical_id}")
            snapshot_path = value.get("snapshot_path")
            if not isinstance(snapshot_path, str):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input snapshot is invalid: {logical_id}")
            absolute_path = self.artifact_path(snapshot_path)
            if not os.path.isfile(absolute_path) or sha256_file(absolute_path) != ref.version_id[7:]:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input is missing or changed: {logical_id}")
            resolved[logical_id] = absolute_path
        return resolved

    def snapshot(self):
        events = self.events.read()
        graph = ArtifactGraph.from_events(events)
        self.validate_artifact_files(graph)
        RevisionProjection.from_events(events)
        return reduce_events(events)

    def write_projection(self) -> None:
        events = self.events.read()
        graph = ArtifactGraph.from_events(events)
        self.validate_artifact_files(graph)
        revisions = RevisionProjection.from_events(events)
        atomic_write_json(self.paths.state_file, reduce_events(events).to_dict())
        atomic_write_json(self.paths.artifacts_file, graph.to_dict())
        atomic_write_json(self.paths.revisions_file, revisions.to_dict())

    def artifact_graph(self) -> ArtifactGraph:
        graph = ArtifactGraph.from_events(self.events.read())
        self.validate_artifact_files(graph)
        return graph

    def artifact_graph_snapshot(self):
        """Return graph and top-level status reduced from one event read."""
        events = self.events.read()
        graph = ArtifactGraph.from_events(events)
        self.validate_artifact_files(graph)
        return graph, reduce_events(events)

    def revisions(self) -> RevisionProjection:
        return RevisionProjection.from_events(self.events.read())

    def revision_snapshot(self):
        events = self.events.read()
        graph = ArtifactGraph.from_events(events)
        self.validate_artifact_files(graph)
        revisions = RevisionProjection.from_events(events)
        return revisions, reduce_events(events)

    def artifact_path(self, relative_path: str) -> str:
        """Resolve a file below the project while refusing links and reparse points."""
        if not isinstance(relative_path, str) or os.path.isabs(relative_path):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact path must be project-relative")
        normalized = relative_path.replace("\\", "/")
        if os.path.splitdrive(normalized)[0] or ".." in normalized.split("/"):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact path must be project-relative")
        candidate = os.path.abspath(os.path.join(self.paths.root, normalized))
        root = os.path.abspath(self.paths.root)
        try:
            inside_root = os.path.normcase(os.path.commonpath([root, candidate])) == os.path.normcase(root)
        except ValueError:
            inside_root = False
        if not inside_root:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact path is outside the project")
        current = root
        for part in normalized.split("/"):
            current = os.path.join(current, part)
            if not os.path.lexists(current):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact file is unavailable")
            value = os.lstat(current)
            attributes = getattr(value, "st_file_attributes", 0)
            if stat.S_ISLNK(value.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact path cannot contain a link or reparse point")
        real_root = os.path.normcase(os.path.realpath(root))
        real_candidate = os.path.normcase(os.path.realpath(candidate))
        try:
            inside_real_root = os.path.commonpath([real_root, real_candidate]) == real_root
        except ValueError:
            inside_real_root = False
        if not inside_real_root or not os.path.isfile(candidate):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact file is unavailable")
        return candidate

    def validate_artifact_files(self, graph: ArtifactGraph) -> None:
        for record in graph._records.values():
            path = self.artifact_path(record.path)
            if f"sha256:{sha256_file(path)}" != record.ref.version_id:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "artifact content no longer matches its registered version")
