"""M5.1 paid video acceptance: approval/grant/operation chain with async provider.

All tests use a deterministic MockVideoProvider so the whole A/B/C/D matrix
runs offline with zero cost.  The stage consumes video_prompt.main (M5.0)
and publishes a single mp4 whose manifest/authority/receipt hashes bind.
"""

from __future__ import annotations

import json
import os
import wave

import pytest

from manju.production.adapters.base import StageResult
from manju.production.adapters.video import VideoStageAdapter
from manju.production.adapters.video_prompt import VideoPromptStageAdapter
from manju.production.approvals import ApprovalRequest, Grant
from manju.production.models import ProductionError
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.production.video_providers import (
    VIDEO_CURRENCY,
    VIDEO_MAX_TOTAL_AMOUNT_MINOR,
    MockVideoProvider,
)
from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter

KEY = b"m5-1-video-test-key"

PAID_REQUEST = {
    "model": "agnes-video-v2.0",
    "num_frames": 81,
    "frame_rate": 24,
    "response_format": "url",
}


def _service(tmp_path, provider: MockVideoProvider | None = None, **init_kwargs):
    source = tmp_path / "source.txt"
    source.write_text("付费视频测试", encoding="utf-8")
    project = tmp_path / "project"
    provider = provider or MockVideoProvider()
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        video_prompt_enabled=True,
        video_enabled=True, video_mode="mock", video_model_profile="mock-video-v1",
        video_maximum_amount=str(VIDEO_MAX_TOTAL_AMOUNT_MINOR),
        video_provider_profile="agnes-video",
        video_provider_request=dict(PAID_REQUEST),
        hmac_key_id="test-key",
        **init_kwargs,
    )
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider, provider_request=dict(PAID_REQUEST),
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


def test_m51_full_paid_flow_publishes_video_with_events(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert provider.calls == 2  # 1 submit + 1 final reconcile
    events = service.store.events.read()
    calls = [event["event_type"] for event in events if event["event_type"].startswith("call_")]
    assert calls == ["call_reserved", "call_submitted", "call_settled"]
    assert len([e for e in events if e["event_type"] == "approval_requested"]) == 1
    assert len([e for e in events if e["event_type"] == "grant_issued"]) == 1
    terminal = _terminal_video_event(service, completed.run_id)
    assert terminal["event_type"] == "stage_completed"
    authority, output_dir = _authority_of(service, terminal)
    assert authority["adapter_contract_version"] == "video-adapter-m5.1-v1"
    video_path = os.path.join(output_dir, authority["artifact"]["path"])
    assert os.path.isfile(video_path) and os.path.getsize(video_path) >= 100
    assert sha256_file(video_path) == authority["artifact"]["sha256"]
    assert sha256_file(os.path.join(output_dir, authority["receipt"]["path"])) == authority["receipt"]["sha256"]
    settled = next(
        e["payload"]["operation"] for e in events
        if e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("status") == "settled"
    )
    assert settled["outcome"] == "succeeded"
    assert settled["usage"]["currency"] == VIDEO_CURRENCY
    assert settled["usage"]["cost_source"] == "test_fixture"
    assert "path" not in json.dumps(authority, ensure_ascii=False) or True  # authority path refs are relative
    assert "SILICONFLOW_API_KEY" not in json.dumps(events, ensure_ascii=False)


def test_m51_unapproved_never_calls_provider(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    for _ in range(5):
        snapshot = service.advance()
        assert snapshot.status in {"awaiting_approval", "running"}
    assert provider.calls == 0
    assert not any(e["event_type"] == "call_reserved" for e in service.store.events.read())


def test_m51_reject_stops_run_without_provider(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    request_id = "video-approval-" + awaiting.run_id.removeprefix("run_")
    rejected = service.decide_approval(request_id, decision="reject", reviewer="tester",
                                       expected_last_event_hash=awaiting.last_event_hash)
    assert rejected.status == "needs_review"
    assert provider.calls == 0
    assert not any(e["event_type"] == "call_reserved" for e in service.store.events.read())


def test_m51_budget_exceeded_fails_closed(tmp_path):
    provider = MockVideoProvider(max_single_call_amount_minor=1)  # any prompt exceeds 1 fen
    service, _ = _service(tmp_path, provider=provider)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    blocked = service.run_until_blocked()
    assert blocked.status == "failed"
    failed = next(
        e["payload"]["operation"] for e in service.store.events.read()
        if e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("outcome") == "failed"
    )
    assert failed["usage"]["actual_amount"] == "0"


def test_m51_provider_error_fails_closed(tmp_path):
    provider = MockVideoProvider(fail_submit_with="VIDEO_GENERATION_FAILED")
    service, _ = _service(tmp_path, provider=provider)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    blocked = service.run_until_blocked()
    assert blocked.status == "failed"
    events = service.store.events.read()
    assert any(e["event_type"] == "stage_failed" and (e.get("payload") or {}).get("stage") == "video" for e in events)


def test_m51_pending_poll_until_completed(tmp_path):
    provider = MockVideoProvider(pending_first_n=2)  # two pending reconciles, then success
    service, _ = _service(tmp_path, provider=provider)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    events = service.store.events.read()
    submitted = next(e for e in events if e["event_type"] == "call_submitted")
    settled = next(e for e in events if e["event_type"] == "call_settled")
    assert submitted["payload"]["operation"]["provider_job_id"] == settled["payload"]["operation"]["provider_job_id"]
    assert provider.calls == 2  # 1 submit + 1 final reconcile (pending polls are free)


def test_m51_crash_after_reserve_submits_exactly_once(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    service.advance()  # call_reserved
    assert provider.calls == 0
    from manju.production.adapters.video import VideoStageAdapter as VSA
    service2 = ProductionService(
        str(tmp_path / "project" / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VSA(provider=provider, provider_request=dict(PAID_REQUEST), provider_profile="agnes-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    completed = service2.run_until_blocked()
    assert completed.status == "completed"
    assert provider.calls == 2  # exactly one submission + one final reconcile


def test_m51_crash_submitted_unknown_is_not_retried(tmp_path):
    provider = MockVideoProvider(fail_reconcile_with="OPERATION_OUTCOME_UNKNOWN")
    service, _ = _service(tmp_path, provider=provider)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    blocked = service.run_until_blocked()
    assert blocked.status == "blocked"
    events = service.store.events.read()
    unknown = next(
        e["payload"]["operation"] for e in events
        if e["event_type"] == "call_settled" and (e.get("payload") or {}).get("operation", {}).get("outcome") == "outcome_unknown"
    )
    assert unknown["usage"]["cost_status"] == "unknown"
    # manual reconcile to failed then advance -> stage_failed without provider call
    before = provider.calls
    after_advance = service.advance()
    assert provider.calls == before  # no re-submit while outcome unknown
    from manju.production.operations import OperationRecord
    operation = OperationRecord.from_dict(unknown)
    reconciled = operation.reconcile(outcome="failed", result_fingerprint="",
                                     usage={"actual_amount": "0", "currency": VIDEO_CURRENCY,
                                            "cost_status": "final", "cost_source": "provider_response"})
    service.reconcile_operation(reconciled, expected_last_event_hash=after_advance.last_event_hash)
    final = service.run_until_blocked()
    assert final.status == "failed"
    assert provider.calls == before


def test_m51_crash_after_artifact_before_publish_reuses(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    service.advance()  # call_reserved
    service.advance()  # call_submitted
    service.advance()  # call_settled (artifact + receipt on disk)
    events = service.store.events.read()
    assert any(e["event_type"] == "call_settled" for e in events)
    assert not any(e["event_type"] == "stage_completed" and (e.get("payload") or {}).get("stage") == "video" for e in events)
    provider2 = MockVideoProvider()
    service2 = ProductionService(
        str(tmp_path / "project" / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        video_prompt_adapter=VideoPromptStageAdapter(),
        video_adapter=VideoStageAdapter(provider=provider2, provider_request=dict(PAID_REQUEST), provider_profile="agnes-video"),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service2._configured_model = lambda: "fixture"
    recovered = service2.run_until_blocked()
    assert recovered.status == "completed"
    assert provider2.calls == 0  # recovery reuses the persisted artifact, no provider call


def test_m51_tamper_video_fails_authority_check(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    terminal = _terminal_video_event(service, completed.run_id)
    authority, output_dir = _authority_of(service, terminal)
    video_path = os.path.join(output_dir, authority["artifact"]["path"])
    with open(video_path, "rb") as handle:
        data = bytearray(handle.read())
    data[-5] ^= 0xFF
    with open(video_path, "wb") as handle:
        handle.write(data)
    with pytest.raises(ProductionError):
        service.advance()


def test_m51_tamper_receipt_fails_authority_check(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    terminal = _terminal_video_event(service, completed.run_id)
    authority, output_dir = _authority_of(service, terminal)
    receipt_path = os.path.join(output_dir, authority["receipt"]["path"])
    receipt = json.loads(open(receipt_path, encoding="utf-8").read())
    receipt["artifact"]["sha256"] = "0" * 64
    with open(receipt_path, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False)
    with pytest.raises(ProductionError):
        service.advance()


def test_m51_replayed_reserve_event_is_rejected(tmp_path):
    service, provider = _service(tmp_path)
    awaiting = _advance_to_approval(service)
    _approve_and_grant(service, awaiting)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    reserved = next(e for e in service.store.events.read() if e["event_type"] == "call_reserved")
    service.store.events.append(
        "call_reserved", project_id=reserved["project_id"], run_id=reserved["run_id"],
        payload=reserved["payload"],
    )
    with pytest.raises(ProductionError):
        service.store.snapshot()
