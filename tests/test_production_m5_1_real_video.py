"""M5.1 real Agnes video acceptance (A/C/D against the live free API).

Requires AGNES_API_KEY in the environment.  Each test runs one short
(81-frame, ~3.4s) video generation; the free provider bills nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

from manju.production.adapters.video import VideoStageAdapter
from manju.production.adapters.video_prompt import VideoPromptStageAdapter
from manju.production.models import ProductionError
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.video_providers import AgnesVideoProvider, VIDEO_CURRENCY, VIDEO_MAX_TOTAL_AMOUNT_MINOR
from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGNES_API_KEY"),
    reason="AGNES_API_KEY not set; real provider test skipped",
)

KEY = b"m5-1-real-video-key"

REAL_REQUEST = {
    "model": "agnes-video-v2.0",
    "num_frames": 81,
    "frame_rate": 24,
    "response_format": "url",
}


def _real_provider():
    return AgnesVideoProvider(
        api_key=os.environ["AGNES_API_KEY"],
        num_frames=81,
        frame_rate=24,
        timeout_seconds=300,
    )


def _service(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("真实付费视频测试", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        video_prompt_enabled=True,
        video_enabled=True, video_mode="paid_agnes", video_model_profile="agnes-video-v1",
        video_maximum_amount=str(VIDEO_MAX_TOTAL_AMOUNT_MINOR),
        video_provider_profile="agnes-video",
        video_provider_request=dict(REAL_REQUEST),
        hmac_key_id="test-key",
    )
    provider = _real_provider()
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider, provider_request=dict(REAL_REQUEST),
                                        provider_profile="agnes-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider


def _advance_to_approval(service):
    awaiting = None
    for _ in range(30):
        awaiting = service.advance()
        if awaiting.status == "awaiting_approval":
            break
    assert awaiting is not None and awaiting.status == "awaiting_approval"
    return awaiting


def _approve_and_grant(service, awaiting):
    request_id = "video-approval-" + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="tester",
                                       expected_last_event_hash=awaiting.last_event_hash)
    granted = service.issue_grant(request_id, grant_id="grant-1", issued_by="tester",
                                  expected_last_event_hash=approved.last_event_hash)
    assert granted.status == "running"
    return granted


def _terminal_video_event(service, run_id):
    return next(
        event for event in service.store.events.read()
        if event.get("run_id") == run_id and event["event_type"] in {"stage_completed", "stage_failed"}
        and (event.get("payload") or {}).get("stage") == "video"
    )


def _authority_of(service, event):
    authority_path = service.store.artifact_path(event["payload"]["authority_path"])
    return json.loads(open(authority_path, encoding="utf-8").read()), os.path.dirname(authority_path)


def test_real_a_normal_call_produces_playable_video(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked(max_advances=60)
    assert completed.status == "completed"
    events = service.store.events.read()
    calls = [event["event_type"] for event in events if event["event_type"].startswith("call_")]
    assert calls == ["call_reserved", "call_submitted", "call_settled"]
    terminal = _terminal_video_event(service, completed.run_id)
    assert terminal["event_type"] == "stage_completed"
    authority, output_dir = _authority_of(service, terminal)
    video_path = os.path.join(output_dir, authority["artifact"]["path"])
    assert os.path.isfile(video_path) and os.path.getsize(video_path) > 1000
    # media probe: H.264 mp4, playable, non-zero duration (run via WSL ffprobe)
    wsl_path = video_path.replace("\\", "/")
    import re as _re
    wsl_path = _re.sub(r"^([A-Za-z]):", lambda m: f"/mnt/{m.group(1).lower()}", wsl_path)
    probe = subprocess.run(
        ["wsl.exe", "bash", "-lc",
         f"ffprobe -v error -show_entries format=duration -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 '{wsl_path}'"],
        capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert "codec_name=h264" in probe.stdout
    assert "width=" in probe.stdout and "height=" in probe.stdout
    duration = [line for line in probe.stdout.splitlines() if line.startswith("duration=")]
    assert duration and float(duration[0].split("=")[1]) > 0
    settled = next(
        e["payload"]["operation"] for e in events
        if e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("status") == "settled"
    )
    assert settled["outcome"] == "succeeded"
    assert settled["usage"]["actual_amount"] == "0"
    assert settled["usage"]["currency"] == VIDEO_CURRENCY
    assert settled["usage"]["cost_source"] == "provider_response"
    assert "AGNES_API_KEY" not in json.dumps(events, ensure_ascii=False)


def test_real_c_recovery_after_artifact_before_publish(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    service.advance()  # call_reserved
    service.advance()  # call_submitted (real provider job submitted)
    # poll until the video finishes and settles (pending observations keep the
    # operation submitted; each pending advance sleeps 15s)
    for _ in range(40):
        service.advance()
        events = service.store.events.read()
        if any(e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("status") == "settled" for e in events):
            break
    events = service.store.events.read()
    assert any(e["event_type"] == "call_settled" for e in events)
    assert not any(e["event_type"] == "stage_completed" and (e.get("payload") or {}).get("stage") == "video" for e in events)
    provider2 = _real_provider()
    service2 = ProductionService(
        str(tmp_path / "project" / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider2, provider_request=dict(REAL_REQUEST),
                                        provider_profile="agnes-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    recovered = service2.run_until_blocked()
    assert recovered.status == "completed"
    terminal = _terminal_video_event(service2, recovered.run_id)
    authority, output_dir = _authority_of(service2, terminal)
    assert authority["adapter_contract_version"] == "video-adapter-m5.1-v1"
    assert provider2.calls == 0  # recovery reuses persisted artifact; no new job


def test_real_d_tamper_video_fails_authority_check(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked(max_advances=60)
    assert completed.status == "completed"
    terminal = _terminal_video_event(service, completed.run_id)
    authority, output_dir = _authority_of(service, terminal)
    video_path = os.path.join(output_dir, authority["artifact"]["path"])
    with open(video_path, "rb") as handle:
        data = bytearray(handle.read())
    data[-10] ^= 0xFF
    with open(video_path, "wb") as handle:
        handle.write(data)
    with pytest.raises(ProductionError):
        service.advance()
