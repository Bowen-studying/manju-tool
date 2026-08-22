"""Application service for deterministic ProductionRun advancement."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import mimetypes
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from manju import __version__
from manju.pipeline.storyboard_supervisor import (
    SUPERVISOR_AGENT_VERSION,
    SUPERVISOR_TOOLSET_VERSION,
)
from manju.production.adapters.storyboard import StoryboardStageAdapter
from manju.production.adapters.visual import VisualStageAdapter
from manju.production.adapters.voice_script import VoiceScriptStageAdapter
from manju.production.adapters.voice_director import VoiceDirectorStageAdapter
from manju.production.adapters.voice_tts import VoiceTTSStageAdapter
from manju.production.adapters.base import StageResult
from manju.production.approvals import ApprovalRequest, Grant, contractual_tariff_usage, create_contractual_tariff
from manju.production.artifacts import ArtifactGraph, ArtifactRecord, ArtifactRef
from manju.production.events import HmacKeyProvider
from manju.production.operations import OperationRecord
from manju.production.manual_operations import (
    ManualBillingEvidence,
    ManualDispatchPackage,
    ManualResultPackage,
    sha256_file as manual_sha256_file,
)
from manju.production.graph import has_event
from manju.production.locking import ProjectLock
from manju.production.models import (
    DAG_VERSION,
    M2_DAG_VERSION,
    M4_DAG_VERSION,
    M4_1_DAG_VERSION,
    M4_2_DAG_VERSION,
    PROJECT_SCHEMA_VERSION,
    ProductionError,
    ProductionSnapshot,
    ProductionStatus,
    ReasonCode,
    fingerprint,
    utc_now,
    stages_for_dag,
)
from manju.production.paths import ProjectPaths
from manju.production.scheduler import ProductionScheduler
from manju.production.store import ProjectStore, sha256_file
from manju.production.tts_providers import TTS_CURRENCY
from manju.utils.ai import get_ai_config
from manju.utils.runtime import atomic_write_json, read_json, safe_filename
from manju.utils.runtime import atomic_write_bytes


SnapshotListener = Callable[[ProductionSnapshot], None]


def initialize_project(
    *,
    source: str,
    source_type: str,
    output_dir: str,
    engine: str = "agent",
    max_scenes: int | None = None,
    max_steps: int = 40,
    max_calls: int | None = None,
    max_revisions: int = 2,
    provider_profile: str = "default",
    hmac_key_id: str = "manju-local-default",
    visual_enabled: bool = False,
    visual_maximum_paid_calls: int = 1,
    visual_maximum_amount: str = "0",
    visual_provider_profile: str = "mock",
    visual_provider_request: dict[str, Any] | None = None,
    visual_operation_kind: str = "mock_image",
    visual_settlement_mode: str = "provider_evidence",
    visual_contractual_tariff_id: str = "",
    visual_contractual_tariff_amount: str = "",
    voice_script_enabled: bool = False,
    voice_director_enabled: bool = False,
    voice_director_max_model_calls: int = 1,
    voice_director_max_steps: int = 8,
    voice_tts_enabled: bool = False,
    voice_tts_model_profile: str = "deterministic-fake-tts-v1",
    voice_tts_mode: str = "offline_mock",
    voice_tts_maximum_amount: str = "1000",
    voice_tts_provider_profile: str = "siliconflow-cosyvoice2",
    voice_tts_provider_request: dict[str, Any] | None = None,
) -> ProductionSnapshot:
    source = os.path.abspath(source)
    if source_type not in {"novel", "script", "storyboard"}:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "source-type 必须是 novel、script 或 storyboard")
    if engine not in {"legacy", "workflow", "agent"}:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "storyboard engine 不受支持")
    if voice_director_enabled and not voice_script_enabled:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-director requires voice-script")
    if voice_director_enabled and (voice_director_max_model_calls < 1 or voice_director_max_steps < 4):
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-director budget is invalid")
    if voice_tts_enabled and not voice_director_enabled:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-tts requires voice-director")
    if voice_tts_enabled and (not isinstance(voice_tts_model_profile, str) or not voice_tts_model_profile.strip()):
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-tts model profile is invalid")
    if voice_tts_enabled and voice_tts_mode not in {"offline_mock", "paid_siliconflow"}:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-tts mode is invalid")
    if voice_tts_enabled and voice_tts_mode == "paid_siliconflow":
        if not str(voice_tts_maximum_amount).isdigit() or not voice_tts_provider_profile.strip():
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-tts paid budget or provider profile is invalid")
        if voice_tts_provider_request is not None and not isinstance(voice_tts_provider_request, dict):
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "voice-tts provider request is invalid")
    if visual_enabled and (visual_maximum_paid_calls < 1 or not str(visual_maximum_amount).isdigit()):
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "visual mock 预算无效")
    if visual_enabled and (not isinstance(visual_provider_profile, str) or not visual_provider_profile or not isinstance(visual_operation_kind, str) or not visual_operation_kind or not isinstance(visual_provider_request, (dict, type(None)))):
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "visual provider configuration is invalid")
    if visual_settlement_mode not in {"provider_evidence", "contractual_tariff"}:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "visual settlement mode is invalid")
    if visual_settlement_mode == "contractual_tariff" and (
        not visual_contractual_tariff_id or not str(visual_contractual_tariff_amount).isdigit()
        or int(visual_contractual_tariff_amount) > int(visual_maximum_amount)
    ):
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "visual contractual tariff is invalid")
    if not os.path.isfile(source):
        raise ProductionError(ReasonCode.SOURCE_MISSING.value, f"源文件不存在: {source}")

    paths = ProjectPaths(os.path.abspath(output_dir))
    if os.path.exists(paths.project_file):
        raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "目标目录已经包含 project.json")
    paths.ensure_layout()

    source_name = safe_filename(os.path.basename(source), fallback="source.txt", max_length=120)
    stored_source = os.path.join(paths.sources_dir, source_name)
    if os.path.abspath(source) != os.path.abspath(stored_source):
        if os.path.exists(stored_source):
            stem, extension = os.path.splitext(source_name)
            stored_source = os.path.join(paths.sources_dir, f"{stem}-{uuid.uuid4().hex[:8]}{extension}")
        shutil.copy2(source, stored_source)
    relative_source = os.path.relpath(stored_source, paths.root)
    project_id = f"prj_{uuid.uuid4().hex}"
    project = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "project_id": project_id,
        "source": {
            "type": source_type,
            "path": relative_source,
            "sha256": sha256_file(stored_source),
            "format": mimetypes.guess_type(stored_source)[0] or "application/octet-stream",
        },
        "profile": "audited-agent",
        "production": {
            "storyboard": {
                "enabled": True,
                "engine": engine,
                "max_scenes": max_scenes,
                "max_steps": max_steps,
                "max_calls": max_calls,
                "max_revisions": max_revisions,
            },
            "visual": {
                "enabled": visual_enabled,
                "engine": ("mock" if visual_provider_profile == "mock" else "production") if visual_enabled else "",
                "maximum_paid_calls": visual_maximum_paid_calls if visual_enabled else 0,
                "maximum_amount": visual_maximum_amount if visual_enabled else "0",
                "provider_profile": visual_provider_profile if visual_enabled else "",
                "provider_request": visual_provider_request if visual_enabled else None,
                "operation_kind": visual_operation_kind if visual_enabled else "",
                "settlement_mode": visual_settlement_mode if visual_enabled else "provider_evidence",
                "contractual_tariff": (
                    create_contractual_tariff(
                        tariff_id=visual_contractual_tariff_id, amount_minor=str(visual_contractual_tariff_amount), currency="USD"
                    ) if visual_enabled and visual_settlement_mode == "contractual_tariff" else None
                ),
            },
            "voice_script": {
                "enabled": voice_script_enabled,
                "mode": "deterministic_offline" if voice_script_enabled else "",
                "schema_version": "voice-script-v1" if voice_script_enabled else "",
            },
            "voice_director": {
                "enabled": voice_director_enabled,
                "mode": "offline_langgraph_mock" if voice_director_enabled else "",
                "schema_version": "voice-direction-v1" if voice_director_enabled else "",
                "policy_version": "voice-director-policy-v1" if voice_director_enabled else "",
                "model_profile": "deterministic-mock" if voice_director_enabled else "",
                "max_model_calls": voice_director_max_model_calls if voice_director_enabled else 0,
                "max_steps": voice_director_max_steps if voice_director_enabled else 0,
            },
            "voice_tts": {
                "enabled": voice_tts_enabled,
                "mode": voice_tts_mode if voice_tts_enabled else "",
                "schema_version": "voice-audio-v1" if voice_tts_enabled else "",
                "model_profile": voice_tts_model_profile if voice_tts_enabled else "",
                "maximum_amount": voice_tts_maximum_amount if voice_tts_enabled and voice_tts_mode == "paid_siliconflow" else "",
                "provider_profile": voice_tts_provider_profile if voice_tts_enabled and voice_tts_mode == "paid_siliconflow" else "",
                "provider_request": voice_tts_provider_request if voice_tts_enabled and voice_tts_mode == "paid_siliconflow" else None,
            },
            "voice": {"enabled": False},
            "video": {"enabled": False},
        },
        "provider_profiles": {"llm": provider_profile},
        "integrity": {"hmac_key_id": hmac_key_id},
    }
    atomic_write_json(paths.project_file, project)
    store = ProjectStore(paths.project_file)
    store.events.append(
        "project_initialized",
        project_id=project_id,
        payload={"source_sha256": project["source"]["sha256"]},
    )
    if voice_director_enabled:
        policy = {
            "schema_version": "voice-director-policy-v1",
            "policy_version": "voice-director-policy-v1",
            "model_profile": "deterministic-mock",
            "allowed_emotions": ["neutral"],
            "max_model_calls": voice_director_max_model_calls,
            "max_steps": voice_director_max_steps,
        }
        os.makedirs(os.path.dirname(paths.voice_director_policy_path), exist_ok=True)
        atomic_write_json(paths.voice_director_policy_path, policy)
        policy_ref = {
            "logical_id": "voice_director.policy",
            "version_id": f"sha256:{sha256_file(paths.voice_director_policy_path)}",
        }
        policy_record = ArtifactRecord.from_dict({
            **policy_ref,
            "path": os.path.relpath(paths.voice_director_policy_path, paths.root),
            "producer": {"stage": "project_policy", "run_id": ""},
            "depends_on": [],
        })
        store.events.append(
            "artifact_registered", project_id=project_id, run_id="",
            payload={"artifact": policy_record.to_dict(state="available"), "bootstrap": True},
        )
        store.events.append(
            "artifact_version_selected", project_id=project_id, run_id="",
            payload={"logical_id": policy_ref["logical_id"], "version_id": policy_ref["version_id"],
                     "previous_version_id": "", "invalidated": []},
        )
    store.write_projection()
    return store.snapshot()


class ProductionService:
    def __init__(
        self,
        project_file: str,
        *,
        storyboard_adapter: StoryboardStageAdapter | None = None,
        visual_adapter: VisualStageAdapter | None = None,
        voice_script_adapter: VoiceScriptStageAdapter | None = None,
        voice_director_adapter: VoiceDirectorStageAdapter | None = None,
        voice_tts_adapter: VoiceTTSStageAdapter | None = None,
        hmac_key_provider: HmacKeyProvider | None = None,
        listeners: tuple[SnapshotListener, ...] = (),
    ):
        self.store = ProjectStore(project_file, key_provider=hmac_key_provider)
        self._hmac_key_provider = hmac_key_provider
        self.paths = self.store.paths
        self.storyboard_adapter = storyboard_adapter or StoryboardStageAdapter()
        self.visual_adapter = visual_adapter or VisualStageAdapter()
        self.voice_script_adapter = voice_script_adapter or VoiceScriptStageAdapter()
        self.voice_director_adapter = voice_director_adapter or VoiceDirectorStageAdapter()
        candidate_voice_tts = voice_tts_adapter or VoiceTTSStageAdapter()
        if type(candidate_voice_tts) is not VoiceTTSStageAdapter:
            raise TypeError("M4.2 offline TTS accepts only VoiceTTSStageAdapter")
        self.voice_tts_adapter = candidate_voice_tts
        self.scheduler = ProductionScheduler()
        self.listeners = listeners

    def _configure_visual_receipt_signer(self, project: dict[str, Any]) -> None:
        """Use the project's ephemeral event key to authenticate pre-settlement receipts."""
        key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
        key = self._hmac_key_provider.get_key(key_id) if self._hmac_key_provider is not None else None
        if key is None:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "visual receipt signing key is unavailable")
        self.visual_adapter.configure_receipt_signer(key_id=key_id, key=key)

    def request_visual_approval(
        self, request: ApprovalRequest, *, expected_last_event_hash: str
    ) -> ProductionSnapshot:
        """Persist a signed M2 approval request; this method never calls a provider."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash):
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "last event hash changed")
            if request.project_id != project["project_id"] or request.run_id != snapshot.run_id:
                raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "request does not bind active project/run")
            key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
            self.store.events.append(
                "approval_requested", project_id=project["project_id"], run_id=snapshot.run_id,
                payload={"approval_request": request.to_dict(), "key_id": key_id},
            )
            return self._snapshot_and_project()

    def list_approvals(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for event in self.store.events.read():
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("event_type") == "approval_requested":
                results.append({"approval_request": payload.get("approval_request"), "status": "awaiting_approval",
                                "last_event_hash": event.get("event_hash", "")})
            elif event.get("event_type") in {"approval_approved", "approval_rejected"}:
                for item in results:
                    if item["approval_request"].get("request_id") == payload.get("request_id"):
                        item["status"] = "approved" if event["event_type"] == "approval_approved" else "rejected"
        return results

    def _artifact_graph_payload(self, project: dict[str, Any], graph: ArtifactGraph, snapshot: ProductionSnapshot) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "project_id": project["project_id"],
            "run_id": snapshot.run_id,
            "status": "ready",
            "reason": {"code": "ARTIFACT_GRAPH_READY", "message": "产物图谱已验证"},
            "next_actions": [],
            "last_event_hash": snapshot.last_event_hash,
            "graph": graph.to_dict(),
        }

    def get_artifact_graph(self) -> dict[str, Any]:
        project = self.store.load_project()
        graph, snapshot = self.store.artifact_graph_snapshot()
        return self._artifact_graph_payload(project, graph, snapshot)

    def _ensure_project_source_artifact(self, project: dict[str, Any]) -> None:
        """Bootstrap the immutable source node before the first run is created."""
        graph = self.store.artifact_graph()
        if graph.current_version("source.script"):
            return
        source_path = self.store.validate_source(project)
        record = ArtifactRecord.from_dict({
            "logical_id": "source.script",
            "version_id": f"sha256:{sha256_file(source_path)}",
            "path": os.path.relpath(source_path, self.paths.root),
            "producer": {"stage": "project_source", "run_id": ""},
            "depends_on": [],
        })
        existing = graph._records.get(record.ref)
        if existing is None:
            graph.register(record)
            self.store.events.append(
                "artifact_registered", project_id=project["project_id"], run_id="",
                payload={"artifact": record.to_dict(state="available"), "bootstrap": True},
            )
            graph = self.store.artifact_graph()
        elif existing != record or graph._states.get(record.ref) != "available":
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  "source bootstrap artifact conflicts with project source")
        graph.select(
            logical_id=record.ref.logical_id, version_id=record.ref.version_id,
            previous_version_id="", recorded_invalidated=[],
        )
        self.store.events.append(
            "artifact_version_selected", project_id=project["project_id"], run_id="",
            payload={"logical_id": record.ref.logical_id, "version_id": record.ref.version_id,
                     "previous_version_id": "", "invalidated": []},
        )

    def _runtime_inputs(self, project: dict[str, Any], run_id: str,
                        selection: tuple[dict[str, Any], ...] = ()) -> dict[str, dict[str, str]]:
        """Freeze file-backed selected inputs into the run contract."""
        graph = self.store.artifact_graph()
        selected = {item["logical_id"]: ArtifactRef.from_dict(item) for item in selection}
        for logical_id in ("source.script", "voice_director.policy"):
            if logical_id not in selected:
                version_id = graph.current_version(logical_id)
                if version_id:
                    selected[logical_id] = ArtifactRef(logical_id, version_id)
        inputs: dict[str, dict[str, str]] = {}
        for logical_id in ("source.script", "style.reference", "voice_director.policy"):
            ref = selected.get(logical_id)
            if ref is None:
                continue
            record = graph.get(ref.logical_id, ref.version_id)
            absolute_path = self.store.artifact_path(record.path)
            if not os.path.isfile(absolute_path):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input is missing or changed: {logical_id}")
            with open(absolute_path, "rb") as handle:
                content = handle.read()
            if hashlib.sha256(content).hexdigest() != ref.version_id[7:]:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input is missing or changed: {logical_id}")
            snapshot_path = os.path.join(
                self.paths.run_dir(run_id), "inputs",
                f"{logical_id.replace('.', '_')}-{ref.version_id[7:]}.bin",
            )
            atomic_write_bytes(snapshot_path, content)
            inputs[logical_id] = {**ref.to_dict(), "path": record.path,
                                  "snapshot_path": os.path.relpath(snapshot_path, self.paths.root)}
        if "source.script" not in inputs:
            source_path = self.store.validate_source(project)
            with open(source_path, "rb") as handle:
                content = handle.read()
            content_hash = hashlib.sha256(content).hexdigest()
            if content_hash != str(project["source"].get("sha256", "")):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "runtime source changed while snapshotting")
            snapshot_path = os.path.join(
                self.paths.run_dir(run_id), "inputs", f"source_script-{content_hash}.bin",
            )
            atomic_write_bytes(snapshot_path, content)
            inputs["source.script"] = {
                "logical_id": "source.script", "version_id": f"sha256:{content_hash}",
                "path": os.path.relpath(source_path, self.paths.root),
                "snapshot_path": os.path.relpath(snapshot_path, self.paths.root),
            }
        return inputs

    def _runtime_input_path(self, contract: dict[str, Any], logical_id: str, *, fallback_path: str = "") -> str:
        inputs = contract.get("runtime_inputs")
        value = inputs.get(logical_id) if isinstance(inputs, dict) else None
        if not isinstance(value, dict):
            if logical_id == "source.script" and fallback_path:
                return fallback_path
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, f"runtime input is absent: {logical_id}")
        ref = ArtifactRef.from_dict(value)
        path = value.get("snapshot_path")
        if not isinstance(path, str):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, f"runtime input snapshot is invalid: {logical_id}")
        absolute_path = self.store.artifact_path(path)
        if not os.path.isfile(absolute_path) or sha256_file(absolute_path) != ref.version_id[7:]:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"runtime input is missing or changed: {logical_id}")
        return absolute_path

    def _stage_private_input(
        self,
        contract: dict[str, Any],
        logical_id: str,
        output_dir: str,
        *,
        fallback_path: str = "",
    ) -> str:
        """Copy verified contract input bytes into the active stage directory.

        Adapters intentionally accept paths.  Passing a run snapshot directly
        would leave a check-to-use interval after the project lock is released,
        so the adapter only receives this newly materialized immutable input.
        """
        inputs = contract.get("runtime_inputs")
        value = inputs.get(logical_id) if isinstance(inputs, dict) else None
        if isinstance(value, dict):
            ref = ArtifactRef.from_dict(value)
            snapshot_path = value.get("snapshot_path")
            if not isinstance(snapshot_path, str):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                      f"runtime input snapshot is invalid: {logical_id}")
            source_path = self.store.artifact_path(snapshot_path)
            expected_hash = ref.version_id.removeprefix("sha256:")
        elif logical_id == "source.script" and fallback_path:
            source_path = fallback_path
            expected_hash = ""
        else:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"runtime input is absent: {logical_id}")
        try:
            with open(source_path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"runtime input is unavailable: {logical_id}") from exc
        actual_hash = hashlib.sha256(content).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"runtime input is missing or changed: {logical_id}")
        private_path = os.path.join(output_dir, ".runtime_inputs",
                                    f"{safe_filename(logical_id)}-{actual_hash}.bin")
        atomic_write_bytes(private_path, content)
        return private_path

    def _stage_private_artifact(self, *, path: str, version_id: str, logical_id: str, output_dir: str) -> str:
        """Verify an upstream artifact once, then bind the adapter to a private copy."""
        if not isinstance(version_id, str) or not version_id.startswith("sha256:"):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"stage artifact version is invalid: {logical_id}")
        source_path = self.store.artifact_path(path)
        try:
            with open(source_path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"stage artifact is unavailable: {logical_id}") from exc
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != version_id.removeprefix("sha256:"):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"stage artifact is missing or changed: {logical_id}")
        private_path = os.path.join(output_dir, ".runtime_inputs",
                                    f"{safe_filename(logical_id)}-{actual_hash}.bin")
        atomic_write_bytes(private_path, content)
        return private_path

    def _relative_artifact_path(self, path: str) -> str:
        """Normalize adapter output paths against the project, never the process CWD."""
        if not isinstance(path, str) or not path:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "stage artifact path is invalid")
        absolute_path = path if os.path.isabs(path) else os.path.join(self.paths.root, path)
        relative_path = os.path.relpath(os.path.abspath(absolute_path), self.paths.root)
        self.store.artifact_path(relative_path)
        return relative_path

    def list_revisions(self) -> dict[str, Any]:
        project = self.store.load_project()
        revisions, snapshot = self.store.revision_snapshot()
        return {
            "schema_version": "1", "project_id": project["project_id"], "run_id": snapshot.run_id,
            "status": "ready", "reason": {"code": "REVISION_HISTORY_READY", "message": "修订历史已验证"},
            "next_actions": [], "last_event_hash": snapshot.last_event_hash,
            "revision_history": revisions.to_dict(),
        }

    def _revision_preview(self, *, project: dict[str, Any], changed: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        graph, snapshot = self.store.artifact_graph_snapshot()
        if snapshot.status not in {ProductionStatus.COMPLETED.value, ProductionStatus.PAUSED.value,
                                   ProductionStatus.NEEDS_REVIEW.value, ProductionStatus.FAILED.value}:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision requires a safe terminal or paused run")
        requested = tuple(ArtifactRef.from_dict(item) for item in changed)
        current = tuple(ArtifactRef(item["logical_id"], item["version_id"]) for item in graph.to_dict()["current"])
        if not requested or len(set(requested)) != len(requested):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision changed artifacts are invalid")
        direct_stage_outputs = {"storyboard.output", "voice_script.main", "voice_direction.main", "voice_audio.main", "visual.asset"}
        if any(item.logical_id in direct_stage_outputs for item in requested):
            raise ProductionError(
                ReasonCode.OPERATION_CONTRACT_INVALID.value,
                "stage output candidates require an explicit producer-run authority model",
            )
        is_candidate_revision = all(graph._states.get(item) == "available" for item in requested)
        if is_candidate_revision:
            predecessor_selection, affected, successor_selection, reused = graph.revision_scope(requested)
            voice_script_enabled = project.get("production", {}).get("voice_script", {}).get("enabled")
            voice_director_enabled = project.get("production", {}).get("voice_director", {}).get("enabled")
            voice_tts_enabled = project.get("production", {}).get("voice_tts", {}).get("enabled")
            visual_enabled = project.get("production", {}).get("visual", {}).get("enabled")
            configured_stages = ["storyboard"]
            if voice_script_enabled:
                configured_stages.append("voice_script")
            if voice_director_enabled:
                configured_stages.append("voice_director")
            if voice_tts_enabled:
                configured_stages.append("voice_tts")
            if visual_enabled:
                configured_stages.append("visual")
            configured_stages = tuple(configured_stages)
            execution_plan = self._revision_execution_plan(graph, affected, requested, stages=configured_stages)
        elif set(requested).issubset(set(current)):
            # Backward-compatible M3.1 history: the version was already selected
            # before preview. New M3.3 callers must use revision candidates.
            predecessor_selection, successor_selection, execution_plan = (), (), ()
            affected = tuple(sorted(ref for ref, state in graph._states.items() if state == "invalidated"))
            reused = tuple(sorted(set(current) - set(requested)))
        else:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision changed artifacts must be candidates or current versions")
        fingerprint_payload = {
            "predecessor_run_id": snapshot.run_id,
            "changed": [item.to_dict() for item in requested],
            "affected": [item.to_dict() for item in affected],
            "reuse_manifest": [item.to_dict() for item in reused],
            "last_event_hash": snapshot.last_event_hash,
        }
        if is_candidate_revision:
            fingerprint_payload.update({
                "predecessor_selection": [item.to_dict() for item in predecessor_selection],
                "successor_selection": [item.to_dict() for item in successor_selection],
                "execution_plan": execution_plan,
            })
        preview_fingerprint = fingerprint(fingerprint_payload)
        return {
            "schema_version": "1", "project_id": project["project_id"], "run_id": snapshot.run_id,
            "status": "ready", "reason": {"code": "REVISION_PREVIEW_READY", "message": "修订影响范围已计算"},
            "next_actions": [{"action": "create_revision", "preview_fingerprint": preview_fingerprint}],
            "last_event_hash": snapshot.last_event_hash, "preview_fingerprint": preview_fingerprint,
            "changed": [item.to_dict() for item in requested], "affected_artifacts": [item.to_dict() for item in affected],
            "reused_artifacts": [item.to_dict() for item in reused],
            "predecessor_selection": [item.to_dict() for item in predecessor_selection],
            "successor_selection": [item.to_dict() for item in successor_selection],
            "execution_plan": execution_plan,
            "estimated_paid_operations": [item for item in execution_plan if item["stage"] == "visual" and item["action"] == "regenerate"],
        }

    @staticmethod
    def _revision_execution_plan(
        graph: ArtifactGraph, affected: tuple[ArtifactRef, ...], changed: tuple[ArtifactRef, ...],
        *, stages: tuple[str, ...] = ("storyboard", "visual"),
    ) -> list[dict[str, Any]]:
        affected_set = set(affected)
        changed_ids = {ref.logical_id for ref in changed}
        return [
            {
                "stage": stage,
                "action": "regenerate" if (
                    any(record.producer_stage == stage and ref in affected_set for ref, record in graph._records.items())
                    or (stage == "storyboard" and "source.script" in changed_ids)
                    or (stage == "visual" and bool({"source.script", "style.reference"} & changed_ids))
                    or (stage == "voice_script" and "source.script" in changed_ids)
                    or (stage == "voice_director" and bool({"source.script", "voice_director.policy"} & changed_ids))
                    or (stage == "voice_tts" and bool({"source.script", "voice_director.policy"} & changed_ids))
                ) else "reuse",
                "artifact_versions": [ref.to_dict() for ref, record in sorted(graph._records.items())
                                      if record.producer_stage == stage and ref not in affected_set],
            }
            for stage in stages
        ]

    @staticmethod
    def _stage_execution_action(contract: dict[str, Any], stage: str) -> str:
        for item in contract.get("execution_plan", []):
            if isinstance(item, dict) and item.get("stage") == stage:
                return str(item.get("action"))
        return "regenerate"

    def _produced_artifacts(self, *, stage: str, run_id: str, contract: dict[str, Any],
                            artifacts: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Describe a stage's immutable output and exact input dependencies."""
        if len(artifacts) != 1:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"{stage} must publish exactly one graph artifact")
        artifact = dict(artifacts[0])
        path = artifact.get("path")
        version_id = artifact.get("version_id")
        if not isinstance(path, str) or not isinstance(version_id, str):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "stage artifact reference is invalid")
        relative_path = self._relative_artifact_path(path)
        absolute_path = self.store.artifact_path(relative_path)
        if not os.path.isfile(absolute_path) or version_id != f"sha256:{sha256_file(absolute_path)}":
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "stage artifact is missing or changed")
        dependencies: list[dict[str, str]] = []
        if stage == "storyboard":
            source = contract.get("runtime_inputs", {}).get("source.script")
            if not isinstance(source, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "storyboard source input is absent")
            dependencies.append(ArtifactRef.from_dict(source).to_dict())
            logical_id = "storyboard.output"
        elif stage == "visual":
            storyboard_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "storyboard"),
                None,
            )
            storyboard_payload = ((storyboard_event or {}).get("payload") or {})
            storyboard_outputs = storyboard_payload.get("produced_artifacts") or storyboard_payload.get("reused_artifacts") or []
            if len(storyboard_outputs) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual storyboard graph output is absent")
            dependencies.append(ArtifactRef.from_dict(storyboard_outputs[0]).to_dict())
            style = contract.get("runtime_inputs", {}).get("style.reference")
            if isinstance(style, dict):
                dependencies.append(ArtifactRef.from_dict(style).to_dict())
            logical_id = "visual.asset"
        elif stage == "voice_script":
            storyboard_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "storyboard"),
                None,
            )
            storyboard_payload = ((storyboard_event or {}).get("payload") or {})
            storyboard_outputs = storyboard_payload.get("produced_artifacts") or storyboard_payload.get("reused_artifacts") or []
            if len(storyboard_outputs) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-script storyboard graph output is absent")
            dependencies.append(ArtifactRef.from_dict(storyboard_outputs[0]).to_dict())
            logical_id = "voice_script.main"
        elif stage == "voice_director":
            storyboard_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "storyboard"),
                None,
            )
            voice_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_script"),
                None,
            )
            storyboard_outputs = ((storyboard_event or {}).get("payload") or {}).get("produced_artifacts") or ((storyboard_event or {}).get("payload") or {}).get("reused_artifacts") or []
            voice_outputs = ((voice_event or {}).get("payload") or {}).get("produced_artifacts") or ((voice_event or {}).get("payload") or {}).get("reused_artifacts") or []
            policy = contract.get("runtime_inputs", {}).get("voice_director.policy")
            if len(storyboard_outputs) != 1 or len(voice_outputs) != 1 or not isinstance(policy, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-director inputs are absent")
            dependencies.extend([
                ArtifactRef.from_dict(storyboard_outputs[0]).to_dict(),
                ArtifactRef.from_dict(voice_outputs[0]).to_dict(),
                ArtifactRef.from_dict(policy).to_dict(),
            ])
            logical_id = "voice_direction.main"
        elif stage == "voice_tts":
            director_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_director"),
                None,
            )
            director_payload = ((director_event or {}).get("payload") or {})
            director_outputs = director_payload.get("produced_artifacts") or director_payload.get("reused_artifacts") or []
            if len(director_outputs) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-tts voice-direction graph output is absent")
            dependencies.append(ArtifactRef.from_dict(director_outputs[0]).to_dict())
            logical_id = "voice_audio.main"
        else:
            raise ProductionError(ReasonCode.INTERNAL_ERROR.value, f"unknown output stage: {stage}")
        record = ArtifactRecord.from_dict({
            "logical_id": logical_id,
            "version_id": version_id,
            "path": relative_path,
            "producer": {"stage": stage, "run_id": run_id},
            "depends_on": dependencies,
        })
        # Validate the exact projection before the terminal event is durable.
        # A bad graph transition must never be appended and discovered only on
        # the next replay.
        self.store.artifact_graph().commit_stage_output(record)
        return [record.to_dict(state="available")]

    def _reuse_predecessor_stage(self, *, project: dict[str, Any], snapshot: ProductionSnapshot,
                                 contract: dict[str, Any], events: list[dict[str, Any]], stage: str) -> ProductionSnapshot:
        """Record verified stage reuse without scheduling a Provider operation."""
        predecessor = str(contract.get("predecessor_run_id", ""))
        terminal = next((event for event in reversed(events)
                         if event.get("run_id") == predecessor and event.get("event_type") == "stage_completed"
                         and (event.get("payload") or {}).get("stage") == stage), None)
        if terminal is None:
            raise ProductionError(ReasonCode.DEPENDENCY_UNSATISFIED.value, f"cannot reuse missing predecessor {stage}")
        payload = dict(terminal.get("payload") or {})
        # Reuse has provenance, but creates no new graph version. Keep the
        # predecessor graph reference for a following regenerated stage.
        if "produced_artifacts" in payload:
            payload["reused_artifacts"] = payload.pop("produced_artifacts")
        original_stage_run_id = str(payload.get("stage_run_id", ""))
        if not original_stage_run_id:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "reused stage has no authority")
        stage_run_id = f"reused-{stage}-{snapshot.run_id.removeprefix('run_')}"
        if not has_event(events, snapshot.run_id, "stage_scheduled", stage):
            self.store.events.append("stage_scheduled", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"stage": stage, "stage_invocation_id": stage_run_id})
        if not has_event(events, snapshot.run_id, "stage_run_attached", stage):
            self.store.events.append("stage_run_attached", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"stage": stage, "stage_run_id": stage_run_id})
        payload.update({"stage": stage, "stage_run_id": stage_run_id,
                        "reused_from": {"run_id": predecessor, "stage_run_id": original_stage_run_id}})
        self.store.events.append("stage_completed", project_id=project["project_id"], run_id=snapshot.run_id, payload=payload)
        return self._snapshot_and_project()

    def preview_revision(self, *, changed: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        return self._revision_preview(project=self.store.load_project(), changed=changed)

    def create_revision(
        self, *, changed: tuple[dict[str, Any], ...], requested_by: str, reason: str,
        preview_fingerprint: str, expected_last_event_hash: str, revision_id: str = "",
    ) -> dict[str, Any]:
        """Create an immutable successor-run contract from an exact preview."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            if not requested_by.strip() or not reason.strip() or not self._expected_last_hash_matches(expected_last_event_hash):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision request is stale or incomplete")
            preview = self._revision_preview(project=project, changed=changed)
            if preview["preview_fingerprint"] != preview_fingerprint or preview["last_event_hash"] != expected_last_event_hash:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "revision preview is stale")
            predecessor_run_id = preview["run_id"]
            successor_run_id = f"run_{uuid.uuid4().hex}"
            revision_id = revision_id or f"rev_{uuid.uuid4().hex}"
            if any(item["revision_id"] == revision_id for item in self.store.revisions().to_dict()["revisions"]):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision_id already exists")
            os.makedirs(self.paths.run_dir(successor_run_id), exist_ok=False)
            contract = self._create_contract(project, successor_run_id, predecessor_run_id=predecessor_run_id,
                                             revision_id=revision_id, reuse_manifest=tuple(preview["reused_artifacts"]),
                                             predecessor_selection=tuple(preview["predecessor_selection"]),
                                             successor_selection=tuple(preview["successor_selection"]),
                                             execution_plan=tuple(preview["execution_plan"]))
            atomic_write_json(self.paths.contract_file(successor_run_id), contract)
            revision = {
                "revision_id": revision_id, "predecessor_run_id": predecessor_run_id,
                "successor_run_id": successor_run_id, "requested_by": requested_by,
                "reason": reason, "changed": preview["changed"], "affected": preview["affected_artifacts"],
                "reuse_manifest": preview["reused_artifacts"], "preview_fingerprint": preview_fingerprint,
            }
            if preview["predecessor_selection"]:
                revision.update({
                    "predecessor_selection": preview["predecessor_selection"],
                    "successor_selection": preview["successor_selection"],
                    "execution_plan": preview["execution_plan"],
                })
            try:
                self.store.events.append("run_created", project_id=project["project_id"], run_id=successor_run_id,
                                         payload={"contract_fingerprint": contract["contract_fingerprint"], "dag_version": contract["dag_version"],
                                                  "stage_sequence": contract["stage_sequence"],
                                                  "predecessor_run_id": predecessor_run_id, "revision_id": revision_id,
                                                  "revision": revision})
            except BaseException:
                # An append error is ambiguous: it can be raised after the JSONL
                # record has reached durable storage. Delete only after a fresh
                # ledger read proves this exact successor contract was not
                # committed. Unknown read state is deliberately preserved for
                # recovery rather than risking deletion of the active contract.
                committed = True
                try:
                    committed = any(
                        event.get("event_type") == "run_created"
                        and event.get("run_id") == successor_run_id
                        and (event.get("payload") or {}).get("contract_fingerprint") == contract["contract_fingerprint"]
                        for event in self.store.events.read()
                    )
                except BaseException:
                    committed = True
                if not committed:
                    # This directory was created by this invocation and has no
                    # ledger authority. Remove only this exact known target.
                    shutil.rmtree(self.paths.run_dir(successor_run_id), ignore_errors=True)
                raise
            self.store.write_projection()
            return self.list_revisions()

    def register_artifact(
        self,
        *,
        logical_id: str,
        path: str,
        producer_stage: str,
        depends_on: tuple[dict[str, Any], ...] = (),
        expected_last_event_hash: str,
    ) -> dict[str, Any]:
        """Register a file-backed immutable version without selecting it."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            if not self._expected_last_hash_matches(expected_last_event_hash):
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "last event hash changed")
            absolute_path = self.store.artifact_path(path)
            snapshot = self.store.snapshot()
            if snapshot.status in {ProductionStatus.COMPLETED.value, ProductionStatus.NEEDS_REVIEW.value,
                                   ProductionStatus.FAILED.value}:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value,
                                      "completed predecessor requires a revision candidate")
            record = ArtifactRecord.from_dict({
                "logical_id": logical_id,
                "version_id": f"sha256:{sha256_file(absolute_path)}",
                "path": os.path.relpath(absolute_path, self.paths.root),
                "producer": {"stage": producer_stage, "run_id": snapshot.run_id},
                "depends_on": list(depends_on),
            })
            graph = self.store.artifact_graph()
            graph.register(record)
            self.store.events.append(
                "artifact_registered", project_id=project["project_id"], run_id=snapshot.run_id,
                payload={"artifact": record.to_dict(state="available")},
            )
            self.store.write_projection()
            graph, current = self.store.artifact_graph_snapshot()
            return self._artifact_graph_payload(project, graph, current)

    def register_revision_candidate(
        self, *, logical_id: str, path: str, producer_stage: str, depends_on: tuple[dict[str, Any], ...] = (),
        expected_last_event_hash: str,
    ) -> dict[str, Any]:
        """Register an available replacement without mutating a completed run."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if (not self._expected_last_hash_matches(expected_last_event_hash)
                    or snapshot.status not in {ProductionStatus.COMPLETED.value, ProductionStatus.PAUSED.value,
                                               ProductionStatus.NEEDS_REVIEW.value, ProductionStatus.FAILED.value}):
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "revision candidate requires a safe predecessor")
            absolute_path = self.store.artifact_path(path)
            record = ArtifactRecord.from_dict({
                "logical_id": logical_id,
                "version_id": f"sha256:{sha256_file(absolute_path)}",
                "path": os.path.relpath(absolute_path, self.paths.root),
                "producer": {"stage": producer_stage or "revision_candidate", "run_id": ""},
                "depends_on": list(depends_on),
            })
            graph = self.store.artifact_graph()
            graph.register(record)
            self.store.events.append(
                "artifact_registered", project_id=project["project_id"], run_id="",
                payload={"artifact": record.to_dict(state="available"), "revision_candidate": True},
            )
            self.store.write_projection()
            graph, current = self.store.artifact_graph_snapshot()
            return self._artifact_graph_payload(project, graph, current)

    def select_artifact_version(
        self, *, logical_id: str, version_id: str, expected_last_event_hash: str
    ) -> dict[str, Any]:
        """Select an available version and atomically record its exact invalidation closure."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            if not self._expected_last_hash_matches(expected_last_event_hash):
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "last event hash changed")
            snapshot = self.store.snapshot()
            if snapshot.status in {ProductionStatus.COMPLETED.value, ProductionStatus.NEEDS_REVIEW.value,
                                   ProductionStatus.FAILED.value}:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "completed predecessor requires a revision candidate")
            graph = self.store.artifact_graph()
            previous_version_id = graph.current_version(logical_id)
            target = graph.get(logical_id, version_id)
            invalidated = graph.invalidated_by(target.ref) if previous_version_id else ()
            # Invalidation is derived from the outgoing selected version, not the
            # incoming one. It is recomputed by ArtifactGraph during every read.
            if previous_version_id:
                from manju.production.artifacts import ArtifactRef
                invalidated = graph.invalidated_by(ArtifactRef(logical_id, previous_version_id))
            graph.select(
                logical_id=logical_id, version_id=version_id,
                previous_version_id=previous_version_id,
                recorded_invalidated=[item.to_dict() for item in invalidated],
            )
            self.store.events.append(
                "artifact_version_selected", project_id=project["project_id"], run_id=snapshot.run_id,
                payload={
                    "logical_id": logical_id,
                    "version_id": version_id,
                    "previous_version_id": previous_version_id,
                    "invalidated": [item.to_dict() for item in invalidated],
                },
            )
            self.store.write_projection()
            graph, current = self.store.artifact_graph_snapshot()
            return self._artifact_graph_payload(project, graph, current)

    def decide_approval(self, request_id: str, *, decision: str, reviewer: str,
                        expected_last_event_hash: str) -> ProductionSnapshot:
        if decision not in {"approve", "reject"} or not reviewer.strip():
            raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "invalid decision or reviewer")
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash) or snapshot.status != ProductionStatus.AWAITING_APPROVAL.value:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "approval state changed")
            approval = next((item for item in self.list_approvals()
                             if item["approval_request"].get("request_id") == request_id), None)
            if approval is None or approval["status"] != "awaiting_approval":
                raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "approval is not pending")
            request = ApprovalRequest.from_dict(approval["approval_request"])
            if datetime.now(timezone.utc) > datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")):
                raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "approval expired")
            key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
            event_type = "approval_approved" if decision == "approve" else "approval_rejected"
            self.store.events.append(event_type, project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"request_id": request_id, "reviewer": reviewer, "key_id": key_id})
            return self._snapshot_and_project()

    def issue_grant(self, request_id: str, *, grant_id: str, issued_by: str, issued_at: str = "",
                    expected_last_event_hash: str) -> ProductionSnapshot:
        """Issue only a signed authority record. No provider execution happens in M2.0."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash):
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "last event hash changed")
            approval = next((item for item in self.list_approvals()
                             if item["approval_request"].get("request_id") == request_id), None)
            if approval is None or approval["status"] != "approved":
                raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "approval was not approved")
            request = ApprovalRequest.from_dict(approval["approval_request"])
            if (snapshot.status != ProductionStatus.AWAITING_APPROVAL.value
                    or request.project_id != project["project_id"] or request.run_id != snapshot.run_id):
                raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "approval does not bind the active run")
            recorded_at = utc_now()
            if datetime.now(timezone.utc) > datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")):
                raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "approval expired")
            key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
            key = self.store.events.key_provider.get_key(key_id) if self.store.events.key_provider else None
            if not key:
                raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value)
            grant = Grant.issue(request, grant_id=grant_id, issued_by=issued_by,
                                issued_at=recorded_at, key_id=key_id, key=key)
            self.store.events.append("grant_issued", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"grant": grant.to_dict(), "key_id": key_id})
            return self._snapshot_and_project()

    def reconcile_operation(self, operation: OperationRecord, *, expected_last_event_hash: str) -> ProductionSnapshot:
        """Record a provider reconciliation; callers supply an observed, non-unknown outcome."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash) or snapshot.status != ProductionStatus.BLOCKED.value:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "operation state changed")
            if operation.status != "settled" or operation.outcome not in {"succeeded", "failed"}:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "reconciliation requires a final observed outcome")
            events = self.store.events.read()
            approval_event = next((event for event in events if event.get("run_id") == snapshot.run_id and event.get("event_type") == "approval_requested"), None)
            grant_event = next((event for event in events if event.get("run_id") == snapshot.run_id and event.get("event_type") == "grant_issued"), None)
            if approval_event is not None or grant_event is not None:
                grant_stage = str(((grant_event or {}).get("payload") or {}).get("grant", {}).get("stage", ""))
                if grant_stage == "voice_tts":
                    grant = self._active_voice_tts_grant(events, project, snapshot.run_id)
                else:
                    grant = self._active_visual_grant(events, project, snapshot.run_id)
                if grant.settlement_mode != "provider_evidence":
                    raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "contractual tariff grants require the dedicated settlement command")
            previous = None
            for event in events:
                if event.get("run_id") != snapshot.run_id or event.get("event_type") not in {
                    "call_reserved", "call_submitted", "call_settled", "call_reconciled",
                }:
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                candidate = payload.get("operation")
                if isinstance(candidate, dict) and candidate.get("operation_id") == operation.operation_id:
                    previous = OperationRecord.from_dict(candidate)
            if previous is None or previous.reconcile(
                outcome=operation.outcome,
                result_fingerprint=operation.result_fingerprint,
                usage=operation.usage,
            ) != operation:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "operation cannot be reconciled")
            key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
            self.store.events.append("call_reconciled", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"operation": operation.to_dict(), "key_id": key_id})
            return self._snapshot_and_project()

    def _manual_key(self, project: dict[str, Any]) -> tuple[str, bytes]:
        key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
        key = self.store.events.key_provider.get_key(key_id) if self.store.events.key_provider else None
        if not key:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "manual package signing key is unavailable")
        return key_id, key

    def _manual_dispatch(self, events: list[dict[str, Any]], run_id: str, operation_id: str = "") -> tuple[ManualDispatchPackage, str] | None:
        for event in events:
            if event.get("run_id") != run_id or event.get("event_type") != "manual_dispatch_prepared":
                continue
            dispatch = ManualDispatchPackage.from_dict((event.get("payload") or {}).get("dispatch"))
            if not operation_id or dispatch.operation_id == operation_id:
                return dispatch, str((event.get("payload") or {}).get("dispatch_sha256", ""))
        return None

    @staticmethod
    def _latest_operation(events: list[dict[str, Any]], run_id: str, operation_id: str) -> OperationRecord | None:
        result = None
        for event in events:
            if event.get("run_id") != run_id or event.get("event_type") not in {"call_reserved", "call_submitted", "call_settled", "call_reconciled"}:
                continue
            value = (event.get("payload") or {}).get("operation")
            if isinstance(value, dict) and value.get("operation_id") == operation_id:
                result = OperationRecord.from_dict(value)
        return result

    def prepare_manual_dispatch(self, *, expected_last_event_hash: str) -> dict[str, Any]:
        """Create one signed offline dispatch package; no provider transport occurs."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash) or snapshot.status != ProductionStatus.RUNNING.value:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "manual dispatch state changed")
            if project["production"]["visual"].get("provider_profile") == "mock":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual dispatch is only for a production profile")
            from manju.production.runtime_profiles import is_manual_sync_profile
            if not is_manual_sync_profile(str(project["production"]["visual"].get("provider_profile", ""))):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "profile is not declared manual_sync")
            events = self.store.events.read()
            grant = self._active_visual_grant(events, project, snapshot.run_id)
            binding = grant.operation_bindings[0]
            existing = self._manual_dispatch(events, snapshot.run_id, str(binding["operation_id"]))
            if existing is not None:
                return {"dispatch": existing[0].to_dict(), "dispatch_sha256": existing[1], "path": os.path.join(self.paths.manual_dispatches_dir, existing[0].package_id + ".json"), "last_event_hash": snapshot.last_event_hash}
            operation = self._latest_operation(events, snapshot.run_id, str(binding["operation_id"]))
            key_id, key = self._manual_key(project)
            if operation is None:
                operation = OperationRecord(str(binding["operation_id"]), grant.grant_id, str(binding["kind"]), str(binding["input_fingerprint"]), grant.provider_profile, reservation_amount=grant.maximum_amount, currency=grant.currency, provider_request=binding.get("provider_request"))
                self.store.events.append("call_reserved", project_id=project["project_id"], run_id=snapshot.run_id, payload={"operation": operation.to_dict(), "key_id": key_id})
                events = self.store.events.read()
            if operation.status != "reserved":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual dispatch requires exactly one reserved operation")
            dispatch = ManualDispatchPackage.create(project_id=project["project_id"], run_id=snapshot.run_id, stage_run_id=grant.stage_run_id,
                                                    operation_id=operation.operation_id, grant_id=grant.grant_id, provider_profile=grant.provider_profile,
                                                    operation_kind=operation.kind, input_fingerprint=operation.input_fingerprint,
                                                    provider_request=operation.provider_request or {}, maximum_amount=grant.maximum_amount,
                                                    currency=grant.currency, key_id=key_id).sign(key)
            digest = dispatch.sha256()
            path = os.path.join(self.paths.manual_dispatches_dir, dispatch.package_id + ".json")
            atomic_write_json(path, dispatch.to_dict())
            self.store.events.append("manual_dispatch_prepared", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"dispatch": dispatch.to_dict(), "dispatch_sha256": digest, "key_id": key_id})
            updated = self._snapshot_and_project()
            return {"dispatch": dispatch.to_dict(), "dispatch_sha256": digest, "path": path, "last_event_hash": updated.last_event_hash}

    def import_manual_result(self, result: ManualResultPackage, *, package_dir: str, expected_last_event_hash: str) -> ProductionSnapshot:
        """Import one signed worker result and deliberately settle its cost as unknown."""
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash) or snapshot.status != ProductionStatus.BLOCKED.value:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "manual result state changed")
            events = self.store.events.read()
            found = self._manual_dispatch(events, snapshot.run_id, result.operation_id)
            if found is None:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual dispatch is unavailable")
            dispatch, digest = found
            key_id, key = self._manual_key(project)
            dispatch.verify(key)
            result.verify(key)
            if result.key_id != key_id or result.dispatch_sha256 != digest or result.package_id != dispatch.package_id or result.claim_token != dispatch.claim_token:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual result does not bind dispatch")
            if any(event.get("run_id") == snapshot.run_id and event.get("event_type") == "manual_result_imported" for event in events):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual result was already imported")
            operation = self._latest_operation(events, snapshot.run_id, result.operation_id)
            if operation is None or operation.status != "reserved":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual result needs reserved operation")
            stage_dir = self.paths.visual_dir(snapshot.run_id, dispatch.stage_run_id)
            os.makedirs(stage_dir, exist_ok=True)
            result_fingerprint = ""
            if result.outcome == "succeeded":
                source = os.path.realpath(os.path.join(os.path.abspath(package_dir), result.artifact_path))
                if os.path.dirname(source) != os.path.realpath(package_dir) or not os.path.isfile(source) or os.path.getsize(source) != result.artifact_size or manual_sha256_file(source) != result.artifact_sha256:
                    raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "manual result artifact is missing or changed")
                pending_name = self.visual_adapter._pending_name(operation.operation_id)
                pending = self.visual_adapter._safe_output(stage_dir, pending_name)
                with open(source, "rb") as handle:
                    atomic_write_bytes(pending, handle.read())
                result_fingerprint = "sha256:" + result.artifact_sha256
                self._configure_visual_receipt_signer(project)
                receipt = {"schema_version": "1", "stage_run_id": dispatch.stage_run_id,
                           "operation": {"operation_id": operation.operation_id, "provider_job_id": "manual:" + dispatch.package_id, "result_fingerprint": result_fingerprint},
                           "artifact": {"pending_path": pending_name, "sha256": result.artifact_sha256, "media_type": result.artifact_media_type, "size": result.artifact_size},
                           "usage": {"worker_id": result.worker_id, "raw_response_sha256": result.raw_response_sha256},
                           "cost": {"actual_amount": "", "currency": "", "cost_status": "unknown", "cost_source": "unknown"}}
                atomic_write_json(self.visual_adapter._receipt_path(stage_dir), self.visual_adapter._seal_receipt(receipt))
            submitted = operation.submit("manual:" + dispatch.package_id)
            settled = submitted.settle(outcome="outcome_unknown", result_fingerprint=result_fingerprint,
                                       usage={"cost_status": "unknown", "cost_source": "unknown", "worker_id": result.worker_id})
            self.store.events.append("call_submitted", project_id=project["project_id"], run_id=snapshot.run_id, payload={"operation": submitted.to_dict(), "key_id": key_id})
            self.store.events.append("call_settled", project_id=project["project_id"], run_id=snapshot.run_id, payload={"operation": settled.to_dict(), "key_id": key_id})
            self.store.events.append("manual_result_imported", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"result": result.to_dict(), "dispatch_sha256": digest, "key_id": key_id})
            return self._snapshot_and_project()

    def reconcile_manual_cost(self, evidence: ManualBillingEvidence, *, package_dir: str, expected_last_event_hash: str) -> ProductionSnapshot:
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash) or snapshot.status != ProductionStatus.BLOCKED.value:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "manual cost state changed")
            events = self.store.events.read()
            found = self._manual_dispatch(events, snapshot.run_id, evidence.operation_id)
            if found is None:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual dispatch is unavailable")
            dispatch, digest = found
            grant = self._active_visual_grant(events, project, snapshot.run_id)
            if grant.settlement_mode != "provider_evidence":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "contractual tariff grants reject provider billing evidence")
            key_id, key = self._manual_key(project)
            evidence.verify(key)
            if evidence.key_id != key_id or evidence.dispatch_sha256 != digest or evidence.claim_token != dispatch.claim_token or evidence.currency != dispatch.currency:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual billing evidence does not bind dispatch")
            if evidence.evidence_path:
                path = os.path.realpath(os.path.join(os.path.abspath(package_dir), evidence.evidence_path))
                if os.path.dirname(path) != os.path.realpath(package_dir) or not os.path.isfile(path) or manual_sha256_file(path) != evidence.evidence_sha256:
                    raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "manual billing evidence is missing or changed")
            operation = self._latest_operation(events, snapshot.run_id, evidence.operation_id)
            if operation is None or operation.status != "settled" or operation.outcome != "outcome_unknown":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual billing needs an unknown settled operation")
            result_event = next((event for event in events if event.get("run_id") == snapshot.run_id and event.get("event_type") == "manual_result_imported" and ((event.get("payload") or {}).get("result") or {}).get("operation_id") == evidence.operation_id), None)
            result = ManualResultPackage.from_dict(((result_event or {}).get("payload") or {}).get("result"))
            if result.outcome != evidence.outcome:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual billing outcome differs from imported result")
            self.store.events.append("manual_cost_reconciled", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"evidence": evidence.to_dict(), "dispatch_sha256": digest, "key_id": key_id})
            reconciled = operation.reconcile(outcome=evidence.outcome, result_fingerprint=operation.result_fingerprint,
                                             usage={"actual_amount": evidence.actual_amount, "currency": evidence.currency,
                                                    "cost_status": "final", "cost_source": "provider_ledger", "provider_reference": evidence.provider_reference,
                                                    "reviewer": evidence.reviewer})
            self.store.events.append("call_reconciled", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"operation": reconciled.to_dict(), "key_id": key_id})
            return self._snapshot_and_project()

    def settle_manual_contractual_tariff(self, *, operation_id: str, expected_last_event_hash: str) -> ProductionSnapshot:
        """Settle an imported manual result at the approved contractual tariff.

        This validates a pre-agreed price, not a provider invoice or upstream
        actual cost. No caller-supplied amount, currency, or evidence is used.
        """
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if not self._expected_last_hash_matches(expected_last_event_hash) or snapshot.status != ProductionStatus.BLOCKED.value:
                raise ProductionError(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "contractual tariff state changed")
            events = self.store.events.read()
            dispatch_found = self._manual_dispatch(events, snapshot.run_id, operation_id)
            if dispatch_found is None:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual dispatch is unavailable")
            dispatch, digest = dispatch_found
            grant = self._active_visual_grant(events, project, snapshot.run_id)
            tariff = grant.contractual_tariff
            if grant.settlement_mode != "contractual_tariff" or not isinstance(tariff, dict):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "grant does not authorize contractual tariff settlement")
            result_event = next((event for event in events if event.get("run_id") == snapshot.run_id and event.get("event_type") == "manual_result_imported" and ((event.get("payload") or {}).get("result") or {}).get("operation_id") == operation_id), None)
            if result_event is None:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "manual result is unavailable")
            result = ManualResultPackage.from_dict((result_event.get("payload") or {}).get("result"))
            if result.outcome not in {"succeeded", "failed"}:
                raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "unknown manual outcome cannot use a contractual tariff")
            key_id, _ = self._manual_key(project)
            expected_usage = contractual_tariff_usage(tariff, result.outcome)
            operation = self._latest_operation(events, snapshot.run_id, operation_id)
            if operation is None or operation.status != "settled":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "contractual tariff needs a settled operation")
            if operation.outcome == result.outcome and operation.usage == expected_usage:
                return snapshot
            if operation.outcome != "outcome_unknown":
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "contractual tariff result was already reconciled")
            reconciled = operation.reconcile(outcome=result.outcome, result_fingerprint=operation.result_fingerprint, usage=expected_usage)
            settlement_payload = {"operation_id": operation_id, "dispatch_sha256": digest, "tariff": dict(tariff),
                                  "outcome": result.outcome, "key_id": key_id}
            existing_settlement = next((event for event in events if event.get("run_id") == snapshot.run_id
                                        and event.get("event_type") == "manual_contractual_tariff_settled"
                                        and (event.get("payload") or {}).get("operation_id") == operation_id), None)
            if existing_settlement is not None and (existing_settlement.get("payload") or {}) != settlement_payload:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "contractual tariff settlement does not match dispatch")
            if result.outcome == "succeeded":
                stage_dir = self.paths.visual_dir(snapshot.run_id, dispatch.stage_run_id)
                self._configure_visual_receipt_signer(project)
                self.visual_adapter.settle_contractual_tariff_receipt(
                    stage_run_id=dispatch.stage_run_id, output_dir=stage_dir, operation=reconciled.to_dict(),
                    tariff=tariff,
                )
            if existing_settlement is None:
                self.store.events.append("manual_contractual_tariff_settled", project_id=project["project_id"], run_id=snapshot.run_id,
                                         payload=settlement_payload)
            self.store.events.append("call_reconciled", project_id=project["project_id"], run_id=snapshot.run_id,
                                     payload={"operation": reconciled.to_dict(), "key_id": key_id})
            return self._snapshot_and_project()

    def _publish(self, snapshot: ProductionSnapshot) -> ProductionSnapshot:
        for listener in self.listeners:
            listener(snapshot)
        return snapshot

    def _expected_last_hash_matches(self, expected_last_event_hash: str) -> bool:
        """Accept the immediately preceding hash only for a post-return lease release audit event."""
        events = self.store.events.read()
        if not events:
            return expected_last_event_hash == ""
        latest = events[-1]
        return latest.get("event_hash") == expected_last_event_hash or (
            latest.get("event_type") == "execution_lease_released"
            and latest.get("previous_hash") == expected_last_event_hash
        )

    def _snapshot_and_project(self) -> ProductionSnapshot:
        self.store.write_projection()
        return self._publish(self.store.snapshot())

    @staticmethod
    def _configured_model() -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            _, model_name, _ = get_ai_config()
        return model_name or "unconfigured"

    def _create_contract(self, project: dict[str, Any], run_id: str, *, predecessor_run_id: str = "",
                          revision_id: str = "", reuse_manifest: tuple[dict[str, Any], ...] = (),
                          predecessor_selection: tuple[dict[str, Any], ...] = (),
                          successor_selection: tuple[dict[str, Any], ...] = (),
                          execution_plan: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
        storyboard = project["production"]["storyboard"]
        visual = project["production"]["visual"]
        voice_script = project["production"].get("voice_script", {"enabled": False})
        voice_director = project["production"].get("voice_director", {"enabled": False})
        voice_tts = project["production"].get("voice_tts", {"enabled": False})
        if voice_tts.get("enabled"):
            configured_profile = voice_tts.get("model_profile")
            adapter_profile = self.voice_tts_adapter.model_profile
            if configured_profile != adapter_profile:
                raise ProductionError(
                    ReasonCode.PROJECT_CONTRACT_CHANGED.value,
                    "voice-tts model profile does not match the configured project profile",
                )
            if voice_tts.get("mode", "offline_mock") == "offline_mock" and not getattr(
                self.voice_tts_adapter.model_port, "idempotency_supported", False
            ):
                raise ProductionError(
                    ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value,
                    "voice-tts model must support idempotent calls",
                )
            # Paid synchronous providers (mode=paid_siliconflow) may omit
            # idempotency: crash recovery fails closed through the durable
            # call-receipt state machine and never blindly re-submits.
        runtime_inputs = self._runtime_inputs(project, run_id, successor_selection)
        source_input = runtime_inputs["source.script"]
        if voice_tts.get("enabled"):
            dag_version = M4_2_DAG_VERSION
            stage_sequence = ["storyboard", "voice_script", "voice_director", "voice_tts"] + (["visual"] if visual.get("enabled") else [])
        elif voice_director.get("enabled"):
            dag_version = M4_1_DAG_VERSION
            stage_sequence = ["storyboard", "voice_script", "voice_director"] + (["visual"] if visual.get("enabled") else [])
        elif voice_script.get("enabled"):
            dag_version = M4_DAG_VERSION
            stage_sequence = ["storyboard", "voice_script"] + (["visual"] if visual.get("enabled") else [])
        else:
            dag_version = M2_DAG_VERSION if visual.get("enabled") else DAG_VERSION
            stage_sequence = list(stages_for_dag(dag_version))
        contract = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now(),
            "project_spec_fingerprint": fingerprint(project),
            "source_fingerprint": source_input["version_id"][7:],
            "storyboard_source_fingerprint": source_input["version_id"][7:],
            "dag_version": dag_version,
            "stage_sequence": stage_sequence,
            "adapter_contract_versions": {
                "storyboard": self.storyboard_adapter.contract_version,
                "visual": self.visual_adapter.contract_version if visual.get("enabled") else "",
                "voice_script": self.voice_script_adapter.contract_version if voice_script.get("enabled") else "",
                "voice_director": self.voice_director_adapter.contract_version if voice_director.get("enabled") else "",
                "voice_tts": self.voice_tts_adapter.contract_version if voice_tts.get("enabled") else "",
            },
            "models": {
                "storyboard": self._configured_model(),
                "voice_director": self.voice_director_adapter.model_profile if voice_director.get("enabled") else "",
                "voice_tts": self.voice_tts_adapter.model_profile if voice_tts.get("enabled") else "",
            },
            "prompt_versions": {
                "storyboard_supervisor": SUPERVISOR_AGENT_VERSION,
            },
            "tool_protocol_versions": {
                "storyboard_supervisor": SUPERVISOR_TOOLSET_VERSION,
            },
            "code_version": __version__,
            "provider_profiles": project.get("provider_profiles", {}),
            "integrity": project.get("integrity", {}),
            "storyboard_settings": storyboard,
            "visual_settings": visual,
            "voice_script_settings": voice_script,
            "voice_director_settings": voice_director,
            "voice_tts_settings": voice_tts,
            "predecessor_run_id": predecessor_run_id,
            "revision_id": revision_id,
            "reuse_manifest": list(reuse_manifest),
            "predecessor_selection": list(predecessor_selection),
            "successor_selection": list(successor_selection),
            "execution_plan": list(execution_plan),
            "runtime_inputs": runtime_inputs,
        }
        contract["contract_fingerprint"] = fingerprint(contract)
        return contract

    def _validate_runtime_contract(self, contract: dict[str, Any]) -> None:
        expected = {
            "model": contract.get("models", {}).get("storyboard"),
            "voice_director_model": contract.get("models", {}).get("voice_director", ""),
            "voice_tts_model": contract.get("models", {}).get("voice_tts", ""),
            "adapter": contract.get("adapter_contract_versions", {}).get("storyboard"),
            "prompt": contract.get("prompt_versions", {}).get("storyboard_supervisor"),
            "tool": contract.get("tool_protocol_versions", {}).get("storyboard_supervisor"),
            "code": contract.get("code_version"),
            "visual_adapter": contract.get("adapter_contract_versions", {}).get("visual", ""),
            "voice_script_adapter": contract.get("adapter_contract_versions", {}).get("voice_script", ""),
            "voice_director_adapter": contract.get("adapter_contract_versions", {}).get("voice_director", ""),
            "voice_tts_adapter": contract.get("adapter_contract_versions", {}).get("voice_tts", ""),
            "stage_sequence": list(stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            )),
        }
        actual = {
            "model": self._configured_model(),
            "voice_director_model": self.voice_director_adapter.model_profile if "voice_director" in stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            ) else "",
            "voice_tts_model": self.voice_tts_adapter.model_profile if "voice_tts" in stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            ) else "",
            "adapter": self.storyboard_adapter.contract_version,
            "prompt": SUPERVISOR_AGENT_VERSION,
            "tool": SUPERVISOR_TOOLSET_VERSION,
            "code": __version__,
            "visual_adapter": self.visual_adapter.contract_version if "visual" in stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            ) else "",
            "voice_script_adapter": self.voice_script_adapter.contract_version if "voice_script" in stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            ) else "",
            "voice_director_adapter": self.voice_director_adapter.contract_version if "voice_director" in stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            ) else "",
            "voice_tts_adapter": self.voice_tts_adapter.contract_version if "voice_tts" in stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            ) else "",
            "stage_sequence": list(stages_for_dag(
                str(contract.get("dag_version", "")), contract.get("stage_sequence")
            )),
        }
        legacy_visual = expected["visual_adapter"] == "visual-adapter-m2-3-v1"
        if legacy_visual:
            actual["visual_adapter"] = expected["visual_adapter"]
        if actual != expected:
            raise ProductionError(
                ReasonCode.PROJECT_CONTRACT_CHANGED.value,
                "当前模型、adapter 或代码版本与活动 run 合同不一致",
            )

    def _validate_stage_authority(
        self,
        project: dict[str, Any],
        events: list[dict[str, Any]],
        snapshot: ProductionSnapshot,
        contract: dict[str, Any],
    ) -> None:
        # A completed upstream stage remains an input authority after a downstream
        # stage completes.  Never validate only the most recent terminal event.
        terminals = [
            event for event in events
            if event.get("run_id") == snapshot.run_id
            and event.get("event_type") in {"stage_completed", "stage_needs_review", "stage_failed"}
        ]
        # A fresh service has no adapter-local receipt signer yet.  Configure it
        # before inspecting any completed visual terminal, so status, doctor and
        # stop/restart paths verify the same authenticated receipt as advance.
        if any((event.get("payload") or {}).get("stage") == "visual" for event in terminals):
            self._configure_visual_receipt_signer(project)
        for terminal in terminals:
            self._validate_terminal_stage_authority(terminal, events, snapshot, contract)

    def _validate_terminal_stage_authority(
        self,
        terminal: dict[str, Any],
        events: list[dict[str, Any]],
        snapshot: ProductionSnapshot,
        contract: dict[str, Any],
    ) -> None:
        payload = terminal.get("payload") if isinstance(terminal.get("payload"), dict) else {}
        stage = str(payload.get("stage", ""))
        if stage not in {"storyboard", "voice_script", "voice_director", "voice_tts", "visual"}:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "terminal stage is unsupported")
        authority_path = str(payload.get("authority_path", ""))
        expected_hash = str(payload.get("authority_hash", ""))
        if terminal.get("event_type") == "stage_failed" and not authority_path and not expected_hash:
            return
        if not authority_path or not expected_hash:
            raise ProductionError(
                ReasonCode.STAGE_INTEGRITY_FAILED.value,
                "终态阶段缺少权威账本引用",
            )
        absolute = os.path.realpath(os.path.join(self.paths.root, authority_path))
        root = os.path.realpath(self.paths.root)
        try:
            contained = os.path.commonpath([root, absolute]) == root
        except ValueError:
            contained = False
        if not contained or not os.path.isfile(absolute) or sha256_file(absolute) != expected_hash:
            raise ProductionError(
                ReasonCode.STAGE_INTEGRITY_FAILED.value,
                "子阶段权威账本缺失、越界或内容已改变",
            )
        authority_files = payload.get("authority_files")
        if not isinstance(authority_files, list) or not authority_files:
            raise ProductionError(
                ReasonCode.STAGE_INTEGRITY_FAILED.value,
                "终态阶段缺少完整 authority file 集合",
            )
        for item in authority_files:
            if not isinstance(item, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value)
            candidate = os.path.realpath(os.path.join(self.paths.root, str(item.get("path", ""))))
            try:
                item_contained = os.path.commonpath([root, candidate]) == root
            except ValueError:
                item_contained = False
            if (
                not item_contained
                or not os.path.isfile(candidate)
                or sha256_file(candidate) != str(item.get("sha256", ""))
            ):
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "子阶段 authority file 集合校验失败",
                )
        artifacts = payload.get("artifacts")
        if terminal.get("event_type") in {"stage_completed", "stage_needs_review"} and (
            not isinstance(artifacts, list) or not artifacts
        ):
            raise ProductionError(
                ReasonCode.STAGE_INTEGRITY_FAILED.value,
                "终态阶段缺少可验证产物",
            )
        for artifact in artifacts or []:
            if not isinstance(artifact, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value)
            artifact_path = os.path.realpath(os.path.join(self.paths.root, str(artifact.get("path", ""))))
            version_id = str(artifact.get("version_id", ""))
            try:
                artifact_contained = os.path.commonpath([root, artifact_path]) == root
            except ValueError:
                artifact_contained = False
            if (
                not artifact_contained
                or not os.path.isfile(artifact_path)
                or not version_id.startswith("sha256:")
                or sha256_file(artifact_path) != version_id.removeprefix("sha256:")
            ):
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "阶段产物缺失、越界或内容已改变",
                )
        if payload.get("reused_from"):
            # The predecessor's authority files were verified above by their
            # recorded hashes. A reused stage deliberately has no successor-
            # local adapter directory to inspect.
            return
        if stage == "storyboard":
            scheduled_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
            output_dir = self.paths.storyboard_dir(snapshot.run_id, scheduled_id)
            inspected = self.storyboard_adapter.inspect(
                stage_run_id=scheduled_id,
                output_dir=output_dir,
                expected=self._storyboard_expected(contract),
            )
            if inspected is None or inspected.stage_run_id != payload.get("stage_run_id"):
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "分镜子 run 身份与顶层事件不一致",
                )
        elif stage == "voice_script":
            scheduled_id = f"voice-script-{snapshot.run_id.removeprefix('run_')}"
            output_dir = self.paths.voice_script_dir(snapshot.run_id, scheduled_id)
            storyboard_event = next(
                (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "storyboard"),
                None,
            )
            storyboard_payload = (storyboard_event or {}).get("payload") or {}
            storyboard_outputs = storyboard_payload.get("produced_artifacts") or storyboard_payload.get("reused_artifacts") or []
            if len(storyboard_outputs) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-script storyboard authority is absent")
            storyboard_ref = ArtifactRef.from_dict(storyboard_outputs[0]).to_dict()
            inspected = self.voice_script_adapter.inspect(
                stage_run_id=scheduled_id, output_dir=output_dir, storyboard_ref=storyboard_ref,
            )
            expected_authority = os.path.relpath(
                os.path.join(output_dir, self.voice_script_adapter.authority_name), self.paths.root,
            )
            expected_artifacts = {
                (os.path.realpath(str(item.get("path", ""))), str(item.get("version_id", "")))
                for item in (inspected.artifacts if inspected is not None else ())
            }
            recorded_artifacts = {
                (os.path.realpath(os.path.join(self.paths.root, str(item.get("path", "")))), str(item.get("version_id", "")))
                for item in artifacts or [] if isinstance(item, dict)
            }
            if (
                inspected is None or inspected.status != "completed"
                or inspected.stage_run_id != payload.get("stage_run_id")
                or payload.get("stage_run_id") != scheduled_id
                or authority_path != expected_authority
                or inspected.authority_hash != expected_hash
                or os.path.realpath(inspected.authority_path) != absolute
                or expected_artifacts != recorded_artifacts
            ):
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "voice-script authority does not match the terminal event",
                )
        elif stage == "voice_director":
            scheduled_id = f"voice-director-{snapshot.run_id.removeprefix('run_')}"
            output_dir = self.paths.voice_director_dir(snapshot.run_id, scheduled_id)
            storyboard_event = next(
                (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "storyboard"), None,
            )
            voice_event = next(
                (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_script"), None,
            )
            storyboard_outputs = ((storyboard_event or {}).get("payload") or {}).get("produced_artifacts") or ((storyboard_event or {}).get("payload") or {}).get("reused_artifacts") or []
            voice_outputs = ((voice_event or {}).get("payload") or {}).get("produced_artifacts") or ((voice_event or {}).get("payload") or {}).get("reused_artifacts") or []
            policy_ref = contract.get("runtime_inputs", {}).get("voice_director.policy")
            if len(storyboard_outputs) != 1 or len(voice_outputs) != 1 or not isinstance(policy_ref, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-director authority inputs are absent")
            inspected = self.voice_director_adapter.inspect(
                stage_run_id=scheduled_id, output_dir=output_dir,
                storyboard_ref=ArtifactRef.from_dict(storyboard_outputs[0]).to_dict(),
                voice_script_ref=ArtifactRef.from_dict(voice_outputs[0]).to_dict(),
                policy_ref=ArtifactRef.from_dict(policy_ref).to_dict(),
                voice_script_path=os.path.join(output_dir, ".runtime_inputs", f"voice_script.main-{ArtifactRef.from_dict(voice_outputs[0]).version_id[7:]}.bin"),
                policy_path=os.path.join(output_dir, ".runtime_inputs", f"voice_director.policy-{ArtifactRef.from_dict(policy_ref).version_id[7:]}.bin"),
            )
            expected_authority = os.path.relpath(os.path.join(output_dir, self.voice_director_adapter.authority_name), self.paths.root)
            expected_artifacts = {
                (os.path.realpath(str(item.get("path", ""))), str(item.get("version_id", "")))
                for item in (inspected.artifacts if inspected is not None else ())
            }
            recorded_artifacts = {
                (os.path.realpath(os.path.join(self.paths.root, str(item.get("path", "")))), str(item.get("version_id", "")))
                for item in artifacts or [] if isinstance(item, dict)
            }
            if (
                inspected is None or inspected.status != "completed"
                or inspected.stage_run_id != payload.get("stage_run_id")
                or payload.get("stage_run_id") != scheduled_id
                or authority_path != expected_authority
                or inspected.authority_hash != expected_hash
                or os.path.realpath(inspected.authority_path) != absolute
                or expected_artifacts != recorded_artifacts
            ):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-director authority does not match terminal event")
        elif stage == "voice_tts":
            scheduled_id = f"voice-tts-{snapshot.run_id.removeprefix('run_')}"
            output_dir = self.paths.voice_tts_dir(snapshot.run_id, scheduled_id)
            director_event = next(
                (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_director"), None,
            )
            director_outputs = ((director_event or {}).get("payload") or {}).get("produced_artifacts") or ((director_event or {}).get("payload") or {}).get("reused_artifacts") or []
            if len(director_outputs) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-tts authority input is absent")
            direction_ref = ArtifactRef.from_dict(director_outputs[0]).to_dict()
            inspected = self.voice_tts_adapter.inspect(
                stage_run_id=scheduled_id, output_dir=output_dir,
                voice_direction_ref=direction_ref,
                voice_direction_path=os.path.join(output_dir, ".runtime_inputs", f"voice_direction.main-{direction_ref['version_id'][7:]}.bin"),
            )
            expected_authority = os.path.relpath(os.path.join(output_dir, self.voice_tts_adapter.authority_name), self.paths.root)
            expected_artifacts = {
                (os.path.realpath(str(item.get("path", ""))), str(item.get("version_id", "")))
                for item in (inspected.artifacts if inspected is not None else ())
            }
            recorded_artifacts = {
                (os.path.realpath(os.path.join(self.paths.root, str(item.get("path", "")))), str(item.get("version_id", "")))
                for item in artifacts or [] if isinstance(item, dict)
            }
            if (
                inspected is None or inspected.status != "completed"
                or inspected.stage_run_id != payload.get("stage_run_id")
                or payload.get("stage_run_id") != scheduled_id
                or authority_path != expected_authority
                or inspected.authority_hash != expected_hash
                or os.path.realpath(inspected.authority_path) != absolute
                or expected_artifacts != recorded_artifacts
            ):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-tts authority does not match terminal event")
        elif stage == "visual":
            scheduled_id = f"visual-{snapshot.run_id.removeprefix('run_')}"
            output_dir = self.paths.visual_dir(snapshot.run_id, scheduled_id)
            inspected = self.visual_adapter.inspect(stage_run_id=scheduled_id, output_dir=output_dir)
            expected_authority = os.path.relpath(
                os.path.join(output_dir, "visual_authority.json"), self.paths.root
            )
            if (
                inspected is None
                or inspected.status != "completed"
                or inspected.stage_run_id != payload.get("stage_run_id")
                or payload.get("stage_run_id") != scheduled_id
                or authority_path != expected_authority
                or inspected.authority_hash != expected_hash
                or os.path.realpath(inspected.authority_path) != absolute
            ):
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "visual authority 与固定 run/stage 输出不一致",
                )
            expected_artifacts = {
                (os.path.realpath(str(item.get("path", ""))), str(item.get("version_id", "")))
                for item in inspected.artifacts
            }
            recorded_artifacts = {
                (os.path.realpath(os.path.join(self.paths.root, str(item.get("path", "")))), str(item.get("version_id", "")))
                for item in artifacts or [] if isinstance(item, dict)
            }
            if expected_artifacts != recorded_artifacts:
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "visual authority artifact 与顶层事件不一致",
                )
            try:
                with open(inspected.authority_path, "r", encoding="utf-8") as handle:
                    authority_value = json.load(handle)
            except (OSError, TypeError, ValueError) as exc:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual authority cannot be read") from exc
            authority_operation = authority_value.get("operation") if isinstance(authority_value, dict) else None
            settled_operation = self._settled_visual_operation(events, snapshot.run_id, scheduled_id)
            if not isinstance(authority_operation, dict) or any(
                authority_operation.get(field) != getattr(settled_operation, field)
                for field in ("operation_id", "provider_job_id", "result_fingerprint")
            ):
                raise ProductionError(
                    ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    "visual authority operation 未绑定已签名结算结果",
                )

    def _settled_visual_operation(self, events: list[dict[str, Any]], run_id: str, stage_run_id: str) -> OperationRecord:
        """Return the single signed success operation that freezes visual output."""
        approval_event = next(
            (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "approval_requested"),
            None,
        )
        grant_event = next(
            (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "grant_issued"),
            None,
        )
        if approval_event is None or grant_event is None:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual completion lacks approval/grant authority")
        request = ApprovalRequest.from_dict((approval_event.get("payload") or {}).get("approval_request", {}))
        grant = Grant.from_dict((grant_event.get("payload") or {}).get("grant", {}))
        key = self.store.events.key_provider.get_key(grant.key_id) if self.store.events.key_provider else None
        if not key:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value)
        # This is a frozen-result check: grant signature and exact request bindings
        # remain mandatory, while expiry is evaluated at the signed operation event.
        grant.validate_against(request, key=key)
        if grant.stage != "visual" or grant.stage_run_id != stage_run_id:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual grant stage binding is invalid")
        latest: dict[str, OperationRecord] = {}
        for event in events:
            if event.get("run_id") != run_id or event.get("event_type") not in {
                "call_reserved", "call_submitted", "call_settled", "call_reconciled",
            }:
                continue
            candidate = (event.get("payload") or {}).get("operation")
            if isinstance(candidate, dict):
                operation = OperationRecord.from_dict(candidate)
                latest[operation.operation_id] = operation
        succeeded = [
            operation for operation in latest.values()
            if operation.status == "settled" and operation.outcome == "succeeded"
        ]
        if len(succeeded) != 1:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual completion needs one final successful operation")
        operation = succeeded[0]
        permitted = {item.get("operation_id"): item for item in grant.operation_bindings}
        binding = permitted.get(operation.operation_id)
        if (
            operation.grant_id != grant.grant_id
            or not isinstance(binding, dict)
            or operation.input_fingerprint != binding.get("input_fingerprint")
            or operation.kind != binding.get("kind")
            or operation.provider_profile != grant.provider_profile
            or operation.currency != grant.currency
        ):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual settled operation exceeds grant binding")
        return operation

    def _active_visual_grant(self, events: list[dict[str, Any]], project: dict[str, Any], run_id: str) -> Grant:
        """Validate the still-active signed grant immediately before paid effects."""
        approval_event = next(
            (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "approval_requested"),
            None,
        )
        grant_event = next(
            (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "grant_issued"),
            None,
        )
        if approval_event is None or grant_event is None:
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "visual grant authority is missing")
        request = ApprovalRequest.from_dict((approval_event.get("payload") or {}).get("approval_request", {}))
        grant = Grant.from_dict((grant_event.get("payload") or {}).get("grant", {}))
        if any(
            event.get("run_id") == run_id
            and event.get("event_type") == "grant_revoked"
            and (event.get("payload") or {}).get("grant_id") == grant.grant_id
            for event in events
        ):
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "grant has been revoked")
        key = self.store.events.key_provider.get_key(grant.key_id) if self.store.events.key_provider else None
        if not key:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value)
        grant.validate_against(request, key=key, now=utc_now())
        if grant.project_id != project["project_id"] or grant.run_id != run_id or grant.stage != "visual":
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "grant does not bind active visual run")
        return grant

    @staticmethod
    def _storyboard_expected(contract: dict[str, Any]) -> dict[str, Any]:
        settings = contract.get("storyboard_settings", {})
        return {
            "source_sha256": contract.get("storyboard_source_fingerprint"),
            "engine": settings.get("engine"),
            "model": contract.get("models", {}).get("storyboard"),
            "prompt_version": contract.get("prompt_versions", {}).get("storyboard_supervisor"),
            "tool_version": contract.get("tool_protocol_versions", {}).get("storyboard_supervisor"),
            "max_steps": settings.get("max_steps"),
            "max_calls": settings.get("max_calls"),
            "max_revisions": settings.get("max_revisions"),
        }

    def get_status(self) -> ProductionSnapshot:
        project = self.store.load_project()
        events = self.store.events.read()
        snapshot = self.store.snapshot()
        if any(event.get("event_type") == "stage_completed" and (event.get("payload") or {}).get("stage") == "visual" for event in events):
            self._configure_visual_receipt_signer(project)
        if snapshot.run_id:
            contract = self.store.validate_contract(project, snapshot.run_id)
            self.store.validate_runtime_inputs(project, contract)
            self._validate_stage_authority(project, events, snapshot, contract)
        else:
            self.store.validate_source(project)
        projection = snapshot.to_dict()
        current_projection = None
        try:
            with open(self.paths.state_file, "r", encoding="utf-8") as handle:
                current_projection = json.load(handle)
        except (OSError, ValueError, TypeError):
            pass
        if not isinstance(current_projection, dict) or (
            current_projection.get("last_event_hash") != projection["last_event_hash"]
        ):
            atomic_write_json(self.paths.state_file, projection)
        return self._publish(snapshot)

    def get_voice_script_status(self) -> dict[str, Any]:
        """Return a path-free DTO for CLI, HTTP, or desktop frontends."""
        snapshot = self.get_status()
        events = self.store.events.read()
        terminal = next(
            (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
             and event.get("event_type") in {"stage_completed", "stage_failed", "stage_needs_review"}
             and (event.get("payload") or {}).get("stage") == "voice_script"),
            None,
        )
        running = has_event(events, snapshot.run_id, "stage_scheduled", "voice_script")
        status = "running" if running else "pending"
        reason = {"code": ReasonCode.PROJECT_READY.value, "message": ""}
        artifact_dto: dict[str, Any] | None = None
        input_dto: dict[str, str] | None = None
        reused_from: dict[str, str] | None = None
        if terminal is not None:
            payload = terminal.get("payload") or {}
            if terminal.get("event_type") == "stage_completed":
                status = "reused" if payload.get("reused_from") else "completed"
                records = payload.get("produced_artifacts") or payload.get("reused_artifacts") or []
                artifacts = payload.get("artifacts") or []
                if len(records) == 1 and len(artifacts) == 1:
                    record = ArtifactRecord.from_dict(records[0])
                    path = self.store.artifact_path(str(artifacts[0].get("path", "")))
                    value = read_json(path)
                    if not isinstance(value, dict) or value.get("schema_version") != "voice-script-v1":
                        raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-script DTO source is invalid")
                    artifact_dto = {
                        "logical_id": record.ref.logical_id,
                        "version_id": record.ref.version_id,
                        "entry_count": value.get("entry_count"),
                    }
                    if len(record.depends_on) == 1:
                        input_dto = record.depends_on[0].to_dict()
                if isinstance(payload.get("reused_from"), dict):
                    reused_from = {
                        "run_id": str(payload["reused_from"].get("run_id", "")),
                        "stage_run_id": str(payload["reused_from"].get("stage_run_id", "")),
                    }
            else:
                status = "failed" if terminal.get("event_type") == "stage_failed" else "needs_review"
                reason = {
                    "code": str(payload.get("reason_code") or ReasonCode.VOICE_SCRIPT_FAILED.value),
                    "message": str(payload.get("message", "")),
                }
        return {
            "schema_version": "1",
            "run_id": snapshot.run_id,
            "stage": "voice_script",
            "status": status,
            "reason": reason,
            "artifact": artifact_dto,
            "input": input_dto,
            "reused_from": reused_from,
            "next_actions": [],
        }

    def get_voice_director_status(self) -> dict[str, Any]:
        """Return a path-free DTO for the M4.1 voice-direction stage."""
        snapshot = self.get_status()
        events = self.store.events.read()
        terminal = next(
            (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
             and event.get("event_type") in {"stage_completed", "stage_failed", "stage_needs_review"}
             and (event.get("payload") or {}).get("stage") == "voice_director"), None,
        )
        running = has_event(events, snapshot.run_id, "stage_scheduled", "voice_director")
        status = "running" if running else "pending"
        reason = {"code": ReasonCode.PROJECT_READY.value, "message": ""}
        artifact_dto: dict[str, Any] | None = None
        input_dto: list[dict[str, str]] = []
        if terminal is not None:
            payload = terminal.get("payload") or {}
            if terminal.get("event_type") == "stage_completed":
                status = "reused" if payload.get("reused_from") else "completed"
                records = payload.get("produced_artifacts") or payload.get("reused_artifacts") or []
                artifacts = payload.get("artifacts") or []
                if len(records) == 1 and len(artifacts) == 1:
                    record = ArtifactRecord.from_dict(records[0])
                    value = read_json(self.store.artifact_path(str(artifacts[0].get("path", ""))))
                    if not isinstance(value, dict) or value.get("schema_version") != "voice-direction-v1":
                        raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-direction DTO source is invalid")
                    artifact_dto = {"logical_id": record.ref.logical_id, "version_id": record.ref.version_id,
                                    "entry_count": value.get("entry_count"), "agent": value.get("agent")}
                    input_dto = [item.to_dict() for item in record.depends_on]
            else:
                status = "failed" if terminal.get("event_type") == "stage_failed" else "needs_review"
                reason = {"code": str(payload.get("reason_code") or ReasonCode.VOICE_DIRECTOR_FAILED.value),
                          "message": str(payload.get("message", ""))}
        return {"schema_version": "1", "run_id": snapshot.run_id, "stage": "voice_director",
                "status": status, "reason": reason, "artifact": artifact_dto,
                "inputs": input_dto, "next_actions": []}

    def get_voice_tts_status(self) -> dict[str, Any]:
        """Return a path-free DTO for the offline M4.2 audio stage."""
        snapshot = self.get_status()
        events = self.store.events.read()
        terminal = next(
            (event for event in reversed(events) if event.get("run_id") == snapshot.run_id
             and event.get("event_type") in {"stage_completed", "stage_failed", "stage_needs_review"}
             and (event.get("payload") or {}).get("stage") == "voice_tts"), None,
        )
        running = has_event(events, snapshot.run_id, "stage_scheduled", "voice_tts")
        status = "running" if running else "pending"
        reason = {"code": ReasonCode.PROJECT_READY.value, "message": ""}
        artifact_dto: dict[str, Any] | None = None
        inputs: list[dict[str, str]] = []
        if terminal is not None:
            payload = terminal.get("payload") or {}
            if terminal.get("event_type") == "stage_completed":
                status = "reused" if payload.get("reused_from") else "completed"
                records = payload.get("produced_artifacts") or payload.get("reused_artifacts") or []
                artifacts = payload.get("artifacts") or []
                if len(records) == 1 and len(artifacts) == 1:
                    record = ArtifactRecord.from_dict(records[0])
                    value = read_json(self.store.artifact_path(str(artifacts[0].get("path", ""))))
                    if not isinstance(value, dict) or value.get("schema_version") != "voice-audio-v1":
                        raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-audio DTO source is invalid")
                    audio_value = value.get("audio") if isinstance(value.get("audio"), dict) else {}
                    artifact_dto = {"logical_id": record.ref.logical_id, "version_id": record.ref.version_id,
                                    "entry_count": value.get("entry_count"),
                                    "audio": {"sha256": audio_value.get("sha256"), "media_type": audio_value.get("media_type")},
                                    "engine": value.get("engine")}
                    inputs = [item.to_dict() for item in record.depends_on]
            else:
                status = "failed" if terminal.get("event_type") == "stage_failed" else "needs_review"
                reason = {"code": str(payload.get("reason_code") or ReasonCode.VOICE_TTS_FAILED.value),
                          "message": str(payload.get("message", ""))}
        return {"schema_version": "1", "run_id": snapshot.run_id, "stage": "voice_tts",
                "status": status, "reason": reason, "artifact": artifact_dto,
                "inputs": inputs, "next_actions": []}

    def _record_execution_lease_acquired(self, lease: ProjectLock) -> None:
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if lease.recovered:
                self.store.events.append(
                    "execution_lease_recovered",
                    project_id=project["project_id"],
                    run_id=snapshot.run_id,
                    payload={
                        "recovered_lease_id": str(lease.recovered.get("lease_id", "legacy")),
                        "recovered_pid": lease.recovered.get("pid"),
                    },
                )
            self.store.events.append(
                "execution_lease_acquired",
                project_id=project["project_id"],
                run_id=snapshot.run_id,
                payload={"lease_id": lease.lease_id, "pid": os.getpid(), "created_at": lease.created_at},
            )

    def _record_execution_lease_released(self, lease: ProjectLock) -> None:
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            self.store.events.append(
                "execution_lease_released",
                project_id=project["project_id"],
                run_id=snapshot.run_id,
                payload={"lease_id": lease.lease_id},
            )

    def _advance_voice_director_action(
        self, *, project: dict[str, Any], snapshot: ProductionSnapshot, contract: dict[str, Any]
    ) -> ProductionSnapshot:
        """Prepare immutable inputs under the project lock, then run the child outside it."""
        run_id = snapshot.run_id
        stage_run_id = f"voice-director-{run_id.removeprefix('run_')}"
        output_dir = self.paths.voice_director_dir(run_id, stage_run_id)
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            events = self.store.events.read()
            snapshot = self.store.snapshot()
            contract = self.store.validate_contract(project, run_id)
            # This child runs through the execution-lock fast path. Recheck
            # the frozen run contract here so a changed adapter/model cannot
            # bypass the normal gate before the child is prepared.
            self._validate_runtime_contract(contract)
            if not has_event(events, run_id, "stage_scheduled", "voice_director"):
                self.store.events.append(
                    "stage_scheduled", project_id=project["project_id"], run_id=run_id,
                    payload={"stage": "voice_director", "stage_invocation_id": stage_run_id},
                )
            events = self.store.events.read()
            storyboard_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "storyboard"), None,
            )
            voice_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_script"), None,
            )
            storyboard_payload = (storyboard_event or {}).get("payload") or {}
            voice_payload = (voice_event or {}).get("payload") or {}
            storyboard_graph = storyboard_payload.get("produced_artifacts") or storyboard_payload.get("reused_artifacts") or []
            voice_graph = voice_payload.get("produced_artifacts") or voice_payload.get("reused_artifacts") or []
            storyboard_public = storyboard_payload.get("artifacts") or []
            voice_public = voice_payload.get("artifacts") or []
            policy_ref = contract.get("runtime_inputs", {}).get("voice_director.policy")
            if len(storyboard_graph) != 1 or len(voice_graph) != 1 or len(storyboard_public) != 1 or len(voice_public) != 1 or not isinstance(policy_ref, dict):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-director requires storyboard, voice-script and policy inputs")
            storyboard_ref = ArtifactRef.from_dict(storyboard_graph[0]).to_dict()
            voice_script_ref = ArtifactRef.from_dict(voice_graph[0]).to_dict()
            storyboard_path = self._stage_private_artifact(
                path=str(storyboard_public[0].get("path", "")), version_id=storyboard_ref["version_id"],
                logical_id="storyboard.output", output_dir=output_dir,
            )
            voice_script_path = self._stage_private_artifact(
                path=str(voice_public[0].get("path", "")), version_id=voice_script_ref["version_id"],
                logical_id="voice_script.main", output_dir=output_dir,
            )
            policy_path = self._stage_private_input(contract, "voice_director.policy", output_dir)
            settings = project["production"]["voice_director"]

        try:
            result = self.voice_director_adapter.execute(
                stage_run_id=stage_run_id, output_dir=output_dir,
                storyboard_path=storyboard_path, storyboard_ref=storyboard_ref,
                voice_script_path=voice_script_path, voice_script_ref=voice_script_ref,
                policy_path=policy_path, policy_ref=ArtifactRef.from_dict(policy_ref).to_dict(),
                max_model_calls=int(settings["max_model_calls"]), max_steps=int(settings["max_steps"]),
            )
        except Exception as exc:
            result = StageResult(
                status="failed", stage_run_id=stage_run_id,
                reason_code=ReasonCode.VOICE_DIRECTOR_FAILED.value,
                message=f"{type(exc).__name__}: {str(exc)[:300]}",
            )

        with ProjectLock(self.paths.lock_file):
            current_events = self.store.events.read()
            current_snapshot = self.store.snapshot()
            if current_snapshot.run_id != run_id:
                raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value)
            if not has_event(current_events, run_id, "stage_run_attached", "voice_director"):
                self.store.events.append(
                    "stage_run_attached", project_id=project["project_id"], run_id=run_id,
                    payload={"stage": "voice_director", "stage_run_id": stage_run_id},
                )
            common = {
                "stage": "voice_director", "stage_run_id": stage_run_id,
                "authority_path": self._relative_artifact_path(result.authority_path) if result.authority_path else "",
                "authority_hash": result.authority_hash,
                "authority_files": [
                    {"path": self._relative_artifact_path(item["path"]), "sha256": item["sha256"]}
                    for item in result.authority_files
                ],
                "artifacts": [
                    {**artifact, "path": self._relative_artifact_path(str(artifact.get("path", "")))}
                    for artifact in result.artifacts
                ],
            }
            if result.status == "completed" and result.artifacts:
                common["produced_artifacts"] = self._produced_artifacts(
                    stage="voice_director", run_id=run_id, contract=contract,
                    artifacts=common["artifacts"], events=self.store.events.read(),
                )
            if result.status == "completed" and (
                not result.authority_path or not result.authority_hash or not result.authority_files or not result.artifacts
            ):
                result = StageResult(status="failed", stage_run_id=stage_run_id,
                                     reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                     message="voice-director result lacks authority or artifact bindings")
            if result.status == "completed":
                self.store.events.append("stage_completed", project_id=project["project_id"], run_id=run_id, payload=common)
            else:
                self.store.events.append(
                    "stage_failed", project_id=project["project_id"], run_id=run_id,
                    payload={**common, "reason_code": result.reason_code or ReasonCode.VOICE_DIRECTOR_FAILED.value, "message": result.message},
                )
            return self._snapshot_and_project()

    def _advance_voice_tts_action(
        self, *, project: dict[str, Any], snapshot: ProductionSnapshot, contract: dict[str, Any]
    ) -> ProductionSnapshot:
        """Run the offline fake TTS child outside the project lock."""
        run_id = snapshot.run_id
        stage_run_id = f"voice-tts-{run_id.removeprefix('run_')}"
        output_dir = self.paths.voice_tts_dir(run_id, stage_run_id)
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            events = self.store.events.read()
            snapshot = self.store.snapshot()
            contract = self.store.validate_contract(project, run_id)
            self._validate_runtime_contract(contract)
            if not has_event(events, run_id, "stage_scheduled", "voice_tts"):
                self.store.events.append(
                    "stage_scheduled", project_id=project["project_id"], run_id=run_id,
                    payload={"stage": "voice_tts", "stage_invocation_id": stage_run_id},
                )
            events = self.store.events.read()
            director_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_director"), None,
            )
            director_payload = (director_event or {}).get("payload") or {}
            direction_public = director_payload.get("artifacts") or []
            direction_graph = director_payload.get("produced_artifacts") or director_payload.get("reused_artifacts") or []
            if len(direction_public) != 1 or len(direction_graph) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-tts requires voice-direction input")
            direction_ref = ArtifactRef.from_dict(direction_graph[0]).to_dict()
            direction_path = self._stage_private_artifact(
                path=str(direction_public[0].get("path", "")), version_id=direction_ref["version_id"],
                logical_id="voice_direction.main", output_dir=output_dir,
            )
        try:
            result = self.voice_tts_adapter.execute(
                stage_run_id=stage_run_id, output_dir=output_dir,
                voice_direction_path=direction_path, voice_direction_ref=direction_ref,
            )
        except Exception as exc:
            result = StageResult(
                status="failed", stage_run_id=stage_run_id,
                reason_code=ReasonCode.VOICE_TTS_FAILED.value,
                message="voice-tts stage failed",
            )
        with ProjectLock(self.paths.lock_file):
            current_events = self.store.events.read()
            current_snapshot = self.store.snapshot()
            if current_snapshot.run_id != run_id:
                raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value)
            if not has_event(current_events, run_id, "stage_run_attached", "voice_tts"):
                self.store.events.append(
                    "stage_run_attached", project_id=project["project_id"], run_id=run_id,
                    payload={"stage": "voice_tts", "stage_run_id": stage_run_id},
                )
            common = {
                "stage": "voice_tts", "stage_run_id": stage_run_id,
                "authority_path": self._relative_artifact_path(result.authority_path) if result.authority_path else "",
                "authority_hash": result.authority_hash,
                "authority_files": [
                    {"path": self._relative_artifact_path(item["path"]), "sha256": item["sha256"]}
                    for item in result.authority_files
                ],
                "artifacts": [
                    {**artifact, "path": self._relative_artifact_path(str(artifact.get("path", "")))}
                    for artifact in result.artifacts
                ],
            }
            if result.status == "completed" and result.artifacts:
                common["produced_artifacts"] = self._produced_artifacts(
                    stage="voice_tts", run_id=run_id, contract=contract,
                    artifacts=common["artifacts"], events=self.store.events.read(),
                )
            if result.status == "completed" and (
                not result.authority_path or not result.authority_hash or not result.authority_files or not result.artifacts
            ):
                result = StageResult(status="failed", stage_run_id=stage_run_id,
                                     reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                     message="voice-tts result lacks authority or artifact bindings")
            if result.status == "completed":
                self.store.events.append("stage_completed", project_id=project["project_id"], run_id=run_id, payload=common)
            else:
                self.store.events.append(
                    "stage_failed", project_id=project["project_id"], run_id=run_id,
                    payload={**common, "reason_code": result.reason_code or ReasonCode.VOICE_TTS_FAILED.value,
                             "message": "voice-tts stage failed"},
                )
            return self._snapshot_and_project()

    def _advance_voice_tts_paid_action(
        self, *, project: dict[str, Any], snapshot: ProductionSnapshot, contract: dict[str, Any]
    ) -> ProductionSnapshot:
        """Paid voice-tts: approval -> grant -> reserve -> submit -> settle -> publish.

        Provider side effects happen only after a signed grant exists.  A
        synchronous TTS transport failure is settled as failed (HTTP rejected)
        or outcome_unknown (transport broke; never blindly re-submit).
        """
        run_id = snapshot.run_id
        stage_run_id = f"voice-tts-{run_id.removeprefix('run_')}"
        output_dir = self.paths.voice_tts_dir(run_id, stage_run_id)
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            events = self.store.events.read()
            snapshot = self.store.snapshot()
            contract = self.store.validate_contract(project, run_id)
            self._validate_runtime_contract(contract)
            if not has_event(events, run_id, "stage_scheduled", "voice_tts"):
                self.store.events.append(
                    "stage_scheduled", project_id=project["project_id"], run_id=run_id,
                    payload={"stage": "voice_tts", "stage_invocation_id": stage_run_id},
                )
            if not has_event(events, run_id, "stage_run_attached", "voice_tts"):
                self.store.events.append(
                    "stage_run_attached", project_id=project["project_id"], run_id=run_id,
                    payload={"stage": "voice_tts", "stage_run_id": stage_run_id},
                )
            events = self.store.events.read()
            director_event = next(
                (event for event in reversed(events) if event.get("run_id") == run_id
                 and event.get("event_type") == "stage_completed"
                 and (event.get("payload") or {}).get("stage") == "voice_director"), None,
            )
            director_payload = (director_event or {}).get("payload") or {}
            direction_public = director_payload.get("artifacts") or []
            direction_graph = director_payload.get("produced_artifacts") or director_payload.get("reused_artifacts") or []
            if len(direction_public) != 1 or len(direction_graph) != 1:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-tts requires voice-direction input")
            direction_ref = ArtifactRef.from_dict(direction_graph[0]).to_dict()
            direction_path = self._stage_private_artifact(
                path=str(direction_public[0].get("path", "")), version_id=direction_ref["version_id"],
                logical_id="voice_direction.main", output_dir=output_dir,
            )
            key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
            approval_event = next(
                (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "approval_requested"), None,
            )
            if approval_event is None:
                request = self.voice_tts_adapter.plan(
                    project_id=project["project_id"], run_id=run_id, stage_run_id=stage_run_id,
                    output_dir=output_dir, voice_direction_path=direction_path,
                    voice_direction_ref=direction_ref,
                    settings=project["production"]["voice_tts"],
                )
                self.store.events.append("approval_requested", project_id=project["project_id"], run_id=run_id,
                                         payload={"approval_request": request.to_dict(), "key_id": key_id})
                return self._snapshot_and_project()
            grant_event = next(
                (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "grant_issued"), None,
            )
            if grant_event is None:
                return self._snapshot_and_project()
            operation_events = [event for event in events if event.get("run_id") == run_id and event.get("event_type") in {"call_reserved", "call_submitted", "call_settled", "call_reconciled"}]
            latest_operation = None
            if operation_events:
                latest_operation = OperationRecord.from_dict((operation_events[-1].get("payload") or {}).get("operation", {}))
            if latest_operation is None:
                grant = self._active_voice_tts_grant(events, project, run_id)
                binding = grant.operation_bindings[0]
                bound_request = binding.get("provider_request")
                operation = OperationRecord(
                    binding["operation_id"], grant.grant_id, binding["kind"], binding["input_fingerprint"],
                    grant.provider_profile, reservation_amount=grant.maximum_amount, currency=grant.currency,
                    provider_request=dict(bound_request) if isinstance(bound_request, dict) else None,
                )
                self.store.events.append("call_reserved", project_id=project["project_id"], run_id=run_id,
                                         payload={"operation": operation.to_dict(), "key_id": key_id})
                return self._snapshot_and_project()
            if latest_operation.status == "reserved":
                self._active_voice_tts_grant(events, project, run_id)
                try:
                    provider_job_id = self.voice_tts_adapter.submit_operation(
                        operation=latest_operation.to_dict(), stage_run_id=stage_run_id, output_dir=output_dir,
                        voice_direction_path=direction_path, voice_direction_ref=direction_ref,
                    )
                    operation = latest_operation.submit(provider_job_id)
                    self.store.events.append("call_submitted", project_id=project["project_id"], run_id=run_id,
                                             payload={"operation": operation.to_dict(), "key_id": key_id})
                except ProductionError as exc:
                    if exc.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value:
                        operation = latest_operation.submit(str(latest_operation.operation_id))
                        self.store.events.append("call_submitted", project_id=project["project_id"], run_id=run_id,
                                                 payload={"operation": operation.to_dict(), "key_id": key_id})
                        unknown = operation.settle(
                            outcome="outcome_unknown", result_fingerprint="",
                            usage={"cost_status": "unknown", "cost_source": "unknown"},
                        )
                        self.store.events.append("call_settled", project_id=project["project_id"], run_id=run_id,
                                                 payload={"operation": unknown.to_dict(), "key_id": key_id})
                        return self._snapshot_and_project()
                    failed = latest_operation.submit(str(latest_operation.operation_id))
                    self.store.events.append("call_submitted", project_id=project["project_id"], run_id=run_id,
                                             payload={"operation": failed.to_dict(), "key_id": key_id})
                    settled = failed.settle(
                        outcome="failed", result_fingerprint="",
                        usage={"actual_amount": "0", "currency": TTS_CURRENCY,
                               "cost_status": "final", "cost_source": "provider_response"},
                    )
                    self.store.events.append("call_settled", project_id=project["project_id"], run_id=run_id,
                                             payload={"operation": settled.to_dict(), "key_id": key_id})
                    return self._snapshot_and_project()
                return self._snapshot_and_project()
            if latest_operation.status == "submitted":
                self._active_voice_tts_grant(events, project, run_id)
                try:
                    observed = self.voice_tts_adapter.observe_operation(
                        stage_run_id=stage_run_id, output_dir=output_dir,
                        operation=latest_operation.to_dict(),
                        voice_direction_path=direction_path, voice_direction_ref=direction_ref,
                    )
                except ProductionError as exc:
                    if exc.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value:
                        operation = latest_operation.settle(
                            outcome="outcome_unknown", result_fingerprint="",
                            usage={"cost_status": "unknown", "cost_source": "unknown"},
                        )
                        self.store.events.append("call_settled", project_id=project["project_id"], run_id=run_id,
                                                 payload={"operation": operation.to_dict(), "key_id": key_id})
                        return self._snapshot_and_project()
                    raise
                operation = latest_operation.settle(
                    outcome=observed.outcome,
                    result_fingerprint=observed.result_fingerprint,
                    usage=observed.settled_usage,
                )
                self.store.events.append("call_settled", project_id=project["project_id"], run_id=run_id,
                                         payload={"operation": operation.to_dict(), "key_id": key_id})
                return self._snapshot_and_project()
            if latest_operation.status == "settled" and latest_operation.outcome == "succeeded":
                result = self.voice_tts_adapter.publish_result(
                    stage_run_id=stage_run_id, output_dir=output_dir,
                    operation=latest_operation.to_dict(),
                    voice_direction_path=direction_path, voice_direction_ref=direction_ref,
                )
                if result.status != "completed":
                    self.store.events.append(
                        "stage_failed", project_id=project["project_id"], run_id=run_id,
                        payload={"stage": "voice_tts", "stage_run_id": stage_run_id,
                                 "reason_code": result.reason_code or ReasonCode.VOICE_TTS_FAILED.value,
                                 "message": "voice-tts paid publish failed"},
                    )
                    return self._snapshot_and_project()
                published_artifacts = [{**item, "path": self._relative_artifact_path(str(item.get("path", "")))} for item in result.artifacts]
                self.store.events.append("stage_completed", project_id=project["project_id"], run_id=run_id,
                                         payload={"stage": "voice_tts", "stage_run_id": stage_run_id,
                                                  "authority_path": self._relative_artifact_path(result.authority_path), "authority_hash": result.authority_hash,
                                                  "authority_files": [{"path": self._relative_artifact_path(item["path"]), "sha256": item["sha256"]} for item in result.authority_files],
                                                  "artifacts": published_artifacts,
                                                  "produced_artifacts": self._produced_artifacts(
                                                      stage="voice_tts", run_id=run_id, contract=contract,
                                                      artifacts=published_artifacts, events=events,
                                                  )})
                return self._snapshot_and_project()
            if latest_operation.status == "settled" and latest_operation.outcome == "failed":
                self.store.events.append("stage_failed", project_id=project["project_id"], run_id=run_id,
                                         payload={"stage": "voice_tts", "stage_run_id": stage_run_id,
                                                  "reason_code": ReasonCode.VOICE_TTS_FAILED.value,
                                                  "message": "voice-tts paid provider reported failure"})
                return self._snapshot_and_project()
            return self._snapshot_and_project()

    def _active_voice_tts_grant(self, events: list[dict[str, Any]], project: dict[str, Any], run_id: str) -> Grant:
        """Validate the still-active signed grant immediately before paid effects."""
        approval_event = next(
            (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "approval_requested"),
            None,
        )
        grant_event = next(
            (event for event in events if event.get("run_id") == run_id and event.get("event_type") == "grant_issued"),
            None,
        )
        if approval_event is None or grant_event is None:
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "voice-tts grant authority is missing")
        request = ApprovalRequest.from_dict((approval_event.get("payload") or {}).get("approval_request", {}))
        grant = Grant.from_dict((grant_event.get("payload") or {}).get("grant", {}))
        if any(
            event.get("run_id") == run_id
            and event.get("event_type") == "grant_revoked"
            and (event.get("payload") or {}).get("grant_id") == grant.grant_id
            for event in events
        ):
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "grant has been revoked")
        key = self.store.events.key_provider.get_key(grant.key_id) if self.store.events.key_provider else None
        if not key:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value)
        grant.validate_against(request, key=key, now=utc_now())
        if grant.project_id != project["project_id"] or grant.run_id != run_id or grant.stage != "voice_tts":
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "grant does not bind active voice-tts run")
        return grant

    def advance(self) -> ProductionSnapshot:
        with ProjectLock(
            self.paths.execution_lock_file,
            on_acquired=self._record_execution_lease_acquired,
            on_released=self._record_execution_lease_released,
        ):
            probe_project = self.store.load_project()
            probe_events = self.store.events.read()
            probe_snapshot = self.store.snapshot()
            if probe_snapshot.run_id:
                probe_contract = self.store.validate_contract(probe_project, probe_snapshot.run_id)
                probe_action = self.scheduler.next_action(probe_snapshot, probe_events)
                if probe_action == "advance_voice_director" and self._stage_execution_action(probe_contract, "voice_director") != "reuse":
                    return self._advance_voice_director_action(
                        project=probe_project, snapshot=probe_snapshot, contract=probe_contract,
                    )
                if probe_action == "advance_voice_tts" and self._stage_execution_action(probe_contract, "voice_tts") != "reuse":
                    voice_tts_mode = probe_project.get("production", {}).get("voice_tts", {}).get("mode", "offline_mock")
                    if voice_tts_mode == "paid_siliconflow":
                        return self._advance_voice_tts_paid_action(
                            project=probe_project, snapshot=probe_snapshot, contract=probe_contract,
                        )
                    return self._advance_voice_tts_action(
                        project=probe_project, snapshot=probe_snapshot, contract=probe_contract,
                    )
            with ProjectLock(self.paths.lock_file):
                project = self.store.load_project()
                events = self.store.events.read()
                snapshot = self.store.snapshot()
                contract = None
                if snapshot.run_id:
                    contract = self.store.validate_contract(project, snapshot.run_id)
                    source_path = self.store.validate_runtime_inputs(project, contract)["source.script"]
                    self._validate_stage_authority(project, events, snapshot, contract)
                    if snapshot.status not in {
                        ProductionStatus.COMPLETED.value,
                        ProductionStatus.NEEDS_REVIEW.value,
                        ProductionStatus.FAILED.value,
                    }:
                        self._validate_runtime_contract(contract)
                else:
                    source_path = self.store.validate_source(project)
                    self._ensure_project_source_artifact(project)
                    events = self.store.events.read()
                    snapshot = self.store.snapshot()
                action = self.scheduler.next_action(snapshot, events)

                if action == "stop":
                    return self._publish(snapshot)
                if action == "create_run":
                    run_id = f"run_{uuid.uuid4().hex}"
                    os.makedirs(self.paths.run_dir(run_id), exist_ok=True)
                    contract = self._create_contract(project, run_id)
                    atomic_write_json(self.paths.contract_file(run_id), contract)
                    self.store.events.append(
                        "run_created", project_id=project["project_id"], run_id=run_id,
                        payload={"contract_fingerprint": contract["contract_fingerprint"], "dag_version": contract["dag_version"],
                                 "stage_sequence": contract["stage_sequence"]},
                    )
                    self.store.events.append(
                        "run_started", project_id=project["project_id"], run_id=run_id, payload={},
                    )
                    return self._snapshot_and_project()
                if action == "start_run":
                    self.store.events.append(
                        "run_started", project_id=project["project_id"],
                        run_id=snapshot.run_id, payload={},
                    )
                    return self._snapshot_and_project()
                if action == "resume_run":
                    self.store.events.append(
                        "run_resumed", project_id=project["project_id"],
                        run_id=snapshot.run_id, payload={},
                    )
                    return self._snapshot_and_project()
                if action == "complete_run":
                    self.store.events.append(
                        "run_completed", project_id=project["project_id"],
                        run_id=snapshot.run_id, payload={},
                    )
                    return self._snapshot_and_project()
                if action in {"advance_storyboard", "advance_voice_script", "advance_voice_director", "advance_voice_tts", "advance_visual"} and contract is not None:
                    stage = action.removeprefix("advance_")
                    if self._stage_execution_action(contract, stage) == "reuse":
                        return self._reuse_predecessor_stage(project=project, snapshot=snapshot, contract=contract,
                                                             events=events, stage=stage)
                if action == "advance_voice_script" and contract is not None:
                    run_id = snapshot.run_id
                    stage_run_id = f"voice-script-{run_id.removeprefix('run_')}"
                    output_dir = self.paths.voice_script_dir(run_id, stage_run_id)
                    if not has_event(events, run_id, "stage_scheduled", "voice_script"):
                        self.store.events.append(
                            "stage_scheduled", project_id=project["project_id"], run_id=run_id,
                            payload={"stage": "voice_script", "stage_invocation_id": stage_run_id},
                        )
                    events = self.store.events.read()
                    storyboard_event = next(
                        (event for event in reversed(events) if event.get("run_id") == run_id
                         and event.get("event_type") == "stage_completed"
                         and (event.get("payload") or {}).get("stage") == "storyboard"),
                        None,
                    )
                    storyboard_payload = (storyboard_event or {}).get("payload") or {}
                    storyboard_graph = storyboard_payload.get("produced_artifacts") or storyboard_payload.get("reused_artifacts") or []
                    storyboard_public = storyboard_payload.get("artifacts") or []
                    if len(storyboard_graph) != 1 or len(storyboard_public) != 1:
                        raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "voice-script requires one storyboard artifact")
                    storyboard_ref = ArtifactRef.from_dict(storyboard_graph[0]).to_dict()
                    public = storyboard_public[0]
                    storyboard_path = self._stage_private_artifact(
                        path=str(public.get("path", "")), version_id=storyboard_ref["version_id"],
                        logical_id="storyboard.output", output_dir=output_dir,
                    )
                    try:
                        result = self.voice_script_adapter.execute(
                            stage_run_id=stage_run_id, storyboard_path=storyboard_path,
                            storyboard_ref=storyboard_ref, output_dir=output_dir,
                        )
                    except Exception as exc:
                        result = StageResult(
                            status="failed", stage_run_id=stage_run_id,
                            reason_code=ReasonCode.VOICE_SCRIPT_FAILED.value,
                            message=f"{type(exc).__name__}: {str(exc)[:300]}",
                        )
                    if not has_event(events, run_id, "stage_run_attached", "voice_script"):
                        self.store.events.append(
                            "stage_run_attached", project_id=project["project_id"], run_id=run_id,
                            payload={"stage": "voice_script", "stage_run_id": stage_run_id},
                        )
                    common = {
                        "stage": "voice_script",
                        "stage_run_id": stage_run_id,
                        "authority_path": self._relative_artifact_path(result.authority_path) if result.authority_path else "",
                        "authority_hash": result.authority_hash,
                        "authority_files": [
                            {"path": self._relative_artifact_path(item["path"]), "sha256": item["sha256"]}
                            for item in result.authority_files
                        ],
                        "artifacts": [
                            {**artifact, "path": self._relative_artifact_path(str(artifact.get("path", "")))}
                            for artifact in result.artifacts
                        ],
                    }
                    if result.status == "completed" and result.artifacts:
                        common["produced_artifacts"] = self._produced_artifacts(
                            stage="voice_script", run_id=run_id, contract=contract,
                            artifacts=common["artifacts"], events=self.store.events.read(),
                        )
                    if result.status == "completed" and (
                        not result.authority_path or not result.authority_hash
                        or not result.authority_files or not result.artifacts
                    ):
                        result = StageResult(
                            status="failed", stage_run_id=stage_run_id,
                            reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                            message="voice-script result lacks authority or artifact bindings",
                        )
                    if result.status == "completed":
                        self.store.events.append(
                            "stage_completed", project_id=project["project_id"], run_id=run_id, payload=common,
                        )
                    else:
                        self.store.events.append(
                            "stage_failed", project_id=project["project_id"], run_id=run_id,
                            payload={
                                **common,
                                "reason_code": result.reason_code or ReasonCode.VOICE_SCRIPT_FAILED.value,
                                "message": result.message,
                            },
                        )
                    return self._snapshot_and_project()
                if action == "advance_visual" and contract is not None:
                    self._configure_visual_receipt_signer(project)
                    run_id = snapshot.run_id
                    stage_run_id = f"visual-{run_id.removeprefix('run_')}"
                    output_dir = self.paths.visual_dir(run_id, stage_run_id)
                    if not has_event(events, run_id, "stage_scheduled", "visual"):
                        self.store.events.append(
                            "stage_scheduled", project_id=project["project_id"], run_id=run_id,
                            payload={"stage": "visual", "stage_invocation_id": stage_run_id},
                        )
                    if not has_event(events, run_id, "stage_run_attached", "visual"):
                        self.store.events.append(
                            "stage_run_attached", project_id=project["project_id"], run_id=run_id,
                            payload={"stage": "visual", "stage_run_id": stage_run_id},
                        )
                    events = self.store.events.read()
                    approval_event = next((event for event in events if event.get("run_id") == run_id and event.get("event_type") == "approval_requested"), None)
                    if approval_event is None:
                        storyboard_event = next((event for event in reversed(events) if event.get("run_id") == run_id and event.get("event_type") == "stage_completed" and (event.get("payload") or {}).get("stage") == "storyboard"), None)
                        artifacts = ((storyboard_event or {}).get("payload") or {}).get("artifacts") or []
                        if not artifacts:
                            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual 缺少 storyboard 公开产物")
                        first_artifact = dict(artifacts[0])
                        first_artifact["artifact_id"] = "storyboard"
                        content_bound = isinstance(contract.get("runtime_inputs"), dict)
                        storyboard_path = self._stage_private_artifact(
                            path=str(first_artifact["path"]), version_id=str(first_artifact["version_id"]),
                            logical_id="storyboard.output", output_dir=output_dir,
                        ) if content_bound else ""
                        style_path = ""
                        if content_bound and isinstance(contract.get("runtime_inputs", {}).get("style.reference"), dict):
                            style_path = self._stage_private_input(contract, "style.reference", output_dir)
                        request = self.visual_adapter.plan(
                            project_id=project["project_id"], run_id=run_id, stage_run_id=stage_run_id,
                            output_dir=output_dir, storyboard_artifact={"artifact_id": "storyboard", "version_id": str(first_artifact["version_id"])},
                            settings=project["production"]["visual"],
                            artifact_versions=tuple({"artifact_id": item["logical_id"], "version_id": item["version_id"]}
                                                    for item in contract.get("successor_selection", [])),
                            storyboard_path=storyboard_path, style_path=style_path,
                        )
                        key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
                        self.store.events.append("approval_requested", project_id=project["project_id"], run_id=run_id,
                                                 payload={"approval_request": request.to_dict(), "key_id": key_id})
                        return self._snapshot_and_project()
                    grant_event = next((event for event in events if event.get("run_id") == run_id and event.get("event_type") == "grant_issued"), None)
                    if grant_event is None:
                        return self._snapshot_and_project()
                    operation_events = [event for event in events if event.get("run_id") == run_id and event.get("event_type") in {"call_reserved", "call_submitted", "call_settled", "call_reconciled"}]
                    latest_operation = None
                    if operation_events:
                        latest_operation = OperationRecord.from_dict((operation_events[-1].get("payload") or {}).get("operation", {}))
                    key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
                    if latest_operation is None:
                        grant = self._active_visual_grant(events, project, run_id)
                        binding = grant.operation_bindings[0]
                        # Reserve the complete signed per-operation allowance before a
                        # provider side effect.  Final cost is checked by the reducer.
                        operation = OperationRecord(binding["operation_id"], grant.grant_id, binding["kind"], binding["input_fingerprint"], grant.provider_profile, reservation_amount=grant.maximum_amount, currency=grant.currency, provider_request=binding.get("provider_request"))
                        self.store.events.append("call_reserved", project_id=project["project_id"], run_id=run_id,
                                                 payload={"operation": operation.to_dict(), "key_id": key_id})
                        return self._snapshot_and_project()
                    if latest_operation.status == "reserved":
                        self._active_visual_grant(events, project, run_id)
                        operation = latest_operation.submit(self.visual_adapter.submit_operation(latest_operation.to_dict()))
                        self.store.events.append("call_submitted", project_id=project["project_id"], run_id=run_id,
                                                 payload={"operation": operation.to_dict(), "key_id": key_id})
                        return self._snapshot_and_project()
                    if latest_operation.status == "submitted":
                        self._active_visual_grant(events, project, run_id)
                        observed = self.visual_adapter.observe_operation(
                            stage_run_id=stage_run_id, output_dir=output_dir, operation=latest_operation.to_dict(),
                        )
                        operation = latest_operation.settle(
                            outcome=observed.outcome,
                            result_fingerprint=observed.result_fingerprint,
                            usage=observed.settled_usage,
                        )
                        self.store.events.append("call_settled", project_id=project["project_id"], run_id=run_id,
                                                 payload={"operation": operation.to_dict(), "key_id": key_id})
                        return self._snapshot_and_project()
                    if latest_operation.status == "settled" and latest_operation.outcome == "succeeded":
                        # Completion consumes the already-settled, event-time-validated
                        # result.  It has no provider side effect, so expiry after settlement
                        # does not invalidate recovery of that frozen outcome.
                        result = self.visual_adapter.publish_result(stage_run_id=stage_run_id, output_dir=output_dir, operation=latest_operation.to_dict())
                        published_artifacts = [{**item, "path": self._relative_artifact_path(str(item["path"]))} for item in result.artifacts]
                        self.store.events.append("stage_completed", project_id=project["project_id"], run_id=run_id,
                                                  payload={"stage": "visual", "stage_run_id": stage_run_id,
                                                           "authority_path": self._relative_artifact_path(result.authority_path), "authority_hash": result.authority_hash,
                                                           "authority_files": [{"path": self._relative_artifact_path(item["path"]), "sha256": item["sha256"]} for item in result.authority_files],
                                                           "artifacts": published_artifacts,
                                                           "produced_artifacts": self._produced_artifacts(
                                                               stage="visual", run_id=run_id, contract=contract,
                                                               artifacts=published_artifacts, events=events,
                                                           )})
                        return self._snapshot_and_project()
                    if latest_operation.status == "settled" and latest_operation.outcome == "failed":
                        self.store.events.append("stage_failed", project_id=project["project_id"], run_id=run_id,
                                                 payload={"stage": "visual", "stage_run_id": stage_run_id,
                                                          "reason_code": ReasonCode.VISUAL_FAILED.value,
                                                          "message": "mock visual provider reported failure"})
                        return self._snapshot_and_project()
                    return self._snapshot_and_project()
                if action != "advance_storyboard" or contract is None:
                    raise ProductionError(ReasonCode.INTERNAL_ERROR.value, f"未知调度动作: {action}")

                run_id = snapshot.run_id
                invocation_id = f"storyboard-{run_id.removeprefix('run_')}"
                output_dir = self.paths.storyboard_dir(run_id, invocation_id)
                if not has_event(events, run_id, "stage_scheduled", "storyboard"):
                    self.store.events.append(
                        "stage_scheduled", project_id=project["project_id"], run_id=run_id,
                        payload={"stage": "storyboard", "stage_invocation_id": invocation_id},
                    )
                settings = project["production"]["storyboard"]
                expected = self._storyboard_expected(contract)
                source_path = self._stage_private_input(
                    contract, "source.script", output_dir, fallback_path=source_path,
                )

            try:
                result = self.storyboard_adapter.execute(
                    stage_run_id=invocation_id,
                    source_path=source_path,
                    output_dir=output_dir,
                    settings=settings,
                    expected=expected,
                )
                failure_message = ""
            except Exception as exc:
                result = None
                failure_message = f"{type(exc).__name__}: {str(exc)[:300]}"

            with ProjectLock(self.paths.lock_file):
                current_events = self.store.events.read()
                current_snapshot = self.store.snapshot()
                if current_snapshot.run_id != run_id:
                    raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value)
                child_run_id = result.stage_run_id if result is not None else invocation_id
                if not has_event(current_events, run_id, "stage_run_attached", "storyboard"):
                    self.store.events.append(
                        "stage_run_attached", project_id=project["project_id"], run_id=run_id,
                        payload={"stage": "storyboard", "stage_run_id": child_run_id},
                    )
                if result is None:
                    self.store.events.append(
                        "stage_failed", project_id=project["project_id"], run_id=run_id,
                        payload={
                            "stage": "storyboard", "stage_run_id": child_run_id,
                            "reason_code": ReasonCode.STORYBOARD_FAILED.value,
                            "message": failure_message,
                        },
                    )
                else:
                    common = {
                        "stage": "storyboard",
                        "stage_run_id": child_run_id,
                        "authority_path": self._relative_artifact_path(result.authority_path)
                        if result.authority_path else "",
                        "authority_hash": result.authority_hash,
                        "authority_files": [
                            {"path": self._relative_artifact_path(item["path"]), "sha256": item["sha256"]}
                            for item in result.authority_files
                        ],
                        "artifacts": [
                            {**artifact, "path": self._relative_artifact_path(str(artifact.get("path", "")))}
                            for artifact in result.artifacts
                        ],
                    }
                    if result.status in {"completed", "needs_review"} and result.artifacts:
                        common["produced_artifacts"] = self._produced_artifacts(
                            stage="storyboard", run_id=run_id, contract=contract,
                            artifacts=common["artifacts"], events=current_events,
                        )
                    if result.status in {"completed", "needs_review"} and (
                        not result.authority_path or not result.authority_hash
                        or not result.authority_files or not result.artifacts
                    ):
                        self.store.events.append(
                            "stage_failed", project_id=project["project_id"], run_id=run_id,
                            payload={
                                **common, "reason_code": ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                "message": "分镜完成结果缺少权威账本或产物引用",
                            },
                        )
                    elif result.status == "completed":
                        self.store.events.append(
                            "stage_completed", project_id=project["project_id"], run_id=run_id,
                            payload=common,
                        )
                    elif result.status == "needs_review":
                        self.store.events.append(
                            "stage_needs_review", project_id=project["project_id"], run_id=run_id,
                            payload={
                                **common,
                                "reason_code": result.reason_code or ReasonCode.STORYBOARD_REVIEW_REQUIRED.value,
                                "message": result.message, "next_actions": [],
                            },
                        )
                    else:
                        self.store.events.append(
                            "stage_failed", project_id=project["project_id"], run_id=run_id,
                            payload={
                                **common,
                                "reason_code": result.reason_code or ReasonCode.STORYBOARD_FAILED.value,
                                "message": result.message,
                            },
                        )
                post_result = self.store.snapshot()
                if (
                    post_result.status == ProductionStatus.PAUSED.value
                    and not has_event(self.store.events.read(), run_id, "run_paused")
                ):
                    self.store.events.append(
                        "run_paused", project_id=project["project_id"], run_id=run_id, payload={},
                    )
                return self._snapshot_and_project()

    def run_until_blocked(self, *, max_advances: int = 20) -> ProductionSnapshot:
        snapshot = self.get_status()
        for _ in range(max_advances):
            if snapshot.status in {
                ProductionStatus.COMPLETED.value,
                ProductionStatus.AWAITING_APPROVAL.value,
                ProductionStatus.BLOCKED.value,
                ProductionStatus.NEEDS_REVIEW.value,
                ProductionStatus.FAILED.value,
                ProductionStatus.CANCELLED.value,
                ProductionStatus.SUPERSEDED.value,
            }:
                return snapshot
            snapshot = self.advance()
        raise ProductionError(ReasonCode.INTERNAL_ERROR.value, "超过单次运行推进上限")

    def request_pause(self) -> ProductionSnapshot:
        with ProjectLock(self.paths.lock_file):
            project = self.store.load_project()
            snapshot = self.store.snapshot()
            if snapshot.status in {
                ProductionStatus.COMPLETED.value,
                ProductionStatus.NEEDS_REVIEW.value,
                ProductionStatus.FAILED.value,
                ProductionStatus.PAUSED.value,
            }:
                return self._publish(snapshot)
            if not snapshot.run_id:
                raise ProductionError(ReasonCode.DEPENDENCY_UNSATISFIED.value, "项目尚无活动 run")
            self.store.events.append(
                "pause_requested",
                project_id=project["project_id"],
                run_id=snapshot.run_id,
                payload={},
            )
            if not os.path.exists(self.paths.execution_lock_file):
                self.store.events.append(
                    "run_paused",
                    project_id=project["project_id"],
                    run_id=snapshot.run_id,
                    payload={},
                )
            return self._snapshot_and_project()

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        run_status = ""
        try:
            project = self.store.load_project()
            checks.append({"name": "project_schema", "status": "passed"})
            self.store.validate_source(project)
            checks.append({"name": "source_integrity", "status": "passed"})
            events = self.store.events.read()
            checks.append({"name": "event_chain", "status": "passed", "event_count": len(events)})
            snapshot = self.store.snapshot()
            run_status = snapshot.status
            if snapshot.run_id:
                contract = self.store.validate_contract(project, snapshot.run_id)
                checks.append({"name": "run_contract", "status": "passed"})
                self.store.validate_runtime_inputs(project, contract)
                checks.append({"name": "runtime_inputs", "status": "passed"})
                self._validate_stage_authority(project, events, snapshot, contract)
                stage_run_id = f"storyboard-{snapshot.run_id.removeprefix('run_')}"
                output_dir = self.paths.storyboard_dir(snapshot.run_id, stage_run_id)
                inspected = self.storyboard_adapter.inspect(
                    stage_run_id=stage_run_id,
                    output_dir=output_dir,
                    expected=self._storyboard_expected(contract),
                )
                if inspected is not None:
                    checks.append({"name": "storyboard_stage", "status": "passed", "stage_status": inspected.status})
            return {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "project_id": project["project_id"],
                "status": "passed",
                "integrity_status": "passed",
                "run_status": run_status,
                "checks": checks,
            }
        except ProductionError as exc:
            checks.append({"name": "integrity", "status": "failed", "code": exc.code, "message": exc.message})
            return {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "project_id": "",
                "status": "failed",
                "integrity_status": "failed",
                "run_status": run_status,
                "checks": checks,
            }
