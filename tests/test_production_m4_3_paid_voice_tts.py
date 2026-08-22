"""M4.3 paid voice-tts acceptance: approval/grant/operation chain, budget and integrity.

All tests in this module use a deterministic MockTtsProvider so the whole
A/B/C/D matrix runs offline with zero billing.  Real-provider smoke tests live
in test_production_m4_3_real_tts.py and run only with SILICONFLOW_API_KEY set.
"""

from __future__ import annotations

import json
import os
import wave

import pytest

from manju.production.adapters.voice_tts import VoiceTTSStageAdapter
from manju.production.models import M4_2_DAG_VERSION, ProductionError, stages_for_dag
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.production.tts_providers import (
    MockTtsProvider,
    TTS_CURRENCY,
    TTS_MAX_TOTAL_AMOUNT_MINOR,
    estimate_tts_amount_minor,
)

from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter


KEY = b"m4-3-paid-voice-tts-test-key"


def _service(tmp_path, provider: MockTtsProvider | None = None, **init_kwargs):
    source = tmp_path / "source.txt"
    source.write_text("付费配音测试", encoding="utf-8")
    project = tmp_path / "project"
    provider = provider or MockTtsProvider()
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=True, voice_director_enabled=True,
        voice_tts_enabled=True,
        voice_tts_mode="paid_siliconflow",
        voice_tts_model_profile="siliconflow-cosyvoice2",
        voice_tts_maximum_amount=str(TTS_MAX_TOTAL_AMOUNT_MINOR),
        voice_tts_provider_profile="siliconflow-cosyvoice2",
        voice_tts_provider_request={"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "v-test", "response_format": "wav", "sample_rate": 16000},
        hmac_key_id="test-key",
        **init_kwargs,
    )
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(mode="paid_siliconflow", tts_provider=provider,
                                               provider_request={"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "v-test"},
                                               provider_profile="siliconflow-cosyvoice2"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider


def _advance_to_approval(service):
    """Advance through storyboard/voice_script/voice_director to voice-tts approval."""
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


def test_m43_full_paid_flow_publishes_audio_with_events(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert provider.calls >= 1
    events = service.store.events.read()
    calls = [event["event_type"] for event in events if event["event_type"].startswith("call_")]
    assert calls == ["call_reserved", "call_submitted", "call_settled"]
    assert len([e for e in events if e["event_type"] == "approval_requested"]) == 1
    assert len([e for e in events if e["event_type"] == "grant_issued"]) == 1
    terminal = _terminal_voice_tts_event(service, completed.run_id)
    assert terminal["event_type"] == "stage_completed"
    authority, output_dir = _authority_of(service, terminal)
    assert authority["mode"] == "paid_siliconflow"
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
    assert int(settled["usage"]["actual_amount"]) <= TTS_MAX_TOTAL_AMOUNT_MINOR
    assert settled["usage"]["currency"] == TTS_CURRENCY
    dto = service.get_voice_tts_status()
    assert dto["status"] == "completed" and dto["artifact"]["audio"]["media_type"] == "audio/wav"
    assert "path" not in json.dumps(dto, ensure_ascii=False)


def test_m43_unapproved_never_calls_provider(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    for _ in range(5):
        assert service.advance().status == "awaiting_approval"
    events = service.store.events.read()
    assert not any(event["event_type"].startswith("call_") for event in events)
    assert provider.calls == 0


def test_m43_rejected_approval_stops_run(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    request_id = "approval-" + awaiting.run_id.removeprefix("run_")
    rejected = service.decide_approval(request_id, decision="reject", reviewer="tester",
                                       expected_last_event_hash=awaiting.last_event_hash)
    assert rejected.status == "needs_review"
    assert provider.calls == 0


def test_m43_budget_exceeded_fails_closed(tmp_path):
    service, provider = _service(tmp_path, provider=MockTtsProvider(fail_with="BUDGET_EXCEEDED"))
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    blocked = service.run_until_blocked()
    assert blocked.status == "failed"
    terminal = _terminal_voice_tts_event(service, blocked.run_id)
    assert terminal["event_type"] == "stage_failed"
    assert provider.calls == 0


def test_m43_provider_error_fails_closed_without_audio(tmp_path):
    service, provider = _service(tmp_path, provider=MockTtsProvider(fail_with="VOICE_TTS_FAILED"))
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    blocked = service.run_until_blocked()
    assert blocked.status == "failed"
    terminal = _terminal_voice_tts_event(service, blocked.run_id)
    assert terminal["event_type"] == "stage_failed"
    assert provider.calls == 0
    assert not terminal["payload"].get("authority_path")
    output_dir = service.paths.voice_tts_dir(blocked.run_id, f"voice-tts-{blocked.run_id.removeprefix('run_')}")
    assert not os.path.isfile(os.path.join(output_dir, "voice_tts_run.json"))
    assert not os.path.isfile(os.path.join(output_dir, "voice_audio.wav"))


def test_m43_crash_after_reserve_submits_exactly_once(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    service.advance()  # call_reserved (no provider call yet)
    assert any(e["event_type"] == "call_reserved" for e in service.store.events.read())
    assert provider.calls == 0
    # simulate process restart with a fresh service over the same project
    service2, provider2 = _service_fresh_restart(tmp_path, provider)
    completed = service2.run_until_blocked()
    assert completed.status == "completed"
    calls_after = provider2.calls
    service2.advance()
    assert provider2.calls == calls_after  # no duplicate synthesis on re-entry


def test_m43_crash_submitted_unknown_is_not_retried(tmp_path):
    service, provider = _service(tmp_path, provider=MockTtsProvider(fail_with="OPERATION_OUTCOME_UNKNOWN"))
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    blocked = service.run_until_blocked()
    assert blocked.status == "blocked"
    events = service.store.events.read()
    assert any(e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("outcome") == "outcome_unknown" for e in events)
    calls_before = provider.calls
    # advance must not re-submit a provider call
    after_advance = service.advance()
    assert provider.calls == calls_before
    # human reconcile to a final outcome
    settled_op = next(
        e["payload"]["operation"] for e in events
        if e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("outcome") == "outcome_unknown"
    )
    from manju.production.operations import OperationRecord
    operation = OperationRecord.from_dict(settled_op)
    reconciled = operation.reconcile(outcome="failed", result_fingerprint="",
                                     usage={"actual_amount": "0", "currency": TTS_CURRENCY,
                                            "cost_status": "final", "cost_source": "provider_response"})
    service.reconcile_operation(reconciled, expected_last_event_hash=after_advance.last_event_hash)
    final = service.advance()
    assert final.status == "failed"


def test_m43_crash_after_audio_before_publish_reuses_audio(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    service.advance()  # call_reserved
    service.advance()  # call_submitted (audio + receipt now on disk)
    settled = service.advance()  # call_settled; manifest/authority not yet published
    events = service.store.events.read()
    assert any(e["event_type"] == "call_settled" for e in events)
    assert not any(e["event_type"] == "stage_completed" and (e.get("payload") or {}).get("stage") == "voice_tts" for e in events)
    calls_after_settle = provider.calls
    # simulate process restart: audio + receipt exist, manifest/authority absent
    service2, provider2 = _service_fresh_restart(tmp_path, provider)
    recovered = service2.run_until_blocked()
    assert recovered.status == "completed"
    assert provider2.calls == calls_after_settle  # no second synthesis on recovery


def test_m43_tamper_audio_fails_authority_check(tmp_path):
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
    service2, _ = _service_fresh_restart(tmp_path, provider)
    with pytest.raises(ProductionError):
        service2.advance()


def test_m43_tamper_manifest_fails_authority_check(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    terminal = _terminal_voice_tts_event(service, completed.run_id)
    authority, output_dir = _authority_of(service, terminal)
    manifest_path = os.path.join(output_dir, authority["artifact"]["path"])
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    manifest["entry_count"] = manifest["entry_count"] + 99
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    service2, _ = _service_fresh_restart(tmp_path, provider)
    with pytest.raises(ProductionError):
        service2.advance()


def test_m43_settled_reentry_is_idempotent(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    calls_before = provider.calls
    again = service.advance()
    assert again.status == "completed"
    assert provider.calls == calls_before


def _service_fresh_restart(tmp_path, provider):
    project = tmp_path / "project"
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(mode="paid_siliconflow", tts_provider=provider,
                                               provider_request={"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "v-test"},
                                               provider_profile="siliconflow-cosyvoice2"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, provider
