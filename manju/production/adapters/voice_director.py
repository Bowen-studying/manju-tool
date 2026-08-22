"""LangGraph-backed, offline voice-direction stage adapter."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from typing import Any

from manju.pipeline.voice_director_agent import (
    DeterministicVoiceDirectorModel,
    VOICE_DIRECTOR_AGENT_VERSION,
    VOICE_DIRECTOR_POLICY_VERSION,
    VoiceDirectorModelPort,
    read_voice_director_checkpoint,
    run_voice_director,
    voice_director_state_fingerprint,
)
from manju.production.adapters.base import StageResult
from manju.production.artifacts import ArtifactRef
from manju.production.models import ReasonCode
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json, read_json


VOICE_DIRECTION_SCHEMA_VERSION = "voice-direction-v1"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(root: str, name: str) -> str:
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root:
        raise ValueError("voice-direction path escaped stage directory")
    return path


def _policy_ref(policy: dict[str, Any]) -> dict[str, str]:
    raw = json.dumps(policy, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    return {"logical_id": "voice_director.policy", "version_id": f"sha256:{_hash_bytes(raw)}"}


def _validate_script(value: Any, expected_ref: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema_version") != "voice-script-v1" or value.get("storyboard") is None:
        raise ValueError("voice-script artifact schema is invalid")
    if value.get("storyboard") != expected_ref:
        raise ValueError("voice-script storyboard binding is invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or value.get("entry_count") != len(entries):
        raise ValueError("voice-script entries are invalid")
    sequences: list[int] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "sequence", "scene_id", "shot_id", "kind", "speaker", "text",
            "shot_duration_seconds", "shot_start_seconds",
        }:
            raise ValueError("voice-script cue schema is invalid")
        if not isinstance(item["sequence"], int) or not isinstance(item["text"], str):
            raise ValueError("voice-script cue types are invalid")
        for field in ("shot_duration_seconds", "shot_start_seconds"):
            if not isinstance(item[field], (int, float)) or isinstance(item[field], bool) or not math.isfinite(float(item[field])):
                raise ValueError("voice-script timing is invalid")
        sequences.append(item["sequence"])
    if sequences != list(range(1, len(entries) + 1)):
        raise ValueError("voice-script sequence is invalid")
    return entries


class VoiceDirectorStageAdapter:
    contract_version = "voice-director-adapter-m4.1-v1"
    artifact_name = "voice_direction.json"
    authority_name = "voice_director_run.json"
    checkpoint_name = "checkpoints.sqlite"
    checkpoint_seal_name = "voice_director_checkpoint_seal.json"
    call_receipt_name = "voice_director_call_receipt.json"

    def __init__(self, model_port: VoiceDirectorModelPort | None = None):
        self.model_port = model_port or DeterministicVoiceDirectorModel()

    @property
    def model_profile(self) -> str:
        if isinstance(self.model_port, DeterministicVoiceDirectorModel):
            return "deterministic-mock"
        declared = getattr(self.model_port, "model_profile", "")
        if isinstance(declared, str) and declared:
            return declared
        model_type = type(self.model_port)
        return f"{model_type.__module__}.{model_type.__qualname__}"

    def inspect(
        self, *, stage_run_id: str, output_dir: str, storyboard_ref: dict[str, str],
        voice_script_ref: dict[str, str], policy_ref: dict[str, str],
        voice_script_path: str = "", policy_path: str = "",
    ) -> StageResult | None:
        artifact_path = _safe_path(output_dir, self.artifact_name)
        authority_path = _safe_path(output_dir, self.authority_name)
        checkpoint_path = _safe_path(output_dir, self.checkpoint_name)
        checkpoint_seal_path = _safe_path(output_dir, self.checkpoint_seal_name)
        call_receipt_path = _safe_path(output_dir, self.call_receipt_name)
        artifact = read_json(artifact_path)
        authority = read_json(authority_path)
        if artifact is None and authority is None:
            return None
        if not isinstance(artifact, dict) or not isinstance(authority, dict):
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                               message="voice-director artifact or authority is missing")
        checkpoint_hash = sha256_file(checkpoint_path) if os.path.isfile(checkpoint_path) else ""
        expected_inputs = {"storyboard": storyboard_ref, "voice_script": voice_script_ref, "policy": policy_ref}
        valid = (
            set(authority) == {"schema_version", "stage_run_id", "status", "adapter_contract_version", "agent_version", "inputs", "artifact", "checkpoint", "checkpoint_seal", "call_receipt", "budget", "call_receipts"}
            and authority.get("schema_version") == "voice-director-run-v1"
            and authority.get("stage_run_id") == stage_run_id
            and authority.get("status") == "completed"
            and authority.get("adapter_contract_version") == self.contract_version
            and authority.get("agent_version") == VOICE_DIRECTOR_AGENT_VERSION
            and authority.get("inputs") == expected_inputs
            and authority.get("artifact") == {"path": self.artifact_name, "sha256": sha256_file(artifact_path), "schema_version": VOICE_DIRECTION_SCHEMA_VERSION}
            and authority.get("checkpoint") == {"path": self.checkpoint_name, "sha256": checkpoint_hash}
            and isinstance(authority.get("checkpoint_seal"), dict)
            and authority.get("checkpoint_seal") == {"path": self.checkpoint_seal_name, "sha256": sha256_file(checkpoint_seal_path) if os.path.isfile(checkpoint_seal_path) else ""}
            and authority.get("call_receipt") == {"path": self.call_receipt_name, "sha256": sha256_file(call_receipt_path) if os.path.isfile(call_receipt_path) else ""}
            and isinstance(authority.get("budget"), dict)
            and isinstance(authority.get("call_receipts"), list)
            and set(artifact) == {"schema_version", "storyboard", "voice_script", "policy", "entry_count", "entries", "agent"}
            and artifact.get("schema_version") == VOICE_DIRECTION_SCHEMA_VERSION
            and artifact.get("storyboard") == storyboard_ref
            and artifact.get("voice_script") == voice_script_ref
            and artifact.get("policy") == policy_ref
            and isinstance(artifact.get("entries"), list)
            and artifact.get("entry_count") == len(artifact["entries"])
            and isinstance(artifact.get("agent"), dict)
            and artifact.get("agent") == {"version": VOICE_DIRECTOR_AGENT_VERSION, "model_profile": self.model_profile}
        )
        if valid:
            receipt = read_json(call_receipt_path)
            seal = read_json(checkpoint_seal_path)
            thread_id = f"{stage_run_id}-{policy_ref['version_id']}"
            try:
                checkpoint_state = read_voice_director_checkpoint(
                    checkpoint_path=checkpoint_path, thread_id=thread_id,
                )
            except (OSError, ValueError, TypeError, RuntimeError, sqlite3.DatabaseError):
                checkpoint_state = None
            budget = authority.get("budget") if isinstance(authority.get("budget"), dict) else {}
            artifact_cues = [
                {key: item.get(key) for key in (
                    "sequence", "scene_id", "shot_id", "kind", "speaker", "text",
                    "shot_duration_seconds", "shot_start_seconds",
                )}
                for item in artifact.get("entries", []) if isinstance(item, dict)
            ]
            receipt_context = receipt.get("input_context") if isinstance(receipt, dict) else None
            valid = (
                isinstance(receipt, dict)
                and receipt.get("schema_version") == "voice-director-call-v1"
                and receipt.get("status") == "completed"
                and isinstance(receipt.get("output"), list)
                and receipt.get("agent_version") == VOICE_DIRECTOR_AGENT_VERSION
                and receipt.get("model_profile") == self.model_profile
                and isinstance(receipt.get("call_index"), int)
                and receipt.get("call_index") == 1
                and isinstance(receipt_context, dict)
                and receipt_context.get("agent_version") == VOICE_DIRECTOR_AGENT_VERSION
                and receipt_context.get("model_profile") == receipt.get("model_profile")
                and receipt_context.get("storyboard_ref") == storyboard_ref
                and receipt_context.get("voice_script_ref") == voice_script_ref
                and receipt_context.get("policy_ref") == policy_ref
                and receipt_context.get("entries") == artifact_cues
                and receipt_context.get("max_model_calls") == budget.get("max_model_calls")
                and receipt_context.get("max_steps") == budget.get("max_steps")
                and receipt.get("max_model_calls") == budget.get("max_model_calls")
                and receipt.get("max_steps") == budget.get("max_steps")
                and set(budget) == {"max_model_calls", "max_steps", "used_model_calls", "used_steps"}
                and isinstance(budget.get("max_model_calls"), int)
                and isinstance(budget.get("max_steps"), int)
                and budget.get("used_model_calls") == 1
                and isinstance(budget.get("used_steps"), int)
                and budget.get("used_steps") <= budget.get("max_steps")
                and isinstance(seal, dict)
                and set(seal) == {"schema_version", "stage_run_id", "thread_id", "status", "checkpoint_sha256", "state_fingerprint"}
                and seal.get("schema_version") == "voice-director-checkpoint-seal-v1"
                and seal.get("stage_run_id") == stage_run_id
                and seal.get("thread_id") == thread_id
                and seal.get("status") == "completed"
                and seal.get("checkpoint_sha256") == checkpoint_hash
                and isinstance(checkpoint_state, dict)
                and seal.get("state_fingerprint") == voice_director_state_fingerprint(checkpoint_state)
            )
            if valid:
                receipt_raw = json.dumps(receipt["output"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                context_raw = json.dumps(receipt_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                valid = (
                    receipt.get("output_fingerprint") == _hash_bytes(receipt_raw.encode("utf-8"))
                    and receipt.get("input_fingerprint") == _hash_bytes(context_raw.encode("utf-8"))
                    and receipt["output"] == [item.get("direction") for item in artifact.get("entries", [])]
                    and checkpoint_state.get("status") == "completed"
                    and checkpoint_state.get("stop_reason") == "completed"
                    and checkpoint_state.get("storyboard_ref") == storyboard_ref
                    and checkpoint_state.get("voice_script_ref") == voice_script_ref
                    and checkpoint_state.get("policy_ref") == policy_ref
                    and checkpoint_state.get("entries") == receipt_context.get("entries")
                    and checkpoint_state.get("policy") == receipt_context.get("policy")
                    and checkpoint_state.get("directives") == receipt["output"]
                    and checkpoint_state.get("model_calls") == budget.get("used_model_calls")
                    and checkpoint_state.get("steps") == budget.get("used_steps")
                    and checkpoint_state.get("max_model_calls") == budget.get("max_model_calls")
                    and checkpoint_state.get("max_steps") == budget.get("max_steps")
                    and checkpoint_state.get("agent_version") == receipt.get("agent_version")
                    and checkpoint_state.get("model_profile") == receipt.get("model_profile")
                    and checkpoint_state.get("call_receipts") == authority.get("call_receipts")
                    and authority.get("call_receipts") == [{
                        "call_index": str(receipt.get("call_index")),
                        "input_fingerprint": receipt.get("input_fingerprint"),
                        "output_fingerprint": receipt.get("output_fingerprint"),
                        "agent_version": receipt.get("agent_version"),
                        "model_profile": receipt.get("model_profile"),
                        "max_model_calls": str(receipt.get("max_model_calls")),
                        "max_steps": str(receipt.get("max_steps")),
                        "input_context": receipt_context,
                    }]
                )
        if valid:
            for item in artifact.get("entries", []):
                direction = item.get("direction") if isinstance(item, dict) else None
                valid = (
                    isinstance(item, dict)
                    and set(item) == {"sequence", "scene_id", "shot_id", "kind", "speaker", "text", "shot_duration_seconds", "shot_start_seconds", "direction"}
                    and isinstance(direction, dict)
                    and set(direction) == {"sequence", "emotion", "rate", "pitch", "volume", "pause_before_ms", "pause_after_ms", "voice_requirements"}
                    and direction.get("sequence") == item.get("sequence")
                    and isinstance(direction.get("emotion"), str)
                    and isinstance(direction.get("voice_requirements"), dict)
                    and all(isinstance(direction.get(field), (int, float)) and not isinstance(direction.get(field), bool) and math.isfinite(float(direction.get(field))) for field in ("rate", "pitch", "volume", "pause_before_ms", "pause_after_ms"))
                )
                if not valid:
                    break
        if valid and voice_script_path:
            try:
                script_bytes = _read_bytes(voice_script_path)
                script = json.loads(script_bytes.decode("utf-8"))
                expected_entries = _validate_script(script, storyboard_ref)
                valid = len(artifact["entries"]) == len(expected_entries) and all(
                    {key: item.get(key) for key in ("sequence", "scene_id", "shot_id", "kind", "speaker", "text", "shot_duration_seconds", "shot_start_seconds")} == cue
                    and isinstance(item.get("direction"), dict)
                    for item, cue in zip(artifact["entries"], expected_entries)
                )
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                valid = False
        if valid and policy_path:
            try:
                policy = json.loads(_read_bytes(policy_path).decode("utf-8"))
                valid = (
                    isinstance(policy, dict)
                    and _policy_ref(policy) == policy_ref
                    and isinstance(receipt, dict)
                    and isinstance(receipt.get("input_context"), dict)
                    and receipt["input_context"].get("policy") == policy
                )
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                valid = False
        if not valid:
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                               message="voice-director authority binding is invalid",
                               authority_path=authority_path,
                               authority_hash=sha256_file(authority_path))
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"logical_id": "voice_direction.main", "version_id": f"sha256:{sha256_file(artifact_path)}", "path": artifact_path},),
            authority_path=authority_path, authority_hash=sha256_file(authority_path),
            authority_files=({"path": authority_path, "sha256": sha256_file(authority_path)}, {"path": checkpoint_path, "sha256": checkpoint_hash}, {"path": checkpoint_seal_path, "sha256": sha256_file(checkpoint_seal_path)}, {"path": call_receipt_path, "sha256": sha256_file(call_receipt_path)}),
        )

    def execute(
        self, *, stage_run_id: str, output_dir: str, storyboard_path: str, storyboard_ref: dict[str, str],
        voice_script_path: str, voice_script_ref: dict[str, str], policy_path: str, policy_ref: dict[str, str],
        max_model_calls: int = 1, max_steps: int = 8,
    ) -> StageResult:
        try:
            storyboard_bytes = _read_bytes(storyboard_path)
            script_bytes = _read_bytes(voice_script_path)
            policy_bytes = _read_bytes(policy_path)
            if _hash_bytes(storyboard_bytes) != storyboard_ref["version_id"][7:] or _hash_bytes(script_bytes) != voice_script_ref["version_id"][7:] or _hash_bytes(policy_bytes) != policy_ref["version_id"][7:]:
                raise ValueError("voice-director input snapshot changed")
            storyboard = json.loads(storyboard_bytes.decode("utf-8"))
            script = json.loads(script_bytes.decode("utf-8"))
            policy = json.loads(policy_bytes.decode("utf-8"))
            if not isinstance(storyboard, dict) or not isinstance(policy, dict) or policy.get("schema_version") != VOICE_DIRECTOR_POLICY_VERSION:
                raise ValueError("voice-director policy is invalid")
            allowed_emotions = policy.get("allowed_emotions")
            if (
                not isinstance(allowed_emotions, list)
                or not allowed_emotions
                or not all(isinstance(value, str) for value in allowed_emotions)
                or policy.get("max_model_calls") != max_model_calls
                or policy.get("max_steps") != max_steps
            ):
                raise ValueError("voice-director policy constraints are invalid")
            entries = _validate_script(script, storyboard_ref)
            if policy_ref != _policy_ref(policy):
                raise ValueError("voice-director policy fingerprint is invalid")
            os.makedirs(output_dir, exist_ok=True)
            existing = self.inspect(
                stage_run_id=stage_run_id, output_dir=output_dir,
                storyboard_ref=storyboard_ref, voice_script_ref=voice_script_ref, policy_ref=policy_ref,
                voice_script_path=voice_script_path, policy_path=policy_path,
            )
            if existing is not None:
                return existing
            checkpoint_path = _safe_path(output_dir, self.checkpoint_name)
            initial = {
                "storyboard_ref": storyboard_ref, "voice_script_ref": voice_script_ref, "policy_ref": policy_ref,
                "entries": entries, "policy": policy, "model_calls": 0, "steps": 0,
                "max_model_calls": max_model_calls, "max_steps": max_steps, "status": "running", "call_receipts": [],
                "agent_version": VOICE_DIRECTOR_AGENT_VERSION, "model_profile": self.model_profile,
                "recovery_fingerprint": _hash_bytes(json.dumps({
                    "agent_version": VOICE_DIRECTOR_AGENT_VERSION, "model_profile": self.model_profile,
                    "storyboard": storyboard_ref, "voice_script": voice_script_ref, "policy": policy_ref,
                    "entries": entries, "policy_value": policy,
                    "max_model_calls": max_model_calls, "max_steps": max_steps,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
            }
            call_receipt_path = _safe_path(output_dir, self.call_receipt_name)
            state = run_voice_director(
                checkpoint_path=checkpoint_path,
                thread_id=f"{stage_run_id}-{policy_ref['version_id']}",
                initial=initial,
                model=self.model_port,
                reservation_path=call_receipt_path,
            )
            checkpoint_seal_path = _safe_path(output_dir, self.checkpoint_seal_name)
            checkpoint_hash = sha256_file(checkpoint_path)
            atomic_write_json(checkpoint_seal_path, {
                "schema_version": "voice-director-checkpoint-seal-v1",
                "stage_run_id": stage_run_id,
                "thread_id": f"{stage_run_id}-{policy_ref['version_id']}",
                "status": "completed",
                "checkpoint_sha256": checkpoint_hash,
                "state_fingerprint": voice_director_state_fingerprint(state),
            })
            directions = state.get("directives") or []
            output_entries = []
            for cue, direction in zip(entries, directions):
                output_entries.append({**cue, "direction": dict(direction)})
            value = {
                "schema_version": VOICE_DIRECTION_SCHEMA_VERSION,
                "storyboard": storyboard_ref, "voice_script": voice_script_ref, "policy": policy_ref,
                "entry_count": len(output_entries), "entries": output_entries,
                "agent": {"version": VOICE_DIRECTOR_AGENT_VERSION, "model_profile": self.model_profile},
            }
            artifact_path = _safe_path(output_dir, self.artifact_name)
            authority_path = _safe_path(output_dir, self.authority_name)
            atomic_write_json(artifact_path, value)
            authority = {
                "schema_version": "voice-director-run-v1", "stage_run_id": stage_run_id, "status": "completed",
                "adapter_contract_version": self.contract_version, "agent_version": VOICE_DIRECTOR_AGENT_VERSION,
                "inputs": {"storyboard": storyboard_ref, "voice_script": voice_script_ref, "policy": policy_ref},
                "artifact": {"path": self.artifact_name, "sha256": sha256_file(artifact_path), "schema_version": VOICE_DIRECTION_SCHEMA_VERSION},
                "checkpoint": {"path": self.checkpoint_name, "sha256": checkpoint_hash},
                "checkpoint_seal": {"path": self.checkpoint_seal_name, "sha256": sha256_file(checkpoint_seal_path)},
                "call_receipt": {"path": self.call_receipt_name, "sha256": sha256_file(call_receipt_path)},
                "budget": {"max_model_calls": max_model_calls, "max_steps": max_steps, "used_model_calls": state.get("model_calls", 0), "used_steps": state.get("steps", 0)},
                "call_receipts": list(state.get("call_receipts", [])),
            }
            atomic_write_json(authority_path, authority)
            return self.inspect(stage_run_id=stage_run_id, output_dir=output_dir, storyboard_ref=storyboard_ref, voice_script_ref=voice_script_ref, policy_ref=policy_ref, voice_script_path=voice_script_path, policy_path=policy_path) or StageResult(status="failed", stage_run_id=stage_run_id)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.VOICE_DIRECTOR_FAILED.value,
                               message=f"voice-director input or graph failed: {exc}")
