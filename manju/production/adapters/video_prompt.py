"""Deterministic, offline storyboard-to-video-prompt stage for M5.0."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from typing import Any

from manju.pipeline.storyboard_schema import get_duration_seconds, normalize_storyboard
from manju.production.adapters.base import StageResult
from manju.production.artifacts import ArtifactRef
from manju.production.models import (
    ProductionError,
    ReasonCode,
    VIDEO_PROMPT_MAX_DURATION_SECONDS,
    VIDEO_PROMPT_MAX_INPUT_BYTES,
    VIDEO_PROMPT_MAX_OUTPUT_BYTES,
    VIDEO_PROMPT_MAX_PROMPT_CHARS,
    VIDEO_PROMPT_MAX_SHOTS,
    VIDEO_PROMPT_MAX_TEXT_CHARS,
    VIDEO_PROMPT_MAX_TOTAL_DURATION_SECONDS,
    canonical_json,
)
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json, read_json


VIDEO_PROMPT_SCHEMA_VERSION = "video-prompt-v1"
VIDEO_PROMPT_RUN_SCHEMA_VERSION = "video-prompt-run-v1"
SUPPORTED_STORYBOARD_SCHEMA_VERSIONS = {None, "1", "1.0", "2", "2.0"}


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _derivation_bytes(value: dict[str, Any]) -> bytes:
    """Return manifest bytes with the non-derivable authority commitment blanked."""
    normalized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    normalized.setdefault("integrity", {})["authority_sha256"] = ""
    return _json_bytes(normalized)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _safe_stage_file(output_dir: str, name: str) -> str:
    """Resolve one fixed stage file without following links or escaping."""
    if not isinstance(name, str) or not name or os.path.basename(name) != name:
        raise ValueError("video-prompt file name is invalid")
    root = os.path.abspath(output_dir)
    current = root
    while current and current != os.path.dirname(current):
        if os.path.lexists(current):
            value = os.lstat(current)
            if stat.S_ISLNK(value.st_mode) or bool(
                getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("video-prompt output directory cannot contain a link or reparse point")
        current = os.path.dirname(current)
    candidate = os.path.abspath(os.path.join(root, name))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained or os.path.dirname(candidate) != root:
        raise ValueError("video-prompt path escaped stage directory")
    if os.path.lexists(candidate):
        value = os.lstat(candidate)
        if stat.S_ISLNK(value.st_mode) or bool(
            getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("video-prompt output cannot be a link or reparse point")
    return candidate


def _input_file(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("storyboard input path is missing")
    absolute = os.path.abspath(path)
    if not os.path.isfile(absolute):
        raise ValueError("storyboard input is unavailable")
    value = os.lstat(absolute)
    if stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise ValueError("storyboard input cannot be a link or reparse point")
    return absolute


def _limits(settings: dict[str, Any] | None) -> dict[str, int | float]:
    settings = settings if isinstance(settings, dict) else {}
    integer_limits = {
        "max_input_bytes": (VIDEO_PROMPT_MAX_INPUT_BYTES, 1),
        "max_output_bytes": (VIDEO_PROMPT_MAX_OUTPUT_BYTES, 1),
        "max_shots": (VIDEO_PROMPT_MAX_SHOTS, 1),
        "max_text_chars": (VIDEO_PROMPT_MAX_TEXT_CHARS, 1),
        "max_prompt_chars": (VIDEO_PROMPT_MAX_PROMPT_CHARS, 1),
    }
    result: dict[str, int | float] = {}
    for key, (hard_max, minimum) in integer_limits.items():
        value = settings.get(key, hard_max)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= hard_max:
            raise ValueError(f"video-prompt {key} exceeds the offline resource limit")
        result[key] = value
    float_limits = {
        "max_duration_seconds": VIDEO_PROMPT_MAX_DURATION_SECONDS,
        "max_total_duration_seconds": VIDEO_PROMPT_MAX_TOTAL_DURATION_SECONDS,
    }
    for key, hard_max in float_limits.items():
        value = settings.get(key, hard_max)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"video-prompt {key} is invalid")
        value = float(value)
        if not math.isfinite(value) or value <= 0 or value > hard_max:
            raise ValueError(f"video-prompt {key} exceeds the offline resource limit")
        result[key] = value
    return result


def _value_text(value: Any, *, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"video-prompt {field} must be text")
    result = value.strip()
    if len(result) > limit:
        raise ValueError(f"video-prompt {field} exceeds the text limit")
    return result


def _shot_input(normalized_shot: dict[str, Any], *, limit: int) -> dict[str, str]:
    visual = normalized_shot.get("visual") if isinstance(normalized_shot.get("visual"), dict) else {}
    prompts = normalized_shot.get("prompts") if isinstance(normalized_shot.get("prompts"), dict) else {}
    fields = {
        "visual_description": _value_text(visual.get("description"), field="visual_description", limit=limit),
        "video_prompt": _value_text(prompts.get("video"), field="video_prompt", limit=limit),
        "video_prompt_cn": _value_text(prompts.get("video_cn"), field="video_prompt_cn", limit=limit),
        "video_prompt_en": _value_text(prompts.get("video_en"), field="video_prompt_en", limit=limit),
        "shot_type": _value_text(visual.get("shot_type"), field="shot_type", limit=limit),
        "composition": _value_text(visual.get("composition"), field="composition", limit=limit),
        "camera_movement": _value_text(visual.get("camera_movement"), field="camera_movement", limit=limit),
        "color_tone": _value_text(visual.get("color_tone"), field="color_tone", limit=limit),
    }
    return fields


def _prompt_for_shot(*, scene_id: str, shot_id: str, fields: dict[str, str]) -> tuple[str, str]:
    source_lines = [f"{key}={value}" for key, value in fields.items() if value]
    input_text = " | ".join(source_lines)
    base = (
        fields.get("video_prompt_cn")
        or fields.get("video_prompt_en")
        or fields.get("video_prompt")
        or fields.get("visual_description")
        or f"shot {scene_id}/{shot_id}"
    )
    modifiers = [
        ("shot type", fields.get("shot_type", "")),
        ("composition", fields.get("composition", "")),
        ("camera movement", fields.get("camera_movement", "")),
        ("color tone", fields.get("color_tone", "")),
    ]
    prompt = "; ".join([f"{base}"] + [f"{label}: {value}" for label, value in modifiers if value])
    return input_text, prompt


def _storyboard_ref(value: dict[str, str]) -> dict[str, str]:
    ref = ArtifactRef.from_dict(value)
    if ref.logical_id != "storyboard.output":
        raise ValueError("video-prompt input must be storyboard.output")
    return ref.to_dict()


def build_video_prompt(
    storyboard: dict[str, Any], storyboard_ref: dict[str, str], *, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build one deterministic prompt record for every storyboard shot."""
    ref = _storyboard_ref(storyboard_ref)
    limits = _limits(settings)
    if not isinstance(storyboard, dict) or storyboard.get("schema_version") not in SUPPORTED_STORYBOARD_SCHEMA_VERSIONS:
        raise ValueError("unsupported storyboard schema version")
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or any(not isinstance(scene, dict) for scene in scenes):
        raise ValueError("storyboard scenes are invalid")
    if any(not isinstance(scene.get("shots"), list) for scene in scenes):
        raise ValueError("storyboard shots are invalid")
    normalized = normalize_storyboard(storyboard)
    if len(normalized["scenes"]) != len(scenes):
        raise ValueError("storyboard scene order is invalid")

    shots: list[dict[str, Any]] = []
    seen_scene_ids: set[str] = set()
    seen_shot_ids: set[str] = set()
    total_duration = 0.0
    sequence = 0
    for scene_index, (source_scene, scene) in enumerate(zip(scenes, normalized["scenes"], strict=True), start=1):
        scene_id = source_scene.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id.strip() or scene_id.strip() != str(scene.get("scene_id", "")):
            raise ValueError(f"scene {scene_index} id is invalid")
        scene_id = scene_id.strip()
        if scene_id in seen_scene_ids:
            raise ValueError("storyboard scene IDs are duplicated")
        seen_scene_ids.add(scene_id)
        source_shots = source_scene["shots"]
        normalized_shots = scene.get("shots")
        if not isinstance(normalized_shots, list) or len(normalized_shots) != len(source_shots):
            raise ValueError("storyboard shot order is invalid")
        for shot_index, (source_shot, shot) in enumerate(zip(source_shots, normalized_shots, strict=True), start=1):
            if not isinstance(source_shot, dict) or not isinstance(shot, dict):
                raise ValueError("storyboard shot is invalid")
            shot_id = source_shot.get("shot_id")
            if not isinstance(shot_id, str) or not shot_id.strip() or shot_id.strip() != str(shot.get("shot_id", "")):
                raise ValueError(f"shot {scene_id}.{shot_index} id is invalid")
            shot_id = shot_id.strip()
            if shot_id in seen_shot_ids:
                raise ValueError("storyboard shot IDs are duplicated")
            seen_shot_ids.add(shot_id)
            raw_duration = source_shot.get("duration_seconds")
            if raw_duration is not None and (not isinstance(raw_duration, (int, float)) or isinstance(raw_duration, bool)):
                raise ValueError("shot duration must be a finite positive number")
            duration = float(get_duration_seconds(source_shot))
            if not math.isfinite(duration) or duration <= 0 or duration > float(limits["max_duration_seconds"]):
                raise ValueError("shot duration exceeds the video-prompt limit")
            total_duration += duration
            if not math.isfinite(total_duration) or total_duration > float(limits["max_total_duration_seconds"]):
                raise ValueError("storyboard duration exceeds the video-prompt limit")
            sequence += 1
            if sequence > int(limits["max_shots"]):
                raise ValueError("storyboard shot count exceeds the video-prompt limit")
            fields = _shot_input(shot, limit=int(limits["max_text_chars"]))
            input_text, prompt = _prompt_for_shot(scene_id=scene_id, shot_id=shot_id, fields=fields)
            if len(input_text) > int(limits["max_prompt_chars"]):
                raise ValueError("video-prompt input text exceeds the prompt limit")
            if len(prompt) > int(limits["max_prompt_chars"]):
                raise ValueError("video-prompt prompt exceeds the prompt limit")
            shots.append({
                "sequence": sequence,
                "scene_id": scene_id,
                "shot_id": shot_id,
                "duration_seconds": round(duration, 6),
                "duration": round(duration, 6),
                "input_text": input_text,
                "input_text_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
                "prompt": prompt,
                "deterministic_prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            })

    value = {
        "schema_version": VIDEO_PROMPT_SCHEMA_VERSION,
        "storyboard": ref,
        "input_snapshot_sha256": ref["version_id"].removeprefix("sha256:"),
        "shot_count": len(shots),
        "shots": shots,
        "integrity": {
            "authority_path": "video_prompt_run.json",
            "authority_sha256": "",
        },
    }
    if len(_json_bytes(value)) > int(limits["max_output_bytes"]):
        raise ValueError("video-prompt output exceeds the offline resource limit")
    return value


def _authority_binding(value: dict[str, Any]) -> str:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    manifest = normalized.get("manifest") if isinstance(normalized.get("manifest"), dict) else {}
    manifest["sha256"] = ""
    normalized["manifest"] = manifest
    return _hash_bytes(canonical_json(normalized).encode("utf-8"))


class VideoPromptStageAdapter:
    """Generate and verify video prompts without an LLM, provider, or call ledger."""

    contract_version = "video-prompt-adapter-m5.0-v1"
    model_profile = "deterministic-offline"
    artifact_name = "video_prompt.json"
    authority_name = "video_prompt_run.json"

    def _failed(self, stage_run_id: str, message: str, *, authority_path: str = "", authority_hash: str = "") -> StageResult:
        return StageResult(
            status="failed",
            stage_run_id=stage_run_id,
            reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
            message=message,
        )

    def _snapshot(
        self, *, storyboard_path: str, storyboard_ref: dict[str, str], settings: dict[str, Any] | None
    ) -> tuple[dict[str, str], bytes, dict[str, Any], dict[str, int | float]]:
        ref = _storyboard_ref(storyboard_ref)
        limits = _limits(settings)
        path = _input_file(storyboard_path)
        with open(path, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("storyboard input is not a regular file")
            snapshot = handle.read(int(limits["max_input_bytes"]) + 1)
        if len(snapshot) > int(limits["max_input_bytes"]):
            raise ValueError("storyboard input exceeds the video-prompt size limit")
        if _hash_bytes(snapshot) != ref["version_id"].removeprefix("sha256:"):
            raise ValueError("storyboard input snapshot hash does not match storyboard.output")
        try:
            storyboard = json.loads(snapshot.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("storyboard input is not valid JSON") from exc
        value = build_video_prompt(storyboard, ref, settings=settings)
        return ref, snapshot, value, limits

    def _validate_manifest(
        self, value: Any, *, storyboard_ref: dict[str, str], settings: dict[str, Any] | None = None
    ) -> None:
        ref = _storyboard_ref(storyboard_ref)
        limits = _limits(settings)
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "storyboard", "input_snapshot_sha256", "shot_count", "shots", "integrity",
        }:
            raise ValueError("video-prompt manifest schema is invalid")
        if value.get("schema_version") != VIDEO_PROMPT_SCHEMA_VERSION or value.get("storyboard") != ref:
            raise ValueError("video-prompt manifest storyboard binding is invalid")
        if value.get("input_snapshot_sha256") != ref["version_id"].removeprefix("sha256:"):
            raise ValueError("video-prompt input snapshot binding is invalid")
        shots = value.get("shots")
        if not isinstance(shots, list) or value.get("shot_count") != len(shots) or len(shots) > int(limits["max_shots"]):
            raise ValueError("video-prompt shot count is invalid")
        if not isinstance(value.get("integrity"), dict) or set(value["integrity"]) != {"authority_path", "authority_sha256"}:
            raise ValueError("video-prompt integrity binding is invalid")
        if value["integrity"].get("authority_path") != self.authority_name or not isinstance(value["integrity"].get("authority_sha256"), str):
            raise ValueError("video-prompt authority binding is invalid")
        expected_ids: list[tuple[str, str]] = []
        total_duration = 0.0
        for expected_sequence, item in enumerate(shots, start=1):
            if not isinstance(item, dict) or set(item) != {
                "sequence", "scene_id", "shot_id", "duration_seconds", "duration", "input_text",
                "input_text_sha256", "prompt", "deterministic_prompt", "prompt_sha256",
            }:
                raise ValueError("video-prompt shot schema is invalid")
            if item.get("sequence") != expected_sequence or not isinstance(item.get("scene_id"), str) or not item.get("scene_id"):
                raise ValueError("video-prompt shot sequence or scene ID is invalid")
            if not isinstance(item.get("shot_id"), str) or not item.get("shot_id"):
                raise ValueError("video-prompt shot ID is invalid")
            duration = item.get("duration_seconds")
            if item.get("duration") != duration or not isinstance(duration, (int, float)) or isinstance(duration, bool):
                raise ValueError("video-prompt duration is invalid")
            duration = float(duration)
            if not math.isfinite(duration) or duration <= 0 or duration > float(limits["max_duration_seconds"]):
                raise ValueError("video-prompt duration exceeds the limit")
            total_duration += duration
            if total_duration > float(limits["max_total_duration_seconds"]):
                raise ValueError("video-prompt total duration exceeds the limit")
            input_text = item.get("input_text")
            prompt = item.get("prompt")
            if (
                not isinstance(input_text, str) or len(input_text) > int(limits["max_prompt_chars"])
                or item.get("input_text_sha256") != hashlib.sha256(input_text.encode("utf-8")).hexdigest()
                or not isinstance(prompt, str) or len(prompt) > int(limits["max_prompt_chars"])
                or item.get("deterministic_prompt") != prompt
                or item.get("prompt_sha256") != hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            ):
                raise ValueError("video-prompt text binding is invalid")
            expected_ids.append((item["scene_id"], item["shot_id"]))
        if len(set(expected_ids)) != len(expected_ids) or len({shot_id for _, shot_id in expected_ids}) != len(expected_ids):
            raise ValueError("video-prompt IDs are duplicated")
        if len(_json_bytes(value)) > int(limits["max_output_bytes"]):
            raise ValueError("video-prompt output exceeds the offline resource limit")

    def inspect(
        self,
        *,
        stage_run_id: str,
        output_dir: str,
        storyboard_ref: dict[str, str],
        storyboard_path: str,
        settings: dict[str, Any] | None = None,
    ) -> StageResult | None:
        try:
            artifact_path = _safe_stage_file(output_dir, self.artifact_name)
            authority_path = _safe_stage_file(output_dir, self.authority_name)
            artifact = read_json(artifact_path)
            authority = read_json(authority_path)
        except (OSError, ValueError, TypeError):
            return self._failed(stage_run_id, "video-prompt output path is invalid")
        if artifact is None and authority is None:
            return None
        if not isinstance(artifact, dict) or not isinstance(authority, dict):
            return self._failed(stage_run_id, "video-prompt manifest or authority is missing")
        try:
            expected_ref = _storyboard_ref(storyboard_ref)
            limits = _limits(settings)
            self._validate_manifest(artifact, storyboard_ref=expected_ref, settings=settings)
            artifact_hash = sha256_file(artifact_path)
            if set(authority) != {
                "schema_version", "stage_run_id", "status", "adapter_contract_version", "storyboard",
                "input_snapshot", "manifest", "limits",
            }:
                raise ValueError("video-prompt authority schema is invalid")
            if (
                authority.get("schema_version") != VIDEO_PROMPT_RUN_SCHEMA_VERSION
                or authority.get("stage_run_id") != stage_run_id
                or authority.get("status") != "completed"
                or authority.get("adapter_contract_version") != self.contract_version
                or authority.get("storyboard") != expected_ref
                or authority.get("limits") != limits
            ):
                raise ValueError("video-prompt authority binding is invalid")
            input_snapshot = authority.get("input_snapshot")
            if not isinstance(input_snapshot, dict) or set(input_snapshot) != {"sha256", "size"}:
                raise ValueError("video-prompt input snapshot record is invalid")
            if input_snapshot.get("sha256") != expected_ref["version_id"].removeprefix("sha256:") or not isinstance(input_snapshot.get("size"), int) or input_snapshot["size"] < 0:
                raise ValueError("video-prompt input snapshot record is invalid")
            manifest = authority.get("manifest")
            if not isinstance(manifest, dict) or manifest != {
                "path": self.artifact_name, "sha256": artifact_hash, "schema_version": VIDEO_PROMPT_SCHEMA_VERSION,
            }:
                raise ValueError("video-prompt manifest hash binding is invalid")
            if artifact["integrity"]["authority_sha256"] != _authority_binding(authority):
                raise ValueError("video-prompt authority hash binding is invalid")
            _ref, snapshot, expected_value, _expected_limits = self._snapshot(
                storyboard_path=storyboard_path, storyboard_ref=expected_ref, settings=settings,
            )
            if input_snapshot.get("size") != len(snapshot) or _derivation_bytes(expected_value) != _derivation_bytes(artifact):
                raise ValueError("video-prompt manifest is not the deterministic storyboard derivation")
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, ProductionError):
            return self._failed(stage_run_id, "video-prompt authority binding is invalid")
        return StageResult(
            status="completed",
            stage_run_id=stage_run_id,
            artifacts=({"logical_id": "video_prompt.main", "version_id": f"sha256:{artifact_hash}", "path": artifact_path},),
            authority_path=authority_path,
            authority_hash=sha256_file(authority_path),
            authority_files=(
                {"path": authority_path, "sha256": sha256_file(authority_path)},
                {"path": artifact_path, "sha256": artifact_hash},
            ),
            metadata={"shot_count": artifact["shot_count"]},
        )

    def execute(
        self,
        *,
        stage_run_id: str,
        storyboard_path: str,
        storyboard_ref: dict[str, str],
        output_dir: str,
        settings: dict[str, Any] | None = None,
    ) -> StageResult:
        try:
            expected_ref, snapshot, value, limits = self._snapshot(
                storyboard_path=storyboard_path, storyboard_ref=storyboard_ref, settings=settings,
            )
            expected_bytes = _json_bytes(value)
            if len(expected_bytes) > int(limits["max_output_bytes"]):
                raise ValueError("video-prompt output exceeds the offline resource limit")
            artifact_path = _safe_stage_file(output_dir, self.artifact_name)
            authority_path = _safe_stage_file(output_dir, self.authority_name)
            authority = {
                "schema_version": VIDEO_PROMPT_RUN_SCHEMA_VERSION,
                "stage_run_id": stage_run_id,
                "status": "completed",
                "adapter_contract_version": self.contract_version,
                "storyboard": expected_ref,
                "input_snapshot": {"sha256": _hash_bytes(snapshot), "size": len(snapshot)},
                "manifest": {"path": self.artifact_name, "sha256": "", "schema_version": VIDEO_PROMPT_SCHEMA_VERSION},
                "limits": limits,
            }
            value["integrity"]["authority_sha256"] = _authority_binding(authority)
            authority["manifest"]["sha256"] = _hash_bytes(_json_bytes(value))
            existing = self.inspect(
                stage_run_id=stage_run_id, output_dir=output_dir, storyboard_ref=expected_ref,
                storyboard_path=storyboard_path, settings=settings,
            )
            if existing is not None:
                if existing.status != "completed":
                    artifact_exists = os.path.isfile(artifact_path)
                    authority_exists = os.path.isfile(authority_path)
                    if artifact_exists != authority_exists:
                        present = read_json(artifact_path if artifact_exists else authority_path)
                        expected = value if artifact_exists else authority
                        if present != expected:
                            return existing
                        if artifact_exists:
                            atomic_write_json(authority_path, authority)
                        else:
                            atomic_write_json(artifact_path, value)
                        return self.inspect(
                            stage_run_id=stage_run_id, output_dir=output_dir, storyboard_ref=expected_ref,
                            storyboard_path=storyboard_path, settings=settings,
                        ) or self._failed(stage_run_id, "video-prompt recovery verification failed")
                    return existing
                with open(artifact_path, "rb") as handle:
                    actual = json.loads(handle.read().decode("utf-8"))
                    if _derivation_bytes(actual) != _derivation_bytes(value):
                        return self._failed(stage_run_id, "video-prompt artifact does not match its storyboard derivation")
                return existing
            os.makedirs(output_dir, exist_ok=True)
            # The authority binding deliberately excludes its final manifest hash,
            # avoiding a circular hash while still binding both files in both
            # directions. The manifest binds the resulting authority commitment.
            atomic_write_json(artifact_path, value)
            if sha256_file(artifact_path) != authority["manifest"]["sha256"]:
                raise ValueError("video-prompt manifest write hash is invalid")
            atomic_write_json(authority_path, authority)
            return self.inspect(
                stage_run_id=stage_run_id, output_dir=output_dir, storyboard_ref=expected_ref,
                storyboard_path=storyboard_path, settings=settings,
            ) or self._failed(stage_run_id, "video-prompt output failed post-write verification")
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, ProductionError):
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.VIDEO_PROMPT_FAILED.value,
                               message="video-prompt stage failed")
