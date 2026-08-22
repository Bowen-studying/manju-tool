"""Offline M4.2 TTS adapter and the boundary for a future paid provider."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import threading
import wave
from dataclasses import dataclass, field
from typing import Any, Protocol

from manju.production.adapters.base import StageResult
from manju.production.models import ReasonCode
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_bytes, atomic_write_json, read_json


VOICE_TTS_AGENT_VERSION = "voice-tts-agent-m4.2-v1"
VOICE_AUDIO_SCHEMA_VERSION = "voice-audio-v1"
MAX_VOICE_TTS_ENTRIES = 10_000
MAX_VOICE_TTS_CUE_DURATION_MS = 30 * 60 * 1000
MAX_VOICE_TTS_TOTAL_DURATION_MS = 30 * 60 * 1000
MAX_VOICE_TTS_AUDIO_BYTES = 16_000 * 2 * (MAX_VOICE_TTS_TOTAL_DURATION_MS // 1000) + 44
MAX_VOICE_TTS_INPUT_BYTES = 4 * 1024 * 1024
MAX_VOICE_TTS_TEXT_BYTES = 1 * 1024 * 1024
MAX_VOICE_TTS_DIRECTION_BYTES = 256 * 1024
_EXECUTION_LOCKS: dict[str, threading.Lock] = {}
_EXECUTION_LOCKS_GUARD = threading.Lock()


class VoiceTTSModelPort(Protocol):
    idempotency_supported: bool

    def synthesize(self, *, entries: list[dict[str, Any]], idempotency_key: str) -> bytes:
        """Return one WAV payload; retries with the key must be idempotent."""


def _wav_silence(duration_ms: int, *, sample_rate: int = 16_000) -> bytes:
    frames = max(1, int(sample_rate * max(1, duration_ms) / 1000))
    payload = b"\x00\x00" * frames
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(payload)
    return stream.getvalue()


@dataclass
class DeterministicVoiceTTSModel:
    """A local fake that emits a valid silent WAV and never reaches a provider."""

    calls: int = 0
    model_profile: str = "deterministic-fake-tts-v1"
    idempotency_supported: bool = True
    _results: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)

    def synthesize(self, *, entries: list[dict[str, Any]], idempotency_key: str) -> bytes:
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        self.calls += 1
        duration_ms = sum(max(100, int(round(float(item.get("shot_duration_seconds", 0)) * 1000))) for item in entries)
        result = _wav_silence(max(100, duration_ms))
        self._results[idempotency_key] = result
        return result


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(root: str, name: str) -> str:
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root:
        raise ValueError("voice-tts path escaped stage directory")
    return path


def _execution_lock(output_dir: str) -> threading.Lock:
    key = os.path.realpath(output_dir)
    with _EXECUTION_LOCKS_GUARD:
        return _EXECUTION_LOCKS.setdefault(key, threading.Lock())


def _idempotency_key(stage_run_id: str, voice_direction_ref: dict[str, str]) -> str:
    value = json.dumps({"stage_run_id": stage_run_id, "voice_direction": voice_direction_ref}, sort_keys=True, separators=(",", ":"))
    return _hash_bytes(value.encode("utf-8"))


def _validate_ref(value: Any, *, logical_id: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"logical_id", "version_id"}
        and value.get("logical_id") == logical_id
        and isinstance(value.get("version_id"), str)
        and len(value["version_id"]) == 71
        and value["version_id"].startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value["version_id"][7:])
    )


def _direction_entries(value: Any, expected_ref: dict[str, str]) -> list[dict[str, Any]]:
    if not _validate_ref(expected_ref, logical_id="voice_direction.main") or not isinstance(value, dict) or value.get("schema_version") != "voice-direction-v1":
        raise ValueError("voice-direction artifact schema is invalid")
    if value.get("voice_script") is None or not _validate_ref(value.get("voice_script"), logical_id="voice_script.main"):
        raise ValueError("voice-direction input binding is invalid")
    if value.get("entry_count") != len(value.get("entries") or []):
        raise ValueError("voice-direction artifact entries are invalid")
    if not isinstance(value.get("entries"), list):
        raise ValueError("voice-direction artifact entries are invalid")
    entries = value["entries"]
    if len(entries) > MAX_VOICE_TTS_ENTRIES:
        raise ValueError("voice-direction cue count exceeds the TTS limit")
    total_duration_ms = 0
    for expected_sequence, item in enumerate(entries, 1):
        if not isinstance(item, dict) or item.get("sequence") != expected_sequence or not isinstance(item.get("text"), str):
            raise ValueError("voice-direction cue sequence is invalid")
        if len(item["text"].encode("utf-8")) > MAX_VOICE_TTS_TEXT_BYTES:
            raise ValueError("voice-direction cue text exceeds the TTS limit")
        direction = item.get("direction")
        if not isinstance(direction, dict) or direction.get("sequence") != item.get("sequence"):
            raise ValueError("voice-direction cue binding is invalid")
        if len(json.dumps(direction, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_VOICE_TTS_DIRECTION_BYTES:
            raise ValueError("voice-direction cue direction exceeds the TTS limit")
        duration = item.get("shot_duration_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(float(duration)):
            raise ValueError("voice-direction cue timing is invalid")
        duration_value = float(duration)
        if duration_value < 0 or duration_value > MAX_VOICE_TTS_CUE_DURATION_MS / 1000:
            raise ValueError("voice-direction cue duration exceeds the TTS limit")
        duration_ms = int(round(duration_value * 1000))
        total_duration_ms += max(100, duration_ms)
        if total_duration_ms > MAX_VOICE_TTS_TOTAL_DURATION_MS:
            raise ValueError("voice-direction total duration exceeds the TTS limit")
    return entries


def _cue_manifest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offset = 0
    result = []
    for item in entries:
        duration_ms = max(100, int(round(float(item.get("shot_duration_seconds", 0)) * 1000)))
        result.append({
            "sequence": item["sequence"],
            "text_sha256": _hash_bytes(item["text"].encode("utf-8")),
            "offset_ms": offset,
            "duration_ms": duration_ms,
        })
        offset += duration_ms
    return result


class VoiceTTSStageAdapter:
    """Offline audio stage; a future provider must enter through M2 approval/operation."""

    contract_version = "voice-tts-adapter-m4.2-v1"
    artifact_name = "voice_audio.json"
    audio_name = "voice_audio.wav"
    authority_name = "voice_tts_run.json"
    call_receipt_name = "voice_tts_call_receipt.json"

    def __init__(self, model_port: VoiceTTSModelPort | None = None):
        candidate = model_port or DeterministicVoiceTTSModel()
        if type(candidate) is not DeterministicVoiceTTSModel:
            raise TypeError("M4.2 offline TTS accepts only DeterministicVoiceTTSModel")
        self.model_port = candidate

    @property
    def model_profile(self) -> str:
        declared = getattr(self.model_port, "model_profile", "")
        if isinstance(declared, str) and declared:
            return declared
        model_type = type(self.model_port)
        return f"{model_type.__module__}.{model_type.__qualname__}"

    def inspect(
        self, *, stage_run_id: str, output_dir: str,
        voice_direction_ref: dict[str, str], voice_direction_path: str,
    ) -> StageResult | None:
        import os

        artifact_path = _safe_path(output_dir, self.artifact_name)
        audio_path = _safe_path(output_dir, self.audio_name)
        authority_path = _safe_path(output_dir, self.authority_name)
        receipt_path = _safe_path(output_dir, self.call_receipt_name)
        call_key = _idempotency_key(stage_run_id, voice_direction_ref)
        artifact = read_json(artifact_path)
        authority = read_json(authority_path)
        if artifact is None and authority is None:
            return None
        if not isinstance(artifact, dict) or not isinstance(authority, dict):
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                               message="voice-tts artifact or authority is missing")
        audio_hash = sha256_file(audio_path) if os.path.isfile(audio_path) else ""
        valid = (
            set(authority) == {"schema_version", "stage_run_id", "status", "adapter_contract_version", "agent_version", "mode", "model", "inputs", "artifact", "audio", "receipt"}
            and authority.get("schema_version") == "voice-tts-run-v1"
            and authority.get("stage_run_id") == stage_run_id
            and authority.get("status") == "completed"
            and authority.get("adapter_contract_version") == self.contract_version
            and authority.get("agent_version") == VOICE_TTS_AGENT_VERSION
            and authority.get("mode") == "offline_mock"
            and authority.get("model") == {"profile": self.model_profile}
            and authority.get("inputs") == {"voice_direction": voice_direction_ref}
            and authority.get("artifact") == {"path": self.artifact_name, "sha256": sha256_file(artifact_path), "schema_version": VOICE_AUDIO_SCHEMA_VERSION}
            and authority.get("audio") == {"path": self.audio_name, "sha256": audio_hash, "media_type": "audio/wav"}
            and authority.get("receipt") == {"path": self.call_receipt_name, "sha256": sha256_file(receipt_path) if os.path.isfile(receipt_path) else ""}
            and set(artifact) == {"schema_version", "voice_direction", "entry_count", "audio", "receipt", "entries", "engine"}
            and artifact.get("schema_version") == VOICE_AUDIO_SCHEMA_VERSION
            and artifact.get("voice_direction") == voice_direction_ref
            and artifact.get("engine") == {"version": VOICE_TTS_AGENT_VERSION, "model_profile": self.model_profile}
            and isinstance(artifact.get("entries"), list)
            and artifact.get("entry_count") == len(artifact["entries"])
            and artifact.get("audio") == {"path": self.audio_name, "sha256": audio_hash, "media_type": "audio/wav"}
            and artifact.get("receipt") == {"path": self.call_receipt_name, "sha256": sha256_file(receipt_path) if os.path.isfile(receipt_path) else ""}
            and os.path.isfile(audio_path)
            and os.path.getsize(audio_path) > 44
            and os.path.getsize(audio_path) <= MAX_VOICE_TTS_AUDIO_BYTES
        )
        receipt = read_json(receipt_path)
        if valid:
            valid = (
                isinstance(receipt, dict)
                and set(receipt) == {"schema_version", "stage_run_id", "status", "inputs", "model", "idempotency_key", "output"}
                and receipt.get("schema_version") == "voice-tts-call-v1"
                and receipt.get("stage_run_id") == stage_run_id
                and receipt.get("status") == "completed"
                and receipt.get("inputs") == {"voice_direction": voice_direction_ref}
                and receipt.get("model") == {"profile": self.model_profile}
                and receipt.get("idempotency_key") == call_key
                and receipt.get("output") == {"path": self.audio_name, "sha256": audio_hash, "size": os.path.getsize(audio_path)}
            )
        if valid:
            try:
                with wave.open(audio_path, "rb") as audio:
                    frame_count = audio.getnframes()
                    frame_width = audio.getnchannels() * audio.getsampwidth()
                    frame_bytes = audio.readframes(frame_count)
                    valid = (
                        audio.getnchannels() == 1
                        and audio.getsampwidth() == 2
                        and audio.getframerate() == 16_000
                        and frame_count > 0
                        and frame_count <= MAX_VOICE_TTS_TOTAL_DURATION_MS * 16
                        and frame_width > 0
                        and len(frame_bytes) == frame_count * frame_width
                    )
            except (EOFError, OSError, wave.Error):
                valid = False
        if valid:
            try:
                valid = os.path.getsize(voice_direction_path) <= MAX_VOICE_TTS_INPUT_BYTES
                if valid:
                    with open(voice_direction_path, "rb") as handle:
                        valid = _hash_bytes(handle.read()) == voice_direction_ref["version_id"][7:]
            except OSError:
                valid = False
        if valid:
            try:
                expected = _cue_manifest(_direction_entries(read_json(voice_direction_path), voice_direction_ref))
                valid = artifact["entries"] == expected
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                valid = False
        if not valid:
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                               message="voice-tts authority binding is invalid",
                               authority_path=authority_path,
                               authority_hash=sha256_file(authority_path))
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"logical_id": "voice_audio.main", "version_id": f"sha256:{sha256_file(artifact_path)}", "path": artifact_path},),
            authority_path=authority_path, authority_hash=sha256_file(authority_path),
            authority_files=({"path": authority_path, "sha256": sha256_file(authority_path)}, {"path": artifact_path, "sha256": sha256_file(artifact_path)}, {"path": audio_path, "sha256": audio_hash}, {"path": receipt_path, "sha256": sha256_file(receipt_path)}),
        )

    def execute(
        self, *, stage_run_id: str, output_dir: str,
        voice_direction_path: str, voice_direction_ref: dict[str, str],
    ) -> StageResult:
        lock = _execution_lock(output_dir)
        lock.acquire()
        try:
            return self._execute_unlocked(
                stage_run_id=stage_run_id, output_dir=output_dir,
                voice_direction_path=voice_direction_path, voice_direction_ref=voice_direction_ref,
            )
        finally:
            lock.release()

    def _execute_unlocked(
        self, *, stage_run_id: str, output_dir: str,
        voice_direction_path: str, voice_direction_ref: dict[str, str],
    ) -> StageResult:
        import os

        try:
            with open(voice_direction_path, "rb") as handle:
                direction_bytes = handle.read()
            if len(direction_bytes) > MAX_VOICE_TTS_INPUT_BYTES:
                raise ValueError("voice-direction input exceeds the TTS limit")
            if _hash_bytes(direction_bytes) != voice_direction_ref["version_id"][7:]:
                raise ValueError("voice-direction input snapshot changed")
            value = json.loads(direction_bytes.decode("utf-8"))
            entries = _direction_entries(value, voice_direction_ref)
            os.makedirs(output_dir, exist_ok=True)
            existing = self.inspect(
                stage_run_id=stage_run_id, output_dir=output_dir,
                voice_direction_ref=voice_direction_ref, voice_direction_path=voice_direction_path,
            )
            if existing is not None:
                if existing.status == "completed":
                    return existing
                # An authority file is evidence of a completed publication attempt.
                # Never overwrite it after integrity failure; only recover the narrow
                # crash window where the manifest was published before authority.
                if os.path.isfile(_safe_path(output_dir, self.authority_name)):
                    return existing
            audio_path = _safe_path(output_dir, self.audio_name)
            artifact_path = _safe_path(output_dir, self.artifact_name)
            authority_path = _safe_path(output_dir, self.authority_name)
            receipt_path = _safe_path(output_dir, self.call_receipt_name)
            prior_receipt = read_json(receipt_path)
            expected_inputs = {"voice_direction": voice_direction_ref}
            expected_model = {"profile": self.model_profile}
            call_key = _idempotency_key(stage_run_id, voice_direction_ref)
            if os.path.isfile(audio_path) and prior_receipt is None:
                raise ValueError("orphaned voice-tts audio requires manual recovery")
            if prior_receipt is not None:
                if not isinstance(prior_receipt, dict) or set(prior_receipt) != {"schema_version", "stage_run_id", "status", "inputs", "model", "idempotency_key", "output"}:
                    raise ValueError("invalid voice-tts call reservation")
                if prior_receipt.get("schema_version") != "voice-tts-call-v1" or prior_receipt.get("stage_run_id") != stage_run_id or prior_receipt.get("inputs") != expected_inputs or prior_receipt.get("model") != expected_model or prior_receipt.get("idempotency_key") != call_key:
                    raise ValueError("voice-tts call receipt binding changed")
                if prior_receipt.get("status") == "reserved":
                    if not getattr(self.model_port, "idempotency_supported", False):
                        raise ValueError("voice-tts model does not support idempotent recovery")
                    audio_bytes = self.model_port.synthesize(entries=entries, idempotency_key=call_key)
                    if not isinstance(audio_bytes, bytes) or len(audio_bytes) <= 44:
                        raise ValueError("voice-tts model returned invalid audio")
                    if len(audio_bytes) > MAX_VOICE_TTS_AUDIO_BYTES:
                        raise ValueError("voice-tts model returned oversized audio")
                    atomic_write_bytes(audio_path, audio_bytes)
                    atomic_write_json(receipt_path, {
                        "schema_version": "voice-tts-call-v1", "stage_run_id": stage_run_id, "status": "completed",
                        "inputs": expected_inputs, "model": expected_model, "idempotency_key": call_key,
                        "output": {"path": self.audio_name, "sha256": sha256_file(audio_path), "size": os.path.getsize(audio_path)},
                    })
                elif prior_receipt.get("status") == "completed":
                    output = prior_receipt.get("output")
                    if not isinstance(output, dict) or output.get("path") != self.audio_name or not os.path.isfile(audio_path) or output.get("sha256") != sha256_file(audio_path) or output.get("size") != os.path.getsize(audio_path):
                        raise ValueError("voice-tts completed receipt is invalid")
                else:
                    raise ValueError("unresolved voice-tts call reservation")
            else:
                atomic_write_json(receipt_path, {
                    "schema_version": "voice-tts-call-v1", "stage_run_id": stage_run_id, "status": "reserved",
                    "inputs": expected_inputs, "model": expected_model, "idempotency_key": call_key,
                    "output": {"path": self.audio_name, "sha256": "", "size": 0},
                })
                if not getattr(self.model_port, "idempotency_supported", False):
                    raise ValueError("voice-tts model does not support idempotent calls")
                audio_bytes = self.model_port.synthesize(entries=entries, idempotency_key=call_key)
                if not isinstance(audio_bytes, bytes) or len(audio_bytes) <= 44:
                    raise ValueError("voice-tts model returned invalid audio")
                if len(audio_bytes) > MAX_VOICE_TTS_AUDIO_BYTES:
                    raise ValueError("voice-tts model returned oversized audio")
                atomic_write_bytes(audio_path, audio_bytes)
                atomic_write_json(receipt_path, {
                    "schema_version": "voice-tts-call-v1", "stage_run_id": stage_run_id, "status": "completed",
                    "inputs": expected_inputs, "model": expected_model, "idempotency_key": call_key,
                    "output": {"path": self.audio_name, "sha256": sha256_file(audio_path), "size": os.path.getsize(audio_path)},
                })
            audio_hash = sha256_file(audio_path)
            manifest = {
                "schema_version": VOICE_AUDIO_SCHEMA_VERSION,
                "voice_direction": voice_direction_ref,
                "entry_count": len(entries),
                "audio": {"path": self.audio_name, "sha256": audio_hash, "media_type": "audio/wav"},
                "receipt": {"path": self.call_receipt_name, "sha256": sha256_file(receipt_path)},
                "entries": _cue_manifest(entries),
                "engine": {"version": VOICE_TTS_AGENT_VERSION, "model_profile": self.model_profile},
            }
            if os.path.isfile(artifact_path):
                if read_json(artifact_path) != manifest:
                    raise ValueError("voice-tts partial manifest is invalid")
            else:
                atomic_write_json(artifact_path, manifest)
            authority = {
                "schema_version": "voice-tts-run-v1", "stage_run_id": stage_run_id, "status": "completed",
                "adapter_contract_version": self.contract_version, "agent_version": VOICE_TTS_AGENT_VERSION,
                "mode": "offline_mock", "model": {"profile": self.model_profile},
                "inputs": {"voice_direction": voice_direction_ref},
                "artifact": {"path": self.artifact_name, "sha256": sha256_file(artifact_path), "schema_version": VOICE_AUDIO_SCHEMA_VERSION},
                "audio": {"path": self.audio_name, "sha256": audio_hash, "media_type": "audio/wav"},
                "receipt": {"path": self.call_receipt_name, "sha256": sha256_file(receipt_path)},
            }
            atomic_write_json(authority_path, authority)
            return self.inspect(
                stage_run_id=stage_run_id, output_dir=output_dir,
                voice_direction_ref=voice_direction_ref, voice_direction_path=voice_direction_path,
            ) or StageResult(status="failed", stage_run_id=stage_run_id)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, RuntimeError, wave.Error) as exc:
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.VOICE_TTS_FAILED.value,
                               message="voice-tts input or fake synthesis failed")
