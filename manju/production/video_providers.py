"""Paid video provider boundary for M5.1 (Agnes async text/image-to-video).

The provider is asynchronous: submit returns a video_id (task_xxx), the
operation is polled with reconcile(), and the finished mp4 is downloaded
with download().  Agnes is free (RPM <= 20) so real settled cost is 0, but
the budget guard and cost ledger are still exercised exactly as for paid
providers so the audit trail is uniform.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from manju.production.models import ProductionError, ReasonCode

VIDEO_PROVIDER_BASE = "https://apihub.agnes-ai.com/v1"
VIDEO_OUTPUT_BASE = "https://platform-outputs.agnes-ai.space/videos"
VIDEO_CURRENCY = "CNY"
VIDEO_MAX_TOTAL_AMOUNT_MINOR = 10_00  # 10.00 CNY symbolic ceiling (provider is free)
VIDEO_MAX_SINGLE_CALL_AMOUNT_MINOR = 1_00  # 1.00 CNY symbolic per-call ceiling

VIDEO_ALLOWED_FRAMES = {81, 121, 161, 241, 441}
VIDEO_DEFAULT_FRAMES = 81
VIDEO_DEFAULT_FRAME_RATE = 24
VIDEO_MEDIA_TYPE = "video/mp4"

_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""


def _https_opener() -> urllib.request.OpenerDirector:
    handlers = []
    if _PROXY:
        handlers.append(urllib.request.ProxyHandler({"https": _PROXY, "http": _PROXY}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler())
    return urllib.request.build_opener(*handlers)


def _estimate_video_amount_minor(prompt_chars: int) -> int:
    """Symbolic estimate for a free provider: prompt length maps to a tiny fee.

    Agnes bills nothing; the estimate keeps the budget guard real so the
    audit trail and fail-closed paths are exercised identically to paid
    providers.  A 1_000-char prompt => 1 fen (0.01 CNY).
    """
    return max(1, (prompt_chars + 99) // 100)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ProviderObservation:
    outcome: str
    provider_job_id: str
    result_fingerprint: str
    artifact_bytes: bytes
    artifact_media_type: str
    actual_amount: str
    currency: str
    usage: dict[str, str]
    cost_status: str
    cost_source: str

    @property
    def settled_usage(self) -> dict[str, str]:
        return {
            "actual_amount": self.actual_amount,
            "currency": self.currency,
            "cost_status": self.cost_status,
            "cost_source": self.cost_source,
        }


class AgnesVideoProvider:
    """Asynchronous video generation through the Agnes free API."""

    capabilities = {"automatic_recovery_safe": True}

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "agnes-video-v2.0",
        num_frames: int = VIDEO_DEFAULT_FRAMES,
        frame_rate: int = VIDEO_DEFAULT_FRAME_RATE,
        width: int = 1152,
        height: int = 768,
        timeout_seconds: int = 300,
    ) -> None:
        if num_frames not in VIDEO_ALLOWED_FRAMES:
            raise ValueError(f"agnes video num_frames must be one of {sorted(VIDEO_ALLOWED_FRAMES)}")
        if not 1 <= frame_rate <= 60:
            raise ValueError("agnes video frame_rate must be 1..60")
        self.api_key = api_key
        self.model = model
        self.num_frames = num_frames
        self.frame_rate = frame_rate
        self.width = width
        self.height = height
        self.timeout_seconds = timeout_seconds
        self.calls = 0
        self.last_request: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def submit(self, operation_id: str, *, idempotency_key: str = "", request: dict[str, Any] | None = None) -> str:
        """Submit a text-to-video (or image-to-video) job and return the video_id.

        The free service reports a full video queue (503 video_queue_full)
        frequently; retry with a short backoff so the paid chain is not
        spuriously settled as failed by a transient queue condition.
        """
        request = request or {}
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "video prompt is required")
        payload = {
            "model": request.get("model", self.model),
            "prompt": prompt,
            "num_frames": int(request.get("num_frames", self.num_frames)),
            "frame_rate": int(request.get("frame_rate", self.frame_rate)),
            "width": int(request.get("width", self.width)),
            "height": int(request.get("height", self.height)),
            "response_format": "url",
        }
        image = request.get("image")
        if isinstance(image, str) and image:
            payload["image"] = image
        self.last_request = dict(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{VIDEO_PROVIDER_BASE}/videos"
        for attempt in range(4):
            try:
                response = _https_opener().open(
                    urllib.request.Request(url, data=body, headers=self._headers(), method="POST"),
                    timeout=self.timeout_seconds,
                )
                raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if "video_queue_full" in detail and attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                if exc.code == 429 and attempt < 3:
                    # video generation is limited to 2 requests per minute
                    time.sleep(35 * (attempt + 1))
                    continue
                raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, f"video submit failed {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, f"video submit network error: {exc}") from exc
        else:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video queue remained full after retries")
        self.calls += 1
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video submit response is not json") from exc
        video_id = data.get("video_id") or data.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, f"video submit lacks a job id: {raw[:200]}")
        return video_id

    def reconcile(self, provider_job_id: str) -> ProviderObservation:
        """Poll one video job; return completed bytes or a pending/failed observation."""
        query = urllib.parse.urlencode({"video_id": provider_job_id})
        url = f"{VIDEO_PROVIDER_BASE}/agnesapi?{query}"
        for attempt in range(4):
            try:
                response = _https_opener().open(
                    urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}"}),
                    timeout=self.timeout_seconds,
                )
                raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, f"video reconcile failed {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, f"video reconcile network error: {exc}") from exc
        else:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video reconcile rate limited after retries")
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video reconcile response is not json") from exc
        status = str(data.get("status", ""))
        url_value = data.get("url") or data.get("video_url")
        if status == "completed" and isinstance(url_value, str) and url_value:
            return self._download_observation(provider_job_id, url_value, data)
        if status in {"failed", "error", "cancelled"}:
            return ProviderObservation(
                "failed", provider_job_id, "", b"", VIDEO_MEDIA_TYPE,
                "0", VIDEO_CURRENCY, {"job_status": status}, "final", "provider_response",
            )
        return ProviderObservation(
            "pending", provider_job_id, "", b"", VIDEO_MEDIA_TYPE,
            "0", VIDEO_CURRENCY, {"job_status": status}, "provisional", "provider_response",
        )

    def _download_observation(self, provider_job_id: str, video_url: str, meta: dict[str, Any]) -> ProviderObservation:
        try:
            response = _https_opener().open(
                urllib.request.Request(video_url, headers={"User-Agent": "Agnes-Client/1.0"}),
                timeout=self.timeout_seconds,
            )
            data = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, f"video download failed: {exc}") from exc
        if not data or len(data) < 100:
            raise ProductionError(ReasonCode.VIDEO_GENERATION_FAILED.value, "video download is empty")
        self.calls += 1
        return ProviderObservation(
            "succeeded", provider_job_id, "sha256:" + _sha256(data), data, VIDEO_MEDIA_TYPE,
            "0", VIDEO_CURRENCY, {"frames": str(meta.get("num_frames", "")), "resolution": str(meta.get("resolution", ""))},
            "final", "provider_response",
        )

    def download(self, provider_job_id: str, video_url: str) -> bytes:
        """Compatibility helper for explicit downloads outside the ledger flow."""
        return self._download_observation(provider_job_id, video_url, {}).artifact_bytes


def _fake_mp4(seed: str) -> bytes:
    """Deterministic tiny mp4-like payload for the offline mock provider."""
    header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42mp41"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return header + digest * 16


@dataclass
class MockVideoProvider:
    """Deterministic offline provider for acceptance tests (never billed)."""

    calls: int = 0
    fail_submit_with: str | None = None
    fail_reconcile_with: str | None = None
    pending_first_n: int = 0
    max_single_call_amount_minor: int = VIDEO_MAX_SINGLE_CALL_AMOUNT_MINOR
    submitted_requests: list[dict[str, Any]] = field(default_factory=list)

    def submit(self, operation_id: str, *, idempotency_key: str = "", request: dict[str, Any] | None = None) -> str:
        request = request or {}
        if self.fail_submit_with is not None:
            raise ProductionError(self.fail_submit_with, f"mock video provider fails submit with {self.fail_submit_with}")
        amount = _estimate_video_amount_minor(len(str(request.get("prompt", ""))))
        if amount > self.max_single_call_amount_minor:
            raise ProductionError(
                ReasonCode.BUDGET_EXCEEDED.value,
                f"video single call estimate {amount} exceeds ceiling {self.max_single_call_amount_minor}",
            )
        self.calls += 1
        self.submitted_requests.append(dict(request))
        return f"mock-video-{operation_id}-{self.calls}"

    def reconcile(self, provider_job_id: str) -> ProviderObservation:
        if self.fail_reconcile_with is not None:
            raise ProductionError(self.fail_reconcile_with, f"mock video provider fails reconcile with {self.fail_reconcile_with}")
        if self.pending_first_n > 0:
            self.pending_first_n -= 1
            return ProviderObservation(
                "pending", provider_job_id, "", b"", VIDEO_MEDIA_TYPE,
                "0", VIDEO_CURRENCY, {"job_status": "pending"}, "provisional", "test_fixture",
            )
        self.calls += 1
        data = _fake_mp4(provider_job_id)
        return ProviderObservation(
            "succeeded", provider_job_id, "sha256:" + _sha256(data), data, VIDEO_MEDIA_TYPE,
            "0", VIDEO_CURRENCY, {"frames": str(VIDEO_DEFAULT_FRAMES), "resolution": "720p"},
            "final", "test_fixture",
        )
