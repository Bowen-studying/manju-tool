"""Offline M4.2 TTS adapter and the paid (SiliconFlow) provider boundary."""

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
from manju.production.approvals import ApprovalRequest
from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import ProviderObservation
from manju.production.store import sha256_file
from manju.production.tts_providers import (
    TTS_CURRENCY,
    TTS_MAX_SINGLE_CALL_AMOUNT_MINOR,
    TTS_MAX_TOTAL_AMOUNT_MINOR,
    MockTtsProvider,
    SiliconFlowTtsProvider,
    TtsProvider,
    estimate_tts_amount_minor,
    fit_wav_to_duration,
)
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


class _ProviderBackedTtsModel:
    """Paid TTS model: one provider call per cue, aligned to shot duration.

    ``idempotency_supported`` is deliberately False: the synchronous provider
    has no durable job and no idempotency key, so the offline receipt state
    machine must fail closed (never blind re-submit) on an interrupted call.
    """

    def __init__(self, provider: TtsProvider, *, request: dict[str, Any], model_profile: str):
        self.provider = provider
        self.request = dict(request)
        self.model_profile = model_profile
        self.idempotency_supported = False
        self.calls = 0
        self.single_call_ceiling_minor = int(
            getattr(provider, "max_single_call_amount_minor", TTS_MAX_SINGLE_CALL_AMOUNT_MINOR)
        )
        self.total_ceiling_minor = TTS_MAX_TOTAL_AMOUNT_MINOR
        self._results: dict[str, bytes] = {}
        voice_map = request.get("voice_map")
        self.voice_map = dict(voice_map) if isinstance(voice_map, dict) else {}

    def _cue_voice(self, item: dict[str, Any]) -> str | None:
        speaker = str(item.get("speaker", ""))
        if speaker in self.voice_map:
            return str(self.voice_map[speaker])
        kind = str(item.get("kind", ""))
        if kind in self.voice_map:
            return str(self.voice_map[kind])
        return None

    def _estimate_entries(self, entries: list[dict[str, Any]]) -> int:
        total = 0
        for item in entries:
            total += estimate_tts_amount_minor(len(str(item.get("text", "")).encode("utf-8")))
        return total

    def synthesize(self, *, entries: list[dict[str, Any]], idempotency_key: str) -> bytes:
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        total_estimate = self._estimate_entries(entries)
        if total_estimate > self.total_ceiling_minor:
            raise ProductionError(
                ReasonCode.BUDGET_EXCEEDED.value,
                f"TTS stage estimate {total_estimate} exceeds ceiling {self.total_ceiling_minor}",
            )
        pieces: list[bytes] = []
        for item in entries:
            text = str(item.get("text", ""))
            if not text.strip():
                raise ValueError("TTS cue text is empty")
            amount = estimate_tts_amount_minor(len(text.encode("utf-8")))
            if amount > self.single_call_ceiling_minor:
                raise ProductionError(
                    ReasonCode.BUDGET_EXCEEDED.value,
                    f"TTS single call estimate {amount} exceeds ceiling {self.single_call_ceiling_minor}",
                )
            self.calls += 1
            cue_key = f"{idempotency_key}:{item['sequence']}"
            cue_request = dict(self.request)
            cue_voice = self._cue_voice(item)
            if cue_voice:
                cue_request["voice"] = cue_voice
            audio = self.provider.synthesize_cue(text=text, idempotency_key=cue_key, request=cue_request)
            if not isinstance(audio, bytes) or len(audio) <= 44:
                raise ValueError("TTS provider returned invalid audio")
            duration_ms = max(100, int(round(float(item.get("shot_duration_seconds", 0)) * 1000)))
            pieces.append(fit_wav_to_duration(audio, target_ms=duration_ms))
        result = b"".join(pieces)
        if len(result) <= 44:
            raise ValueError("TTS provider returned empty audio")
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
    """Audio stage in offline_mock mode, or a paid provider behind M2 approval/operation."""

    contract_version = "voice-tts-adapter-m4.2-v1"
    artifact_name = "voice_audio.json"
    audio_name = "voice_audio.wav"
    authority_name = "voice_tts_run.json"
    call_receipt_name = "voice_tts_call_receipt.json"

    def __init__(
        self,
        model_port: VoiceTTSModelPort | None = None,
        *,
        mode: str = "offline_mock",
        tts_provider: TtsProvider | None = None,
        provider_request: dict[str, Any] | None = None,
        provider_profile: str = "siliconflow-cosyvoice2",
    ):
        if mode not in {"offline_mock", "paid_siliconflow"}:
            raise ValueError("voice-tts mode must be offline_mock or paid_siliconflow")
        self.mode = mode
        self.provider_profile = provider_profile
        if mode == "offline_mock":
            candidate = model_port or DeterministicVoiceTTSModel()
            if type(candidate) is not DeterministicVoiceTTSModel:
                raise TypeError("M4.2 offline TTS accepts only DeterministicVoiceTTSModel")
            self.model_port = candidate
        else:
            if model_port is not None:
                raise TypeError("paid voice-tts must use tts_provider, not model_port")
            if tts_provider is None:
                raise ValueError("paid voice-tts requires a TtsProvider")
            if not isinstance(tts_provider, (SiliconFlowTtsProvider, MockTtsProvider)):
                if not isinstance(tts_provider, TtsProvider):
                    raise TypeError("paid voice-tts provider must satisfy TtsProvider")
            self.model_port = _ProviderBackedTtsModel(
                tts_provider,
                request=dict(provider_request or {}),
                model_profile=provider_profile,
            )
        self.provider_request = dict(provider_request or {})

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
            and authority.get("mode") == self.mode
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
            audio_hash, _ = self._synthesize_audio(
                stage_run_id=stage_run_id, output_dir=output_dir,
                voice_direction_ref=voice_direction_ref, entries=entries,
            )
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
                "mode": self.mode, "model": {"profile": self.model_profile},
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

    def _load_entries(
        self, *, voice_direction_path: str, voice_direction_ref: dict[str, str],
    ) -> list[dict[str, Any]]:
        try:
            with open(voice_direction_path, "rb") as handle:
                direction_bytes = handle.read()
        except OSError as exc:
            raise ValueError("voice-direction input is unavailable") from exc
        if len(direction_bytes) > MAX_VOICE_TTS_INPUT_BYTES:
            raise ValueError("voice-direction input exceeds the TTS limit")
        if _hash_bytes(direction_bytes) != voice_direction_ref["version_id"][7:]:
            raise ValueError("voice-direction input snapshot changed")
        try:
            value = json.loads(direction_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("voice-direction input is not valid JSON") from exc
        return _direction_entries(value, voice_direction_ref)

    def _synthesize_audio(
        self, *, stage_run_id: str, output_dir: str,
        voice_direction_ref: dict[str, str], entries: list[dict[str, Any]],
    ) -> tuple[str, int]:
        """Durable call-receipt state machine shared by offline and paid modes.

        Reserved -> completed transitions are the only permitted mutations.
        A provider model that declares ``idempotency_supported=False`` (all
        synchronous paid TTS) fails closed on an interrupted reservation so a
        crash can never trigger a blind, double-billing re-submission.
        """
        audio_path = _safe_path(output_dir, self.audio_name)
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
        return sha256_file(audio_path), os.path.getsize(audio_path)

    def _public_provider_request(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Only public generation controls enter the approval contract."""
        allowed = {"model", "voice", "response_format", "sample_rate", "voice_map"}
        request = {}
        for key in allowed:
            value = settings.get("provider_request", {}).get(key) if isinstance(settings.get("provider_request"), dict) else None
            if value is not None:
                request[key] = value
        for key, value in self.provider_request.items():
            if key in allowed:
                request[key] = value
        return request

    def estimate_entries_amount_minor(self, entries: list[dict[str, Any]]) -> int:
        estimator = getattr(self.model_port, "_estimate_entries", None)
        if callable(estimator):
            result = estimator(entries)
            if isinstance(result, (int, str)) and str(result).isdigit():
                return int(result)
        return 0

    def plan(
        self, *, project_id: str, run_id: str, stage_run_id: str, output_dir: str,
        voice_direction_path: str, voice_direction_ref: dict[str, str],
        settings: dict[str, Any],
    ) -> ApprovalRequest:
        """Build the signed M2 approval request for one paid voice-tts batch."""
        os.makedirs(output_dir, exist_ok=True)
        entries = self._load_entries(voice_direction_path=voice_direction_path, voice_direction_ref=voice_direction_ref)
        estimate = self.estimate_entries_amount_minor(entries)
        maximum = int(settings.get("maximum_amount", TTS_MAX_TOTAL_AMOUNT_MINOR))
        if estimate > maximum:
            raise ProductionError(
                ReasonCode.BUDGET_EXCEEDED.value,
                f"voice-tts estimate {estimate} exceeds approved budget {maximum}",
            )
        provider_request = self._public_provider_request(settings)
        request_fingerprint = ""
        if provider_request:
            encoded = json.dumps(provider_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            request_fingerprint = "sha256:" + hashlib.sha256(encoded).hexdigest()
        operation_id = "voice-tts-" + run_id.removeprefix("run_")
        artifact_versions = ({"artifact_id": "voice_direction.main", "version_id": voice_direction_ref["version_id"]},)
        input_material = json.dumps({
            "operation_id": operation_id,
            "artifacts": list(artifact_versions),
            "provider_request_fingerprint": request_fingerprint,
            "estimated_amount_minor": estimate,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ApprovalRequest(
            request_id="approval-" + run_id.removeprefix("run_"),
            project_id=project_id, run_id=run_id,
            stage="voice_tts", stage_run_id=stage_run_id, kind="paid_voice_tts_batch",
            state_fingerprint="sha256:" + hashlib.sha256(
                json.dumps(list(artifact_versions), sort_keys=True).encode("utf-8")
            ).hexdigest(),
            artifact_versions=artifact_versions,
            operation_intents=({
                "operation_id": operation_id,
                "input_fingerprint": "sha256:" + hashlib.sha256(input_material.encode("utf-8")).hexdigest(),
                "kind": "paid_voice_tts",
                **(dict(provider_request=provider_request, provider_request_fingerprint=request_fingerprint) if provider_request else {}),
            },),
            maximum_paid_calls=1,
            maximum_amount=str(maximum),
            currency=str(settings.get("currency", TTS_CURRENCY)),
            provider_profile=str(settings.get("provider_profile", self.provider_profile)),
            expires_at="2099-01-01T00:00:00Z",
        )

    def submit_operation(
        self, *, operation: dict[str, Any], stage_run_id: str, output_dir: str,
        voice_direction_path: str, voice_direction_ref: dict[str, str],
    ) -> str:
        """Synthesize the paid audio and return the local provider job id.

        The durable call receipt guarantees that an interrupted paid call is
        never blindly re-submitted: a reserved receipt on a synchronous provider
        raises, so the caller must settle the operation as outcome-unknown.
        Provider controls must exactly match the signed grant binding; any
        voice/model drift fails closed before a provider side effect.
        """
        bound_request = operation.get("provider_request")
        if not isinstance(bound_request, dict):
            raise ProductionError(
                ReasonCode.GRANT_CONTRACT_INVALID.value,
                "voice-tts grant binding lacks a provider request",
            )
        current_request = self._public_provider_request({"provider_request": self.provider_request})
        if current_request != bound_request:
            raise ProductionError(
                ReasonCode.GRANT_CONTRACT_INVALID.value,
                "voice-tts provider controls drift from the signed grant",
            )
        entries = self._load_entries(voice_direction_path=voice_direction_path, voice_direction_ref=voice_direction_ref)
        os.makedirs(output_dir, exist_ok=True)
        try:
            self._synthesize_audio(
                stage_run_id=stage_run_id, output_dir=output_dir,
                voice_direction_ref=voice_direction_ref, entries=entries,
            )
        except ProductionError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, RuntimeError, wave.Error) as exc:
            raise ProductionError(
                ReasonCode.VOICE_TTS_FAILED.value,
                f"voice-tts paid synthesis failed: {exc}",
            ) from exc
        return str(operation["operation_id"])

    def observe_operation(
        self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any],
        voice_direction_path: str, voice_direction_ref: dict[str, str],
    ) -> ProviderObservation:
        """Verify the durable local receipt and return a succeeded observation."""
        entries = self._load_entries(voice_direction_path=voice_direction_path, voice_direction_ref=voice_direction_ref)
        audio_path = _safe_path(output_dir, self.audio_name)
        receipt_path = _safe_path(output_dir, self.call_receipt_name)
        receipt = read_json(receipt_path)
        if not isinstance(receipt, dict) or receipt.get("status") != "completed" or not os.path.isfile(audio_path):
            raise ProductionError(
                ReasonCode.OPERATION_OUTCOME_UNKNOWN.value,
                "voice-tts paid receipt is not completed; outcome unknown",
            )
        output = receipt.get("output")
        audio_hash = sha256_file(audio_path)
        if (
            not isinstance(output, dict)
            or output.get("path") != self.audio_name
            or output.get("sha256") != audio_hash
            or output.get("size") != os.path.getsize(audio_path)
        ):
            raise ProductionError(
                ReasonCode.STAGE_INTEGRITY_FAILED.value,
                "voice-tts paid receipt does not match local audio",
            )
        with open(audio_path, "rb") as handle:
            audio_bytes = handle.read()
        estimate = self.estimate_entries_amount_minor(entries)
        usage = {
            "calls": str(getattr(self.model_port, "calls", 0)),
            "characters": str(sum(len(str(item.get("text", ""))) for item in entries)),
            "estimated_amount_minor": str(estimate),
            "actual_amount": str(estimate),
            "currency": TTS_CURRENCY,
            "cost_status": "final",
            "cost_source": "provider_response",
        }
        return ProviderObservation(
            outcome="succeeded",
            provider_job_id=str(operation.get("operation_id", "")),
            result_fingerprint="sha256:" + audio_hash,
            artifact_bytes=audio_bytes,
            artifact_media_type="audio/wav",
            actual_amount=str(estimate),
            currency=TTS_CURRENCY,
            usage=usage,
            cost_status="final",
            cost_source="provider_response",
        )

    def publish_result(
        self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any],
        voice_direction_path: str, voice_direction_ref: dict[str, str],
    ) -> StageResult:
        """Publish manifest and authority from the settled paid audio."""
        entries = self._load_entries(voice_direction_path=voice_direction_path, voice_direction_ref=voice_direction_ref)
        os.makedirs(output_dir, exist_ok=True)
        audio_path = _safe_path(output_dir, self.audio_name)
        artifact_path = _safe_path(output_dir, self.artifact_name)
        authority_path = _safe_path(output_dir, self.authority_name)
        receipt_path = _safe_path(output_dir, self.call_receipt_name)
        if not os.path.isfile(audio_path) or not os.path.isfile(receipt_path):
            return StageResult(status="failed", stage_run_id=stage_run_id,
                               reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                               message="voice-tts paid audio or receipt is missing")
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
                return StageResult(status="failed", stage_run_id=stage_run_id,
                                   reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                   message="voice-tts paid partial manifest is invalid")
        else:
            atomic_write_json(artifact_path, manifest)
        authority = {
            "schema_version": "voice-tts-run-v1", "stage_run_id": stage_run_id, "status": "completed",
            "adapter_contract_version": self.contract_version, "agent_version": VOICE_TTS_AGENT_VERSION,
            "mode": self.mode, "model": {"profile": self.model_profile},
            "inputs": {"voice_direction": voice_direction_ref},
            "artifact": {"path": self.artifact_name, "sha256": sha256_file(artifact_path), "schema_version": VOICE_AUDIO_SCHEMA_VERSION},
            "audio": {"path": self.audio_name, "sha256": audio_hash, "media_type": "audio/wav"},
            "receipt": {"path": self.call_receipt_name, "sha256": sha256_file(receipt_path)},
        }
        atomic_write_json(authority_path, authority)
        return self.inspect(
            stage_run_id=stage_run_id, output_dir=output_dir,
            voice_direction_ref=voice_direction_ref, voice_direction_path=voice_direction_path,
        ) or StageResult(status="failed", stage_run_id=stage_run_id,
                          reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                          message="voice-tts paid authority is invalid")
