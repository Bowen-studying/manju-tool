"""Deterministic storyboard-to-voice-script stage."""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

from manju.pipeline.storyboard_schema import SCHEMA_VERSION, get_duration_seconds, normalize_storyboard
from manju.production.adapters.base import StageResult
from manju.production.artifacts import ArtifactRef
from manju.production.models import ReasonCode
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json, read_json


VOICE_SCRIPT_SCHEMA_VERSION = "voice-script-v1"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_voice_script(storyboard: dict[str, Any], storyboard_ref: dict[str, str]) -> dict[str, Any]:
    """Return a stable script without inference, rewriting, or provider calls."""
    ArtifactRef.from_dict(storyboard_ref)
    if storyboard.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported storyboard schema version: {storyboard.get('schema_version')!r}")
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or any(not isinstance(scene, dict) for scene in scenes):
        raise ValueError("storyboard scenes are invalid")
    for scene in scenes:
        shots = scene.get("shots")
        if not isinstance(shots, list) or any(not isinstance(shot, dict) for shot in shots):
            raise ValueError("storyboard shots are invalid")
        for shot in shots:
            if "duration_seconds" not in shot:
                continue
            declared_duration = shot["duration_seconds"]
            if type(declared_duration) not in (int, float):
                raise ValueError("shot duration must be a finite positive number")
            try:
                numeric_duration = float(declared_duration)
            except (OverflowError, ValueError) as exc:
                raise ValueError("shot duration must be a finite positive number") from exc
            if not math.isfinite(numeric_duration) or numeric_duration <= 0:
                raise ValueError("shot duration must be a finite positive number")

    normalized = normalize_storyboard(storyboard)
    entries: list[dict[str, Any]] = []
    elapsed = 0.0
    sequence = 0
    for scene_index, scene in enumerate(normalized["scenes"]):
        source_scene = scenes[scene_index]
        source_shots = source_scene["shots"]
        for shot_index, shot in enumerate(scene["shots"]):
            source_shot = source_shots[shot_index]
            audio = source_shot.get("audio") if isinstance(source_shot.get("audio"), dict) else {}
            dialogue = _text(audio.get("dialogue"))
            narration = _text(audio.get("narration"))
            duration = round(float(get_duration_seconds(shot)), 6)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("shot duration must be a finite positive number")
            start = round(elapsed, 6)
            if not math.isfinite(start):
                raise ValueError("shot timeline must remain finite")
            cues: list[tuple[str, str, str]] = []
            if dialogue:
                cues.append(("dialogue", _text(audio.get("speaker")) or "unknown", dialogue))
            if narration:
                cues.append(("narration", "narrator", narration))
            if not cues:
                legacy = _text(source_shot.get("dialogue_narration"))
                if legacy:
                    cues.append(("legacy_spoken", "unknown", legacy))
            for kind, speaker, spoken in cues:
                sequence += 1
                entries.append({
                    "sequence": sequence,
                    "scene_id": str(scene.get("scene_id", "")),
                    "shot_id": str(shot.get("shot_id", "")),
                    "kind": kind,
                    "speaker": speaker,
                    "text": spoken,
                    "shot_duration_seconds": duration,
                    "shot_start_seconds": start,
                })
            elapsed = round(elapsed + duration, 6)
            if not math.isfinite(elapsed):
                raise ValueError("shot timeline must remain finite")
    return {
        "schema_version": VOICE_SCRIPT_SCHEMA_VERSION,
        "storyboard": ArtifactRef.from_dict(storyboard_ref).to_dict(),
        "title": _text(normalized.get("title")),
        "entry_count": len(entries),
        "entries": entries,
    }


class VoiceScriptStageAdapter:
    """Offline adapter whose manifest and artifact are fully content-bound."""

    contract_version = "voice-script-adapter-m4-0-v1"
    artifact_name = "voice_script.json"
    authority_name = "voice_script_run.json"

    @staticmethod
    def _safe_path(output_dir: str, name: str) -> str:
        root = os.path.realpath(output_dir)
        path = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(path) != root:
            raise ValueError("voice-script path escaped stage directory")
        return path

    def inspect(
        self, *, stage_run_id: str, output_dir: str, storyboard_ref: dict[str, str]
    ) -> StageResult | None:
        artifact_path = self._safe_path(output_dir, self.artifact_name)
        authority_path = self._safe_path(output_dir, self.authority_name)
        artifact = read_json(artifact_path)
        authority = read_json(authority_path)
        if artifact is None and authority is None:
            return None
        if not isinstance(artifact, dict) or not isinstance(authority, dict):
            return StageResult(
                status="failed", stage_run_id=stage_run_id,
                reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                message="voice-script artifact or authority is missing",
            )
        artifact_hash = sha256_file(artifact_path)
        expected_ref = ArtifactRef.from_dict(storyboard_ref).to_dict()
        valid = (
            set(authority) == {
                "schema_version", "stage_run_id", "status", "adapter_contract_version",
                "storyboard", "artifact",
            }
            and authority.get("schema_version") == "voice-script-run-v1"
            and authority.get("stage_run_id") == stage_run_id
            and authority.get("status") == "completed"
            and authority.get("adapter_contract_version") == self.contract_version
            and authority.get("storyboard") == expected_ref
            and authority.get("artifact") == {
                "path": self.artifact_name,
                "sha256": artifact_hash,
                "schema_version": VOICE_SCRIPT_SCHEMA_VERSION,
            }
            and set(artifact) == {"schema_version", "storyboard", "title", "entry_count", "entries"}
            and artifact.get("schema_version") == VOICE_SCRIPT_SCHEMA_VERSION
            and artifact.get("storyboard") == expected_ref
            and isinstance(artifact.get("title"), str)
            and isinstance(artifact.get("entries"), list)
            and artifact.get("entry_count") == len(artifact["entries"])
        )
        if not valid:
            return StageResult(
                status="failed", stage_run_id=stage_run_id,
                reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                message="voice-script authority binding is invalid",
                authority_path=authority_path,
                authority_hash=sha256_file(authority_path),
            )
        return StageResult(
            status="completed",
            stage_run_id=stage_run_id,
            artifacts=({
                "logical_id": "voice_script.main",
                "version_id": f"sha256:{artifact_hash}",
                "path": artifact_path,
            },),
            authority_path=authority_path,
            authority_hash=sha256_file(authority_path),
            authority_files=({"path": authority_path, "sha256": sha256_file(authority_path)},),
        )

    def execute(
        self, *, stage_run_id: str, storyboard_path: str, storyboard_ref: dict[str, str], output_dir: str
    ) -> StageResult:
        expected = ArtifactRef.from_dict(storyboard_ref)
        try:
            with open(storyboard_path, "rb") as handle:
                storyboard_bytes = handle.read()
        except OSError:
            storyboard_bytes = b""
        if hashlib.sha256(storyboard_bytes).hexdigest() != expected.version_id[7:]:
            return StageResult(
                status="failed", stage_run_id=stage_run_id,
                reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                message="voice-script storyboard input is missing or changed",
            )
        try:
            storyboard = json.loads(storyboard_bytes.decode("utf-8"))
            if not isinstance(storyboard, dict):
                raise ValueError("storyboard must be an object")
            value = build_voice_script(storyboard, expected.to_dict())
            expected_artifact = json.dumps(
                value, ensure_ascii=False, indent=2, allow_nan=False,
            ).encode("utf-8")
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return StageResult(
                status="failed", stage_run_id=stage_run_id,
                reason_code=ReasonCode.VOICE_SCRIPT_FAILED.value,
                message=f"voice-script input is invalid: {exc}",
            )
        existing = self.inspect(
            stage_run_id=stage_run_id, output_dir=output_dir, storyboard_ref=storyboard_ref,
        )
        if existing is not None:
            if existing.status != "completed":
                return existing
            artifact_path = self._safe_path(output_dir, self.artifact_name)
            try:
                with open(artifact_path, "rb") as handle:
                    actual_artifact = handle.read()
            except OSError:
                actual_artifact = b""
            if actual_artifact != expected_artifact:
                return StageResult(
                    status="failed", stage_run_id=stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message="voice-script artifact does not match its storyboard derivation",
                    authority_path=existing.authority_path,
                    authority_hash=existing.authority_hash,
                )
            return existing
        os.makedirs(output_dir, exist_ok=True)
        artifact_path = self._safe_path(output_dir, self.artifact_name)
        authority_path = self._safe_path(output_dir, self.authority_name)
        atomic_write_json(artifact_path, value)
        atomic_write_json(authority_path, {
            "schema_version": "voice-script-run-v1",
            "stage_run_id": stage_run_id,
            "status": "completed",
            "adapter_contract_version": self.contract_version,
            "storyboard": expected.to_dict(),
            "artifact": {
                "path": self.artifact_name,
                "sha256": sha256_file(artifact_path),
                "schema_version": VOICE_SCRIPT_SCHEMA_VERSION,
            },
        })
        return self.inspect(
            stage_run_id=stage_run_id, output_dir=output_dir, storyboard_ref=expected.to_dict(),
        ) or StageResult(status="failed", stage_run_id=stage_run_id)
