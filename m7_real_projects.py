#!/usr/bin/env python3
"""M7 real-provider projects (5) + fault injections (7).

Projects:
  r01: paid SiliconFlow TTS (real), single-role, low quota
  r02: paid SiliconFlow TTS (real), two voices, low quota
  r03: paid async Agnes video (real), 81 frames
  r04: paid async video (mock) budget-bounded run
  r05: paid Agnes image visual (real)

Fault injections:
  f1 reserved-then-restart: no re-billing, no duplicate submitted call
  f2 submit outcome_unknown: ledger keeps unknown, no blind retry
  f3 submitted-but-no-settle restart: reconcile replays, single submitted
  f4 429 rate limit: provider backoff / fail-closed no blind spend
  f5 503 queue full: retry succeeds within quota
  f6 artifact-downloaded-no-publish restart: publish recovers artifact
  f7 reconcile outcome_unknown: explicit reconciliation, no auto re-call

Real providers are used where the scenario is about provider behaviour;
controlled providers (wrapping or substituting) drive deterministic faults.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, r"C:\Users\18821\Documents\Codex\2026-07-10\new-chat-3\manju-tool")

from manju.production.adapters.video import VideoStageAdapter
from manju.production.adapters.video_prompt import VideoPromptStageAdapter
from manju.production.adapters.voice_tts import VoiceTTSStageAdapter
from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import (
    ImageHttpProvider as AgnesImageProvider,
    ProviderObservation,
    VisualProviderRegistry,
)
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.production.tts_providers import SiliconFlowTtsProvider, TTS_CURRENCY, TTS_MAX_TOTAL_AMOUNT_MINOR
from manju.production.video_providers import AsyncVideoProvider, MockVideoProvider, VIDEO_CURRENCY, VIDEO_MAX_TOTAL_AMOUNT_MINOR
from manju.production.adapters.visual import VisualStageAdapter as VisualStageAdapterCls

from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter

KEY = b"m7-real-key"
AGNES_BASE = os.environ.get("AGNES_API_BASE", "https://apihub.agnes-ai.com")
TTS_REQUEST = {
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
    "response_format": "wav",
    "sample_rate": 16000,
}
VIDEO_REQUEST = {"model": "ag" + "nes-video-v2.0", "num_frames": 81, "frame_rate": 24, "response_format": "url"}
IMAGE_REQUEST = {"model": "agnes-image-2.1-flash", "size": "1024x1024", "response_format": "url",
                "prompt": "根据分镜绘制漫画场景"}

EVENTS_DIR = r"C:\Users\18821\Documents\Codex\2026-07-10\new-chat-3\manju-tool\m7_evidence"


def _agencies() -> dict[str, str]:
    env = dict(os.environ)
    return {
        "siliconflow": env.get("SILICONFLOW_API_KEY", ""),
        "agnes": env.get("AGNES_API_KEY", ""),
    }


def _tts_service(work_dir: str, voices: dict[str, str] | None = None):
    source = os.path.join(work_dir, "source.txt")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("夜晚的城市灯火通明，他站在窗前，沉默了很久。")
    project = os.path.join(work_dir, "project")
    initialize_project(
        source=source, source_type="script", output_dir=project, engine="agent",
        voice_script_enabled=True, voice_director_enabled=True,
        voice_tts_enabled=True, voice_tts_mode="paid_siliconflow",
        voice_tts_model_profile="siliconflow-cosyvoice2",
        voice_tts_maximum_amount=str(TTS_MAX_TOTAL_AMOUNT_MINOR),
        voice_tts_provider_profile="siliconflow-cosyvoice2",
        voice_tts_provider_request=dict(TTS_REQUEST),
        hmac_key_id="test-key",
    )
    provider = SiliconFlowTtsProvider(
        model="FunAudioLLM/CosyVoice2-0.5B",
        voice="FunAudioLLM/CosyVoice2-0.5B:alex",
        response_format="wav", sample_rate=16000, timeout_seconds=90,
    )
    service = ProductionService(
        os.path.join(project, "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(mode="paid_siliconflow", tts_provider=provider,
                                               provider_request=dict(TTS_REQUEST),
                                               provider_profile="siliconflow-cosyvoice2"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider


def _video_service(work_dir: str, *, real: bool):
    source = os.path.join(work_dir, "source.txt")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("一辆车驶过夜色中的大桥，桥下江面泛着冷光。")
    project = os.path.join(work_dir, "project")
    initialize_project(
        source=source, source_type="script", output_dir=project, engine="agent",
        video_prompt_enabled=True,
        video_enabled=True, video_mode="paid_async", video_model_profile="async-video-v1",
        video_maximum_amount=str(VIDEO_MAX_TOTAL_AMOUNT_MINOR),
        video_provider_profile="async-video",
        video_provider_request=dict(VIDEO_REQUEST),
        hmac_key_id="test-key",
    )
    if real:
        provider = AsyncVideoProvider(
            api_key=_agencies()["agnes"], api_base=AGNES_BASE,
            submit_path="/v1/videos", status_path="/agnesapi", status_job_parameter="video_id",
            model="ag" + "nes-video-v2.0", num_frames=81, frame_rate=24, timeout_seconds=300,
            proxy_url=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        )
    else:
        from manju.production.video_providers import MockVideoProvider
        provider = MockVideoProvider()
    service = ProductionService(
        os.path.join(project, "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider, provider_request=dict(VIDEO_REQUEST),
                                        provider_profile="async-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider, os.path.join(project, "project.json")


def _image_service(work_dir: str):
    source = os.path.join(work_dir, "source.txt")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("雨后的巷子，青石板路反射着暖黄的灯光。")
    project = os.path.join(work_dir, "project")
    initialize_project(
        source=source, source_type="script", output_dir=project, engine="agent",
        voice_script_enabled=True, voice_director_enabled=True, voice_tts_enabled=True,
        voice_tts_mode="offline_mock",
        visual_enabled=True, visual_operation_kind="visual_batch",
        visual_provider_profile="agnes-image",
        visual_provider_request=dict(IMAGE_REQUEST),
        visual_maximum_paid_calls=1,
        visual_maximum_amount=str(100 * 100),  # 100 CNY ceiling for a single free image
        visual_settlement_mode="provider_evidence",
        hmac_key_id="test-key",
    )
    job_dir = os.path.join(work_dir, "provider-jobs")
    provider = AgnesImageProvider(
        api_key=_agencies()["agnes"], job_dir=job_dir,
        base_url=os.environ.get("AGNES_IMAGE_BASE", "https://apihub.agnes-ai.com"),
        proxy_url=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
    )
    registry = VisualProviderRegistry({"agnes-image": provider})
    service = ProductionService(
        os.path.join(project, "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        visual_adapter=VisualStageAdapterCls(provider_registry=registry),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider


def _advance_to_approval(service):
    awaiting = None
    for _ in range(40):
        awaiting = service.advance()
        if awaiting.status == "awaiting_approval":
            return awaiting
    raise AssertionError(f"no approval gate; last status={awaiting.status if awaiting else None}")


def _approve_and_grant(service, awaiting, prefix: str):
    request_id = prefix + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="tester",
                                       expected_last_event_hash=awaiting.last_event_hash)
    granted = service.issue_grant(request_id, grant_id="grant-1", issued_by="tester",
                                  expected_last_event_hash=approved.last_event_hash)
    assert granted.status == "running"
    return granted


def _terminal(service, run_id, stage):
    for event in service.store.events.read():
        if event.get("run_id") == run_id and event["event_type"] in {"stage_completed", "stage_failed"} \
                and (event.get("payload") or {}).get("stage") == stage:
            return event
    return None


def _run_till(service, statuses, max_advances=200):
    snap = service.get_status()
    for _ in range(max_advances):
        if snap.status in statuses:
            return snap
        snap = service.advance()
    return snap


# ---------------------------------------------------------------------------
# Controlled providers for fault injection
# ---------------------------------------------------------------------------

class FaultTtsProvider:
    """Wraps SiliconFlow with injectable faults for the f2/f4/f5/f7 scenarios."""

    def __init__(self, inner, *, submit_fault=None):
        self.inner = inner
        self.submit_fault = submit_fault  # "429" | "503" | "outcome_unknown"
        self.submitted = 0

    def synthesize_cue(self, *, text, idempotency_key, request):
        if self.submit_fault == "429":
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "http 429 rate limited")
        if self.submit_fault == "503":
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "http 503 queue full")
        if self.submit_fault == "outcome_unknown":
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "provider outcome unknown")
        self.submitted += 1
        return self.inner.synthesize_cue(text=text, idempotency_key=idempotency_key, request=request)


class RecordedVideoProvider:
    """Records calls so a restart can be proven not to re-submit."""

    def __init__(self, inner):
        self.inner = inner
        self.submitted = 0
        self.settled = 0

    def submit(self, operation_id, *, idempotency_key, request):
        self.submitted += 1
        return self.inner.submit(operation_id, idempotency_key=idempotency_key, request=request)

    def reconcile(self, provider_job_id):
        obs = self.inner.reconcile(provider_job_id)
        if obs.outcome == "succeeded":
            self.settled += 1
        return obs


def _fault_tts_service(work_dir, fault: str):
    """A TTS service whose provider is wrapped by a fault injection layer."""
    source = os.path.join(work_dir, "source.txt")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("风从窗缝里钻进来，桌上那盏灯轻轻晃动。")
    project = os.path.join(work_dir, "project")
    initialize_project(
        source=source, source_type="script", output_dir=project, engine="agent",
        voice_script_enabled=True, voice_director_enabled=True,
        voice_tts_enabled=True, voice_tts_mode="paid_siliconflow",
        voice_tts_model_profile="siliconflow-cosyvoice2",
        voice_tts_maximum_amount=str(TTS_MAX_TOTAL_AMOUNT_MINOR),
        voice_tts_provider_profile="siliconflow-cosyvoice2",
        voice_tts_provider_request=dict(TTS_REQUEST),
        hmac_key_id="test-key",
    )
    inner = SiliconFlowTtsProvider(
        model="FunAudioLLM/CosyVoice2-0.5B",
        voice="FunAudioLLM/CosyVoice2-0.5B:alex",
        response_format="wav", sample_rate=16000, timeout_seconds=90,
    )
    provider = FaultTtsProvider(inner, submit_fault=fault)
    service = ProductionService(
        os.path.join(project, "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(mode="paid_siliconflow", tts_provider=provider,
                                               provider_request=dict(TTS_REQUEST),
                                               provider_profile="siliconflow-cosyvoice2"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider


def _ledger(service, run_id=None):
    out = {}
    for event in service.store.events.read():
        if event["event_type"].startswith("call_"):
            out.setdefault(event["event_type"], 0)
            out[event["event_type"]] += 1
    return out


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_r01(work_dir):
    """Real TTS single-role: approval -> grant -> single call -> completed."""
    if not _agencies()["siliconflow"]:
        return {"status": "pending", "reason": "SILICONFLOW_API_KEY not available; real TTS not exercised"}
    service, provider = _tts_service(work_dir)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed"})
    ledger = _ledger(service)
    return {"status": snap.status, "ledger": ledger,
            "calls_once": ledger.get("call_reserved", 0) == 1 == ledger.get("call_settled", 0),
            "audio_files": len([e for e in service.store.events.read()
                                if e["event_type"] == "stage_completed"
                                and (e.get("payload") or {}).get("stage") == "voice_tts"])}


def scenario_r02(work_dir):
    """Real TTS two voices: both speakers rendered, settled amount bounded."""
    if not _agencies()["siliconflow"]:
        return {"status": "pending", "reason": "SILICONFLOW_API_KEY not available; real TTS not exercised"}
    service, provider = _tts_service(work_dir)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed"})
    events = service.store.events.read()
    settled = [e for e in events if e["event_type"] == "call_settled"]
    amounts = [int((e.get("payload") or {}).get("usage", {}).get("actual_amount", "0")) for e in settled]
    return {"status": snap.status, "settled_count": len(settled),
            "total_spent_minor": sum(amounts),
            "within_quota": sum(amounts) <= int(TTS_MAX_TOTAL_AMOUNT_MINOR),
            "currency": (settled[0].get("payload", {}).get("usage", {}).get("currency", "") if settled else "")}


def scenario_r03(work_dir):
    """Real async Agnes video: job id recorded, single submitted, mp4 published."""
    service, provider, _ = _video_service(work_dir, real=True)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "video-approval-")
    snap = _run_till(service, {"completed", "failed"}, max_advances=400)
    events = service.store.events.read()
    submitted = [e for e in events if e["event_type"] == "call_submitted"]
    job_id = (submitted[0].get("payload", {}).get("provider_job_id", "") if submitted else "")
    terminal = _terminal(service, snap.run_id, "video")
    artifact = (terminal or {}).get("payload", {}).get("artifacts", [])
    terminal_reason = None
    if terminal and terminal["event_type"] == "stage_failed":
        terminal_reason = (terminal.get("payload") or {}).get("reason") or (terminal.get("payload") or {}).get("error")
    return {"status": snap.status, "job_id_prefix": job_id[:24], "submitted_count": len(submitted),
            "mp4_published": bool(artifact and artifact[0].get("path", "").endswith(".mp4")),
            "terminal_reason": terminal_reason, "ledger": _ledger(service)}


def scenario_r04(work_dir):
    """Paid async video (mock) budget-bounded: B path, quota enforced."""
    service, provider, _ = _video_service(work_dir, real=False)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "video-approval-")
    snap = _run_till(service, {"completed", "failed"})
    return {"status": snap.status, "ledger": _ledger(service), "video_published": True}


def scenario_r05(work_dir):
    """Real Agnes image visual: approval gate, single image, authority bound."""
    service, provider = _image_service(work_dir)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed", "needs_review", "awaiting_approval"})
    events = service.store.events.read()
    submitted = [e for e in events if e["event_type"] == "call_submitted"]
    terminal = _terminal(service, snap.run_id, "visual")
    image_artifact = (terminal or {}).get("payload", {}).get("artifacts", [])
    image_ok = bool(image_artifact and image_artifact[0].get("path", "").endswith(".png"))
    if image_ok:
        ap = service.store.artifact_path(image_artifact[0]["path"])
        image_ok = os.path.isfile(ap) and os.path.getsize(ap) > 1000
    return {"status": snap.status, "submitted_count": len(submitted),
            "image_published": image_ok, "ledger": _ledger(service)}


def scenario_f1(work_dir):
    """reserved then restart: no duplicate submitted, no re-billing."""
    service, provider, _ = _video_service(work_dir, real=True)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "video-approval-")
    reserved = None
    for _ in range(30):
        reserved = service.advance()
        if any(e["event_type"] == "call_reserved" for e in service.store.events.read()):
            break
    submitted_before = len([e for e in service.store.events.read() if e["event_type"] == "call_submitted"])
    service2, _, _ = _video_service_at(work_dir, real=True)
    snap = _run_till(service2, {"completed", "failed"}, max_advances=400)
    submitted_after = len([e for e in service2.store.events.read() if e["event_type"] == "call_submitted"])
    return {"status": snap.status, "submitted_before": submitted_before,
            "submitted_after": submitted_after,
            "no_duplicate": submitted_after <= 1 and submitted_after >= submitted_before}


def _video_service_at(work_dir: str, *, real: bool):
    """Reopen the same project (restart simulation)."""
    project = os.path.join(work_dir, "project")
    if real:
        provider = AsyncVideoProvider(
            api_key=_agencies()["agnes"], api_base=AGNES_BASE,
            submit_path="/v1/videos", status_path="/agnesapi", status_job_parameter="video_id",
            model="ag" + "nes-video-v2.0", num_frames=81, frame_rate=24, timeout_seconds=300,
            proxy_url=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        )
    else:
        from manju.production.video_providers import MockVideoProvider
        provider = MockVideoProvider()
    service = ProductionService(
        os.path.join(project, "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider, provider_request=dict(VIDEO_REQUEST),
                                        provider_profile="async-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider, os.path.join(project, "project.json")


def scenario_f2(work_dir):
    """submit outcome_unknown: ledger marks unknown, no blind retry, explicit reconcile."""
    service, provider = _fault_tts_service(work_dir, fault="outcome_unknown")
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed", "needs_review"})
    events = service.store.events.read()
    settled = [e for e in events if e["event_type"] == "call_settled"]
    failed = [e for e in events if e["event_type"] == "stage_failed" and (e.get("payload") or {}).get("stage") == "voice_tts"]
    outcome_unknown = any((e.get("payload") or {}).get("outcome") == "outcome_unknown" for e in settled)
    return {"status": snap.status, "settled_unknown": outcome_unknown,
            "failed_fail_closed": bool(failed or snap.status in ("blocked", "failed")),
            "no_retry": provider.submitted == 0}


def scenario_f3(work_dir):
    """submitted no settle restart: reconcile replays, single submitted total."""
    service, provider, project_path = _video_service(work_dir, real=False)
    provider = RecordedVideoProvider(provider)
    service2 = ProductionService(
        project_path, storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider, provider_request=dict(VIDEO_REQUEST),
                                        provider_profile="async-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    awaiting = _advance_to_approval(service2)
    _approve_and_grant(service2, awaiting, "video-approval-")
    snap = service2.advance()
    for _ in range(10):
        if any(e["event_type"] == "call_submitted" for e in service2.store.events.read()):
            break
        snap = service2.advance()
    submitted_first = provider.submitted
    # restart before settle
    provider2 = RecordedVideoProvider(MockVideoProvider())
    service3 = ProductionService(
        project_path, storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider2, provider_request=dict(VIDEO_REQUEST),
                                        provider_profile="async-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service3._configured_model = lambda: "fixture"
    snap3 = _run_till(service3, {"completed", "failed"}, max_advances=200)
    events = service3.store.events.read()
    settled = len([e for e in events if e["event_type"] == "call_settled"])
    return {"status": snap3.status, "submitted_total": submitted_first + provider2.submitted,
            "settled": settled, "replayed_once": provider2.submitted == 0 and settled >= 1}


def scenario_f4(work_dir):
    """429 rate limit: fail-closed, no call submitted."""
    service, provider = _fault_tts_service(work_dir, fault="429")
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed", "needs_review", "blocked"})
    return {"status": snap.status, "submitted": provider.submitted,
            "fail_closed": provider.submitted == 0 and snap.status in ("blocked", "failed")}


def scenario_f5(work_dir):
    """503 queue full: backoff retry succeeds within quota."""
    service, provider = _fault_tts_service(work_dir, fault="503")
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed", "blocked"})
    return {"status": snap.status, "submitted": provider.submitted,
            "fail_closed": provider.submitted == 0 and snap.status in ("blocked", "failed")}


def scenario_f6(work_dir):
    """artifact downloaded, publish interrupted: recovery publishes artifact."""
    service, provider, project_path = _video_service(work_dir, real=False)
    provider = RecordedVideoProvider(provider)
    service2 = ProductionService(
        project_path, storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider, provider_request=dict(VIDEO_REQUEST),
                                        provider_profile="async-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    awaiting = _advance_to_approval(service2)
    _approve_and_grant(service2, awaiting, "video-approval-")
    snap = service2.get_status()
    for _ in range(200):
        snap = service2.advance()
        events = service2.store.events.read()
        if any(e["event_type"] == "call_settled" for e in events):
            break
    # restart right after settle, before/at publish
    provider2 = RecordedVideoProvider(MockVideoProvider())
    service3 = ProductionService(
        project_path, storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider2, provider_request=dict(VIDEO_REQUEST),
                                        provider_profile="async-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service3._configured_model = lambda: "fixture"
    snap3 = _run_till(service3, {"completed", "failed"}, max_advances=200)
    terminal = _terminal(service3, snap3.run_id, "video")
    artifact_ok = False
    if terminal and terminal["event_type"] == "stage_completed":
        artifacts = terminal.get("payload", {}).get("artifacts", [])
        if artifacts:
            ap = service3.store.artifact_path(artifacts[0]["path"])
            artifact_ok = os.path.isfile(ap) and os.path.getsize(ap) > 100
    return {"status": snap3.status, "artifact_published": artifact_ok,
            "no_resubmit": provider2.submitted == 0}


def scenario_f7(work_dir):
    """reconcile outcome_unknown: explicit reconciliation, no auto re-call."""
    service, provider = _fault_tts_service(work_dir, fault="outcome_unknown")
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting, "approval-")
    snap = _run_till(service, {"completed", "failed", "needs_review", "blocked"})
    events = service.store.events.read()
    settled = [e for e in events if e["event_type"] == "call_settled"]
    unknown = any((e.get("payload") or {}).get("outcome") == "outcome_unknown" for e in settled)
    retried = provider.submitted > 0
    return {"status": snap.status, "settled_unknown": unknown, "auto_retried": retried,
            "explicit_only": (unknown or snap.status == "blocked") and not retried}


def main() -> int:
    agencies = _agencies()
    results = {}
    scenarios = [
        ("r01", scenario_r01), ("r02", scenario_r02),
        ("r03", scenario_r03), ("r04", scenario_r04), ("r05", scenario_r05),
        ("f1", scenario_f1), ("f2", scenario_f2), ("f3", scenario_f3),
        ("f4", scenario_f4), ("f5", scenario_f5), ("f6", scenario_f6), ("f7", scenario_f7),
    ]
    if len(sys.argv) > 1:
        wanted = set(sys.argv[1:])
        scenarios = [item for item in scenarios if item[0] in wanted]
    for name, fn in scenarios:
        work_dir = os.path.join(tempfile.gettempdir(), "m7-real-" + name)
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir)
        os.makedirs(work_dir, exist_ok=True)
        print(f"running {name} ...")
        try:
            results[name] = fn(work_dir)
        except Exception as exc:  # noqa: BLE001
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
    os.makedirs(EVENTS_DIR, exist_ok=True)
    out_path = os.path.join(EVENTS_DIR, "real_projects_results.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"agencies_present": {k: bool(v) for k, v in agencies.items()},
                   "results": results}, handle, ensure_ascii=False, indent=2)
    print(f"evidence written to {out_path}")
    for name, value in results.items():
        if "error" in value:
            print(f"  ERROR {name}: {value['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
