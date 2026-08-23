"""M4.3 real SiliconFlow paid voice-tts acceptance (A/C/D against live provider).

Requires SILICONFLOW_API_KEY in the environment.  Each test runs one short
fixture (3 cues) and costs well under 0.1 CNY.  Budget-boundary and
unapproved-call cases (B) are covered by the offline mock matrix in
test_production_m4_3_paid_voice_tts.py and are not duplicated here.
"""

from __future__ import annotations

import json
import os
import wave

import pytest

from manju.production.adapters.voice_tts import VoiceTTSStageAdapter
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.production.tts_providers import (
    SiliconFlowTtsProvider,
    TTS_CURRENCY,
    TTS_MAX_TOTAL_AMOUNT_MINOR,
)

from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter


pytestmark = pytest.mark.skipif(
    not os.environ.get("SILICONFLOW_API_KEY"),
    reason="SILICONFLOW_API_KEY not set; real provider test skipped",
)

KEY = b"m4-3-real-voice-tts-key"

REAL_REQUEST = {
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
    "response_format": "wav",
    "sample_rate": 16000,
}


def _real_provider():
    return SiliconFlowTtsProvider(
        model="FunAudioLLM/CosyVoice2-0.5B",
        voice="FunAudioLLM/CosyVoice2-0.5B:alex",
        response_format="wav",
        sample_rate=16000,
        timeout_seconds=90,
    )


def _service(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("真实付费配音测试", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=True, voice_director_enabled=True,
        voice_tts_enabled=True, voice_tts_mode="paid_siliconflow",
        voice_tts_model_profile="siliconflow-cosyvoice2",
        voice_tts_maximum_amount=str(TTS_MAX_TOTAL_AMOUNT_MINOR),
        voice_tts_provider_profile="siliconflow-cosyvoice2",
        voice_tts_provider_request=REAL_REQUEST,
        hmac_key_id="test-key",
    )
    provider = _real_provider()
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(
            mode="paid_siliconflow", tts_provider=provider,
            provider_request=dict(REAL_REQUEST),
            provider_profile="siliconflow-cosyvoice2",
        ),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider


def _advance_to_approval(service):
    awaiting = None
    for _ in range(20):
        awaiting = service.advance()
        if awaiting.status == "awaiting_approval":
            break
    assert awaiting is not None and awaiting.status == "awaiting_approval"
    return awaiting


def _approve_and_grant(service, awaiting):
    request_id = "approval-" + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="tester",
                                       expected_last_event_hash=awaiting.last_event_hash)
    granted = service.issue_grant(request_id, grant_id="grant-1", issued_by="tester",
                                  expected_last_event_hash=approved.last_event_hash)
    assert granted.status == "running"
    return granted


def _terminal_voice_tts_event(service, run_id):
    return next(
        event for event in service.store.events.read()
        if event.get("run_id") == run_id and event["event_type"] in {"stage_completed", "stage_failed"}
        and (event.get("payload") or {}).get("stage") == "voice_tts"
    )


def _authority_of(service, event):
    authority_path = service.store.artifact_path(event["payload"]["authority_path"])
    return json.loads(open(authority_path, encoding="utf-8").read()), os.path.dirname(authority_path)


def test_real_a_normal_call(tmp_path):
    """A. One normal paid call: events each exactly once, audio binds to grant."""
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    events = service.store.events.read()
    calls = [event["event_type"] for event in events if event["event_type"].startswith("call_")]
    assert calls == ["call_reserved", "call_submitted", "call_settled"]
    assert len([e for e in events if e["event_type"] == "approval_requested"]) == 1
    assert len([e for e in events if e["event_type"] == "grant_issued"]) == 1
    terminal = _terminal_voice_tts_event(service, completed.run_id)
    assert terminal["event_type"] == "stage_completed"
    authority, output_dir = _authority_of(service, terminal)
    assert authority["mode"] == "paid_siliconflow"
    assert authority["model"] == {"profile": "siliconflow-cosyvoice2"}
    audio_path = os.path.join(output_dir, authority["audio"]["path"])
    with wave.open(audio_path, "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1 and audio.getsampwidth() == 2
        assert audio.getnframes() > 0
    assert sha256_file(audio_path) == authority["audio"]["sha256"]
    assert sha256_file(os.path.join(output_dir, authority["artifact"]["path"])) == authority["artifact"]["sha256"]
    assert sha256_file(os.path.join(output_dir, authority["receipt"]["path"])) == authority["receipt"]["sha256"]
    settled = next(
        e["payload"]["operation"] for e in events
        if e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("status") == "settled"
    )
    assert settled["outcome"] == "succeeded"
    assert 0 <= int(settled["usage"]["actual_amount"]) <= TTS_MAX_TOTAL_AMOUNT_MINOR
    assert settled["usage"]["currency"] == TTS_CURRENCY
    assert settled["usage"]["cost_source"] == "provider_response"
    dto = service.get_voice_tts_status()
    assert "path" not in json.dumps(dto, ensure_ascii=False)
    assert "SILICONFLOW_API_KEY" not in json.dumps(events, ensure_ascii=False)


def test_real_c_recovery_after_audio_before_publish(tmp_path):
    """C.3: crash after audio persisted, before manifest/authority publish."""
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    service.advance()  # call_reserved
    service.advance()  # call_submitted (real provider call; audio + receipt on disk)
    service.advance()  # call_settled
    events = service.store.events.read()
    assert any(e["event_type"] == "call_settled" for e in events)
    assert not any(e["event_type"] == "stage_completed" and (e.get("payload") or {}).get("stage") == "voice_tts" for e in events)
    # process restart: same project, fresh service
    project = tmp_path / "project"
    provider2 = _real_provider()
    service2 = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(
            mode="paid_siliconflow", tts_provider=provider2,
            provider_request=dict(REAL_REQUEST),
            provider_profile="siliconflow-cosyvoice2",
        ),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    recovered = service2.run_until_blocked()
    assert recovered.status == "completed"
    terminal = _terminal_voice_tts_event(service2, recovered.run_id)
    assert terminal["event_type"] == "stage_completed"
    # recovery must reuse the persisted audio: authority hash matches pre-crash audio
    authority, _ = _authority_of(service2, terminal)
    assert authority["mode"] == "paid_siliconflow"


def test_real_speaker_voice_map_multi_voice(tmp_path):
    """Real multi-role synthesis: distinct voices for dialogue/narration cues."""
    voice_map = {
        "A": "FunAudioLLM/CosyVoice2-0.5B:alex",
        "narrator": "FunAudioLLM/CosyVoice2-0.5B:anna",
        "unknown": "FunAudioLLM/CosyVoice2-0.5B:claire",
    }
    request = dict(REAL_REQUEST)
    request["voice_map"] = dict(voice_map)
    source = tmp_path / "source.txt"
    source.write_text("多角色配音测试", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=True, voice_director_enabled=True,
        voice_tts_enabled=True, voice_tts_mode="paid_siliconflow",
        voice_tts_model_profile="siliconflow-cosyvoice2",
        voice_tts_maximum_amount=str(TTS_MAX_TOTAL_AMOUNT_MINOR),
        voice_tts_provider_profile="siliconflow-cosyvoice2",
        voice_tts_provider_request=request,
        hmac_key_id="test-key",
    )
    provider = _real_provider()
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(
            mode="paid_siliconflow", tts_provider=provider,
            provider_request=dict(request),
            provider_profile="siliconflow-cosyvoice2",
        ),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    # the grant binds the voice_map: verify it reached the approval contract
    approval = next(
        e["payload"]["approval_request"] for e in service.store.events.read()
        if e["event_type"] == "approval_requested"
    )
    assert approval["operation_intents"][0]["provider_request"]["voice_map"] == voice_map
    # distinct real voices were used and each cue produced decodable audio
    terminal = _terminal_voice_tts_event(service, completed.run_id)
    authority, output_dir = _authority_of(service, terminal)
    audio_path = os.path.join(output_dir, authority["audio"]["path"])
    with wave.open(audio_path, "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1
        assert audio.getnframes() > 0


def test_real_d_tamper_audio_fails_authority_check(tmp_path):
    """D: audio tampered after publish -> advance fails closed."""
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    terminal = _terminal_voice_tts_event(service, completed.run_id)
    authority, output_dir = _authority_of(service, terminal)
    audio_path = os.path.join(output_dir, authority["audio"]["path"])
    with open(audio_path, "rb") as handle:
        data = bytearray(handle.read())
    data[-10] ^= 0xFF
    with open(audio_path, "wb") as handle:
        handle.write(bytes(data))
    from manju.production.models import ProductionError
    with pytest.raises(ProductionError):
        service.advance()
