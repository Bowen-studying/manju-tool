"""Provider-neutral paid video generation stage adapter for M5.1.

Mirrors the voice-tts paid chain (approval -> grant -> reserve -> submit ->
observe -> settle -> publish) but treats the provider as asynchronous: submit
returns a video_id that is polled by observe_operation until the provider
completes, then the mp4 is downloaded and committed with a signed receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import Any

from manju.production.adapters.base import StageResult
from manju.production.artifacts import ArtifactRef
from manju.production.models import (
    ProductionError,
    ReasonCode,
    canonical_json,
)
from manju.production.store import sha256_file
from manju.production.video_providers import (
    VIDEO_DEFAULT_FRAME_RATE,
    VIDEO_DEFAULT_FRAMES,
    VIDEO_MAX_SINGLE_CALL_AMOUNT_MINOR,
    VIDEO_MAX_TOTAL_AMOUNT_MINOR,
    VIDEO_MEDIA_TYPE,
    ProviderObservation,
    _estimate_video_amount_minor,
)
from manju.utils.runtime import atomic_write_bytes, atomic_write_json, read_json


VIDEO_SCHEMA_VERSION = "video-run-v1"
VIDEO_CONTRACT_VERSION = "video-adapter-m5.1-v1"
ARTIFACT_NAME = "video.mp4"
RECEIPT_NAME = "video_receipt.json"
AUTHORITY_NAME = "video_authority.json"
PLAN_NAME = "video_plan.json"
PENDING_PREFIX = "video_pending_"

_ALLOWED_REQUEST = {"model", "prompt", "num_frames", "frame_rate", "width", "height", "image", "response_format"}


def _safe_output(output_dir: str, name: str) -> str:
    """Resolve one fixed stage file without following links or escaping."""
    if not isinstance(name, str) or not name or os.path.basename(name) != name:
        raise ValueError("video file name is invalid")
    root = os.path.abspath(output_dir)
    current = root
    while current and current != os.path.dirname(current):
        if os.path.lexists(current):
            value = os.lstat(current)
            if stat.S_ISLNK(value.st_mode) or bool(
                getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("video output directory cannot contain a link or reparse point")
        current = os.path.dirname(current)
    candidate = os.path.abspath(os.path.join(root, name))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained or os.path.dirname(candidate) != root:
        raise ValueError("video path escaped stage directory")
    if os.path.lexists(candidate):
        value = os.lstat(candidate)
        if stat.S_ISLNK(value.st_mode) or bool(
            getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("video output cannot be a link or reparse point")
    return candidate


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _combine_prompts(shots: list[dict[str, Any]], *, max_chars: int = 6000) -> str:
    """Merge shot prompts into one continuous single-take sequence description.

    Mirrors the combined-shot convention: each shot gets a time-slice label,
    and the sequence reads as one camera move with no visible cuts.
    """
    lines: list[str] = []
    elapsed = 0.0
    for index, shot in enumerate(shots):
        duration = float(shot.get("duration_seconds") or shot.get("duration") or 3.0)
        prompt = _text(shot.get("prompt") or shot.get("deterministic_prompt") or shot.get("video_prompt_cn"))
        if not prompt:
            continue
        if index == 0:
            prefix = f"[0-{duration:.1f}s] SHOT/{shot.get('shot_id', '')}: {prompt}"
        else:
            prefix = f"[{elapsed:.1f}-{elapsed + duration:.1f}s] SHOT/{shot.get('shot_id', '')}: {prompt}"
        lines.append(prefix)
        elapsed += duration
    if not lines:
        raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video prompt main has no prompts")
    body = " ".join(lines)
    tail = " Seamless continuous single-take sequence. One camera movement. No visible cuts. Cinematic."
    if len(body) + len(tail) > max_chars:
        body = body[: max_chars - len(tail)]
    return body + tail


def _public_provider_request(settings: dict[str, Any]) -> dict[str, Any]:
    """Only public generation controls enter the approval contract."""
    allowed = {"model", "num_frames", "frame_rate", "width", "height", "response_format"}
    request: dict[str, Any] = {}
    raw = settings.get("provider_request") if isinstance(settings.get("provider_request"), dict) else {}
    for key in allowed:
        if key in raw:
            request[key] = raw[key]
    return request


class VideoStageAdapter:
    """Paid video adapter backed by an asynchronous provider."""

    contract_version = VIDEO_CONTRACT_VERSION
    artifact_name = ARTIFACT_NAME
    receipt_name = RECEIPT_NAME

    def __init__(self, provider, *, provider_profile: str = "async-video", provider_request: dict[str, Any] | None = None):
        self.provider = provider
        self.provider_profile = provider_profile
        self.provider_request = dict(provider_request or {})

    def _load_video_prompt(self, *, video_prompt_path: str, video_prompt_ref: dict[str, str]) -> list[dict[str, Any]]:
        artifact_path = os.path.realpath(video_prompt_path)
        if not os.path.isfile(artifact_path):
            raise ProductionError(ReasonCode.DEPENDENCY_UNSATISFIED.value, "video prompt artifact is unavailable")
        value = read_json(artifact_path)
        if not isinstance(value, dict) or not isinstance(value.get("shots"), list):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "video prompt artifact is invalid")
        expected = video_prompt_ref.get("version_id", "").removeprefix("sha256:")
        if expected and sha256_file(artifact_path) != expected:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "video prompt input hash drifted")
        return value["shots"]

    def plan(
        self, *, project_id: str, run_id: str, stage_run_id: str, output_dir: str,
        video_prompt_artifact: dict[str, str], settings: dict[str, Any],
        artifact_versions: tuple[dict[str, str], ...] = (),
        video_prompt_path: str = "",
        image_material: str = "",
    ) -> Any:
        os.makedirs(output_dir, exist_ok=True)
        shots = self._load_video_prompt(video_prompt_path=video_prompt_path, video_prompt_ref=video_prompt_artifact)
        combined = _combine_prompts(shots)
        from manju.production.approvals import ApprovalRequest
        operation_id = "video-" + run_id.removeprefix("run_")
        provider_request = _public_provider_request(settings)
        provider_request["prompt"] = combined
        provider_request["model"] = str(provider_request.get("model") or getattr(self.provider, "model", "video-v2.0"))
        provider_request["num_frames"] = int(provider_request.get("num_frames") or getattr(self.provider, "num_frames", VIDEO_DEFAULT_FRAMES))
        provider_request["frame_rate"] = int(provider_request.get("frame_rate") or getattr(self.provider, "frame_rate", VIDEO_DEFAULT_FRAME_RATE))
        if image_material:
            provider_request["image"] = image_material
        encoded = json.dumps(provider_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request_fingerprint = "sha256:" + hashlib.sha256(encoded).hexdigest()
        approved_artifacts = ({"artifact_id": "video_prompt.main", "version_id": video_prompt_artifact.get("version_id", "")},)
        input_material = json.dumps({
            "operation_id": operation_id, "artifacts": approved_artifacts,
            "provider_request_fingerprint": request_fingerprint,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request = ApprovalRequest(
            request_id="video-approval-" + run_id.removeprefix("run_"), project_id=project_id, run_id=run_id,
            stage="video", stage_run_id=stage_run_id, kind="paid_video_batch",
            state_fingerprint="sha256:" + hashlib.sha256(json.dumps(approved_artifacts, sort_keys=True).encode("utf-8")).hexdigest(),
            artifact_versions=approved_artifacts,
            operation_intents=({
                "operation_id": operation_id,
                "input_fingerprint": "sha256:" + hashlib.sha256(input_material.encode("utf-8")).hexdigest(),
                "kind": "video_generation",
                "provider_request": provider_request,
                "provider_request_fingerprint": request_fingerprint,
            },),
            maximum_paid_calls=int(settings.get("maximum_paid_calls", 1)),
            maximum_amount=str(settings.get("maximum_amount", VIDEO_MAX_TOTAL_AMOUNT_MINOR)),
            currency=str(settings.get("currency", "CNY")),
            provider_profile=str(settings.get("provider_profile", self.provider_profile)),
            expires_at="2099-01-01T00:00:00Z",
            settlement_mode=str(settings.get("settlement_mode", "provider_evidence")),
        )
        atomic_write_json(_safe_output(output_dir, PLAN_NAME), {"published_approval": request.to_dict(), "adapter_contract_version": self.contract_version})
        return request

    def submit_operation(
        self, *, operation: dict[str, Any], stage_run_id: str, output_dir: str,
        video_prompt_path: str, video_prompt_ref: dict[str, str],
    ) -> str:
        bound_request = operation.get("provider_request")
        if not isinstance(bound_request, dict) or not bound_request.get("prompt"):
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "video grant binding lacks a prompt")
        current = _public_provider_request({"provider_request": self.provider_request})
        current["prompt"] = bound_request.get("prompt")
        if bound_request.get("image"):
            current["image"] = bound_request.get("image")
        if current != bound_request:
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "video provider controls drift from the signed grant")
        self._load_video_prompt(video_prompt_path=video_prompt_path, video_prompt_ref=video_prompt_ref)
        provider = self.provider
        return provider.submit(str(operation["operation_id"]), idempotency_key=str(operation["operation_id"]), request=bound_request)

    def observe_operation(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any]) -> ProviderObservation:
        provider_job_id = str(operation.get("provider_job_id", ""))
        if not provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "video operation lacks a provider job id")
        provider = self.provider
        observed = provider.reconcile(provider_job_id)
        if observed.provider_job_id != provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider observation job does not match operation")
        if observed.outcome != "succeeded":
            return observed
        if observed.artifact_media_type != VIDEO_MEDIA_TYPE:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "video artifact media type is not supported")
        if observed.result_fingerprint != "sha256:" + hashlib.sha256(observed.artifact_bytes).hexdigest():
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "video provider artifact commitment is invalid")
        if not observed.artifact_bytes or len(observed.artifact_bytes) < 100:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video artifact is empty")
        pending_name = PENDING_PREFIX + str(operation["operation_id"]) + ".mp4"
        pending = _safe_output(output_dir, pending_name)
        atomic_write_bytes(pending, observed.artifact_bytes)
        receipt = {
            "schema_version": VIDEO_SCHEMA_VERSION, "stage_run_id": stage_run_id,
            "operation": {"operation_id": operation["operation_id"], "provider_job_id": provider_job_id, "result_fingerprint": observed.result_fingerprint},
            "artifact": {"pending_path": pending_name, "sha256": sha256_file(pending), "media_type": VIDEO_MEDIA_TYPE, "size": len(observed.artifact_bytes)},
            "usage": observed.usage or {},
            "cost": {"actual_amount": observed.actual_amount, "currency": observed.currency, "cost_status": observed.cost_status, "cost_source": observed.cost_source},
        }
        atomic_write_json(_safe_output(output_dir, RECEIPT_NAME), receipt)
        return observed

    def publish_result(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any]) -> StageResult:
        receipt = read_json(_safe_output(output_dir, RECEIPT_NAME))
        if not isinstance(receipt, dict):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "video result receipt is required before publish")
        artifact_data = receipt.get("artifact")
        if not isinstance(artifact_data, dict) or not artifact_data.get("pending_path"):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "video receipt artifact is invalid")
        pending = _safe_output(output_dir, str(artifact_data["pending_path"]))
        artifact = _safe_output(output_dir, ARTIFACT_NAME)
        with open(pending, "rb") as handle:
            atomic_write_bytes(artifact, handle.read())
        if sha256_file(artifact) != artifact_data.get("sha256"):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "published video bytes do not match receipt")
        cost = receipt.get("cost") if isinstance(receipt.get("cost"), dict) else {}
        authority = _safe_output(output_dir, AUTHORITY_NAME)
        atomic_write_json(authority, {
            "schema_version": "2", "stage_run_id": stage_run_id,
            "artifact": {"path": ARTIFACT_NAME, "sha256": sha256_file(artifact), "media_type": VIDEO_MEDIA_TYPE},
            "receipt": {"path": RECEIPT_NAME, "sha256": sha256_file(_safe_output(output_dir, RECEIPT_NAME))},
            "operation": {"operation_id": operation.get("operation_id"), "provider_job_id": operation.get("provider_job_id"), "result_fingerprint": operation.get("result_fingerprint")},
            "adapter_contract_version": self.contract_version,
            "settlement": {key: cost[key] for key in ("cost_source", "actual_amount", "currency", "cost_status") if key in cost},
        })
        return self.inspect(stage_run_id=stage_run_id, output_dir=output_dir) or StageResult(status="failed", stage_run_id=stage_run_id)

    def inspect(self, *, stage_run_id: str, output_dir: str) -> StageResult | None:
        authority = _safe_output(output_dir, AUTHORITY_NAME)
        value = read_json(authority)
        if value is None:
            return None
        artifact = value.get("artifact") if isinstance(value.get("artifact"), dict) else {}
        receipt_ref = value.get("receipt") if isinstance(value.get("receipt"), dict) else {}
        operation = value.get("operation") if isinstance(value.get("operation"), dict) else {}
        if (
            set(value) != {"schema_version", "stage_run_id", "artifact", "receipt", "operation", "adapter_contract_version", "settlement"}
            or value.get("schema_version") != "2" or value.get("stage_run_id") != stage_run_id
            or value.get("adapter_contract_version") != self.contract_version
            or set(artifact) != {"path", "sha256", "media_type"} or artifact.get("media_type") != VIDEO_MEDIA_TYPE
            or artifact.get("path") != ARTIFACT_NAME or set(receipt_ref) != {"path", "sha256"}
            or receipt_ref.get("path") != RECEIPT_NAME
            or set(operation) != {"operation_id", "provider_job_id", "result_fingerprint"}
            or not isinstance(value.get("settlement"), dict)
        ):
            return StageResult(status="failed", stage_run_id=stage_run_id, reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value, message="video public authority invalid")
        output = _safe_output(output_dir, ARTIFACT_NAME)
        receipt_path = _safe_output(output_dir, RECEIPT_NAME)
        if not os.path.isfile(output) or not os.path.isfile(receipt_path) or artifact["sha256"] != sha256_file(output) or receipt_ref["sha256"] != sha256_file(receipt_path) or operation["result_fingerprint"] != "sha256:" + sha256_file(output):
            return StageResult(status="failed", stage_run_id=stage_run_id, reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value, message="video authority binding failed")
        return StageResult(status="completed", stage_run_id=stage_run_id,
                           artifacts=({"path": output, "version_id": "sha256:" + sha256_file(output)},),
                           authority_path=authority, authority_hash=sha256_file(authority),
                           authority_files=({"path": authority, "sha256": sha256_file(authority)}, {"path": receipt_path, "sha256": sha256_file(receipt_path)}))
