"""Paid TTS provider boundary: synchronous SiliconFlow transport with budget guard.

Design notes
------------
- The SiliconFlow /v1/audio/speech endpoint is synchronous: one HTTP request
  returns the finished WAV.  There is no durable upstream job to reconcile, so
  this provider deliberately declares no idempotency and no automatic crash
  recovery.  The adapter's durable local call-receipt state machine is the only
  authority that prevents blind re-submission (which would double-bill).
- Budget is enforced in minor units (1 元 = 100 分).  A single call is rejected
  before any provider side effect when its estimated price exceeds the per-call
  ceiling; the signed Grant maximum bounds the whole stage.
- Credentials come exclusively from the environment via ``api_key_env``.  They
  never enter project JSON, events, receipts, DTOs or audit exports.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import ProviderObservation


TTS_CURRENCY = "CNY"
# Budget ceilings in minor units (分): single call 0.5 元, stage total 10 元.
TTS_MAX_SINGLE_CALL_AMOUNT_MINOR = 50
TTS_MAX_TOTAL_AMOUNT_MINOR = 1000
# SiliconFlow CosyVoice2-0.5B price: ¥105 per 1M UTF-8 bytes →
# 0.000105 元/byte = 0.0105 分/byte.  Estimates are deliberately rounded UP.
TTS_PRICE_PER_BYTE_MINOR = 105 / 1_000_000
TTS_TARGET_SAMPLE_RATE = 16_000


def estimate_tts_amount_minor(text_bytes: int) -> int:
    """Conservative upper-bound estimate of one synthesis call in 分."""
    if text_bytes < 0:
        raise ValueError("text byte count must be non-negative")
    return math.ceil(text_bytes * TTS_PRICE_PER_BYTE_MINOR)


@runtime_checkable
class TtsProvider(Protocol):
    """Paid TTS transport; only the voice-tts adapter may invoke this interface."""

    def synthesize_cue(
        self, *, text: str, idempotency_key: str, request: dict[str, Any]
    ) -> bytes:
        """Synthesize one cue and return WAV bytes (16-bit PCM)."""
        ...


@dataclass
class SiliconFlowTtsProvider:
    """Synchronous SiliconFlow /v1/audio/speech transport.

    Every call is independently billed; no upstream job survives the HTTP
    response, so crash recovery must never blindly re-submit.
    """

    base_url: str = "https://api.siliconflow.cn/v1/audio/speech"
    api_key_env: str = "SILICONFLOW_API_KEY"
    model: str = "FunAudioLLM/CosyVoice2-0.5B"
    voice: str = "FunAudioLLM/CosyVoice2-0.5B:default"
    response_format: str = "wav"
    sample_rate: int = TTS_TARGET_SAMPLE_RATE
    timeout_seconds: float = 90.0
    max_single_call_amount_minor: int = TTS_MAX_SINGLE_CALL_AMOUNT_MINOR
    max_single_cue_text_bytes: int = 2000
    _environ: Mapping[str, str] | None = None

    def _api_key(self) -> str:
        source = os.environ if self._environ is None else self._environ
        key = source.get(self.api_key_env, "")
        if not isinstance(key, str) or not key:
            raise ProductionError(
                ReasonCode.HMAC_KEY_UNAVAILABLE.value,
                f"TTS provider credential {self.api_key_env} is unavailable",
            )
        return key

    def synthesize_cue(
        self, *, text: str, idempotency_key: str, request: dict[str, Any]
    ) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty TTS cue text")
        text_bytes = len(text.encode("utf-8"))
        if text_bytes > self.max_single_cue_text_bytes:
            raise ValueError("TTS cue text exceeds the provider single-call limit")
        amount = estimate_tts_amount_minor(text_bytes)
        if amount > self.max_single_call_amount_minor:
            raise ProductionError(
                ReasonCode.BUDGET_EXCEEDED.value,
                f"TTS single call estimate {amount} exceeds ceiling {self.max_single_call_amount_minor}",
            )
        model = request.get("model") or self.model
        voice = request.get("voice") or self.voice
        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": self.response_format,
            "sample_rate": self.sample_rate,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_obj = urllib.request.Request(
            self.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request_obj, timeout=self.timeout_seconds) as response:
                audio = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ProductionError(
                ReasonCode.VOICE_TTS_FAILED.value,
                f"TTS provider HTTP {exc.code}: {detail}",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # The request may or may not have been billed upstream.  The caller
            # must treat this as outcome-unknown and never blindly retry.
            raise ProductionError(
                ReasonCode.OPERATION_OUTCOME_UNKNOWN.value,
                "TTS provider transport failure: outcome unknown, do not retry blindly",
            ) from exc
        if not isinstance(audio, bytes) or len(audio) <= 44:
            raise ProductionError(
                ReasonCode.VOICE_TTS_FAILED.value,
                "TTS provider returned no usable audio",
            )
        return audio


@dataclass
class MockTtsProvider:
    """Deterministic offline provider for acceptance tests (never billed)."""

    model_profile: str = "mock-siliconflow-cosyvoice2"
    calls: int = 0
    max_single_call_amount_minor: int = TTS_MAX_SINGLE_CALL_AMOUNT_MINOR
    fail_with: str | None = None
    voices: list[str] = field(default_factory=list)

    def synthesize_cue(
        self, *, text: str, idempotency_key: str, request: dict[str, Any]
    ) -> bytes:
        if self.fail_with is not None:
            raise ProductionError(self.fail_with, f"mock provider fails with {self.fail_with}")
        self.calls += 1
        self.voices.append(str(request.get("voice", "")))
        amount = estimate_tts_amount_minor(len(text.encode("utf-8")))
        if amount > self.max_single_call_amount_minor:
            raise ProductionError(
                ReasonCode.BUDGET_EXCEEDED.value,
                f"TTS single call estimate {amount} exceeds ceiling {self.max_single_call_amount_minor}",
            )
        duration_ms = max(300, min(10_000, len(text) * 180))
        return _make_test_wav(duration_ms, sample_rate=TTS_TARGET_SAMPLE_RATE)


def _make_test_wav(duration_ms: int, *, sample_rate: int = TTS_TARGET_SAMPLE_RATE) -> bytes:
    """Deterministic non-silent 16-bit mono WAV (sine-ish ramp), for hashing."""
    frames = max(1, int(sample_rate * max(1, duration_ms) / 1000))
    payload = bytearray()
    for index in range(frames):
        value = (index * 7) % 65536 - 32768
        payload += value.to_bytes(2, "little", signed=True)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(bytes(payload))
    return stream.getvalue()


def _parse_riff_wav(data: bytes) -> tuple[int, int, int, bytes]:
    """Parse a RIFF WAVE payload tolerating overflowing size fields.

    SiliconFlow emits WAV files whose RIFF size field overflows (0xFFFFFFxx).
    The stdlib wave module then reports an absurd frame count, so we parse the
    chunk structure ourselves and clamp any oversized chunk to the file tail.
    Returns (frame_rate, channels, sampwidth, frame_bytes).
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("TTS provider audio is not a RIFF WAVE")
    total = len(data)
    pos = 12
    fmt: tuple[int, int, int] | None = None
    frames = bytearray()
    while pos + 8 <= total:
        chunk_id = data[pos:pos + 4]
        chunk_size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body_start = pos + 8
        body_end = body_start + chunk_size
        if body_end > total:
            body_end = total
        if chunk_id == b"fmt " and body_end - body_start >= 16:
            channels = int.from_bytes(data[body_start + 2:body_start + 4], "little")
            rate = int.from_bytes(data[body_start + 4:body_start + 8], "little")
            bits = int.from_bytes(data[body_start + 14:body_start + 16], "little")
            sampwidth = bits // 8
            fmt = (rate, channels, sampwidth)
        elif chunk_id == b"data":
            frames.extend(data[body_start:body_end])
        step = 8 + chunk_size + (chunk_size & 1)
        if step <= 0:
            break
        pos += step
    if fmt is None:
        raise ValueError("TTS provider WAV lacks a fmt chunk")
    if not frames:
        raise ValueError("TTS provider WAV lacks audio data")
    return fmt[0], fmt[1], fmt[2], bytes(frames)


def _wav_info(data: bytes) -> tuple[int, int, int]:
    """Return (frame_rate, channels, sampwidth) for a WAV payload."""
    rate, channels, sampwidth, _ = _parse_riff_wav(data)
    return rate, channels, sampwidth


def _wav_frames(data: bytes) -> bytes:
    _, _, _, frames = _parse_riff_wav(data)
    return frames


def fit_wav_to_duration(
    data: bytes, *, target_ms: int, sample_rate: int = TTS_TARGET_SAMPLE_RATE
) -> bytes:
    """Resample-to-target via zero-padding or truncation at the frame level.

    The authoritative M4.2 manifest aligns every cue to its shot duration, so a
    provider WAV shorter than the shot is padded with silence and one longer
    than the shot is truncated (a rare overlong cue is preserved as-is when
    already within the total cap by the caller).
    """
    frame_rate, channels, sampwidth, frames = _parse_riff_wav(data)
    if sampwidth != 2 or channels != 1:
        raise ValueError("TTS provider audio must be 16-bit mono")
    if frame_rate != sample_rate:
        # Keep it simple and deterministic: treat frame-rate mismatch as unsafe
        # for the fixed 16 kHz contract and reject rather than resample.
        raise ValueError("TTS provider sample rate must be 16000")
    frame_count = len(frames) // 2
    target_frames = int(round(sample_rate * max(1, target_ms) / 1000))
    if frame_count >= target_frames:
        kept = frames[: target_frames * 2]
    else:
        kept = frames + b"\x00\x00" * (target_frames - frame_count)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(kept)
    return stream.getvalue()
