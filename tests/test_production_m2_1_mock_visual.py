import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import manju.production.service as production_service
from manju.production.adapters.base import StageResult
from manju.production.adapters.visual import MockVisualProvider, VisualStageAdapter
from manju.production.models import ProductionError, ReasonCode, canonical_json
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.utils.runtime import atomic_write_json
from manju.utils.runtime import read_json


class FixtureStoryboardAdapter:
    contract_version = "fixture-storyboard-m2-v1"

    def execute(self, *, stage_run_id, output_dir, **_kwargs):
        os.makedirs(output_dir, exist_ok=True)
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        atomic_write_json(artifact, {"schema_version": "1", "stage_run_id": stage_run_id})
        artifact_hash = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
        atomic_write_json(authority, {"schema_version": "1", "stage_run_id": stage_run_id, "artifact_sha256": artifact_hash})
        authority_hash = hashlib.sha256(Path(authority).read_bytes()).hexdigest()
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"path": artifact, "version_id": "sha256:" + artifact_hash},),
            authority_path=authority, authority_hash=authority_hash,
            authority_files=({"path": authority, "sha256": authority_hash},),
        )

    def inspect(self, *, stage_run_id, output_dir, **_kwargs):
        authority = os.path.join(output_dir, "authority.json")
        artifact = os.path.join(output_dir, "storyboard.json")
        if not os.path.isfile(authority) or not os.path.isfile(artifact):
            return None
        return self.execute(stage_run_id=stage_run_id, output_dir=output_dir)


RecordingMockProvider = MockVisualProvider


def _granted_service(tmp_path, *, outcome="succeeded"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.txt"
    source.write_text("mock visual source", encoding="utf-8")
    project_dir = tmp_path / "project"
    initialize_project(source=str(source), source_type="script", output_dir=str(project_dir), engine="agent",
                       visual_enabled=True, visual_maximum_paid_calls=1, visual_maximum_amount="0", hmac_key_id="test-key")
    provider = RecordingMockProvider(default_outcome=outcome)
    service = ProductionService(str(project_dir / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
                                visual_adapter=VisualStageAdapter(provider=provider),
                                hmac_key_provider=MappingHmacKeyProvider({"test-key": b"offline-only-key"}))
    service._configured_model = lambda: "mock-model"
    service.advance()
    service.advance()
    awaiting = service.advance()
    request_id = "approval-" + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="tester", expected_last_event_hash=awaiting.last_event_hash)
    service.issue_grant(request_id, grant_id="grant-1", issued_by="tester", expected_last_event_hash=approved.last_event_hash)
    return service, provider


def test_m2_mock_visual_approval_grant_and_completion_without_provider(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("mock visual source", encoding="utf-8")
    project_dir = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project_dir), engine="agent",
        visual_enabled=True, visual_maximum_paid_calls=1, visual_maximum_amount="0", hmac_key_id="test-key",
    )
    provider = RecordingMockProvider()
    service = ProductionService(
        str(project_dir / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        visual_adapter=VisualStageAdapter(provider=provider),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": b"offline-only-key"}),
    )
    with patch("manju.production.service.get_ai_config", return_value=(None, "mock-model", None)):
        service.advance()  # create/run start
        service.advance()  # storyboard
        awaiting = service.advance()  # visual public plan + signed approval
        assert awaiting.status == "awaiting_approval"
        approved = service.decide_approval(
            "approval-" + awaiting.run_id.removeprefix("run_"), decision="approve", reviewer="tester",
            expected_last_event_hash=awaiting.last_event_hash,
        )
        granted = service.issue_grant(
            "approval-" + awaiting.run_id.removeprefix("run_"), grant_id="grant-1", issued_by="tester",
            expected_last_event_hash=approved.last_event_hash,
        )
        completed = service.run_until_blocked()

    assert granted.status == "running"
    assert completed.status == "completed"
    assert sum(provider.submit_counts.values()) == 1
    assert provider.idempotency_keys == {
        "visual-" + completed.run_id.removeprefix("run_"): "visual-" + completed.run_id.removeprefix("run_")
    }
    assert len(provider.reconciles) == 1
    events = service.store.events.read()
    assert [event["event_type"] for event in events if event["event_type"].startswith("call_")] == [
        "call_reserved", "call_submitted", "call_settled"
    ]
    assert any(event["event_type"] == "stage_completed" and event["payload"].get("stage") == "visual" for event in events)


def test_fresh_service_verifies_completed_receipt_for_status_run_advance_and_doctor_without_reconcile(tmp_path):
    service, _ = _granted_service(tmp_path / "fresh-completed")
    assert service.run_until_blocked().status == "completed"
    project_file = str(Path(service.paths.root) / "project.json")

    def restarted():
        provider = RecordingMockProvider()
        value = ProductionService(
            project_file, storyboard_adapter=FixtureStoryboardAdapter(),
            visual_adapter=VisualStageAdapter(provider=provider),
            hmac_key_provider=MappingHmacKeyProvider({"test-key": b"offline-only-key"}),
        )
        value._configured_model = lambda: "mock-model"
        return value, provider

    status_service, status_provider = restarted()
    assert status_service.get_status().status == "completed"
    run_service, run_provider = restarted()
    assert run_service.run_until_blocked().status == "completed"
    advance_service, advance_provider = restarted()
    assert advance_service.advance().status == "completed"
    doctor_service, doctor_provider = restarted()
    assert doctor_service.doctor()["status"] == "passed"
    for provider in (status_provider, run_provider, advance_provider, doctor_provider):
        assert provider.reconciles == [] and provider.submit_counts == {}


def test_visual_plan_round_trip_and_authority_tamper_detection(tmp_path):
    adapter = VisualStageAdapter()
    output_dir = str(tmp_path / "visual")
    request = adapter.plan(
        project_id="prj_1", run_id="run_1", stage_run_id="visual_1", output_dir=output_dir,
        storyboard_artifact={"artifact_id": "storyboard", "version_id": "sha256:storyboard"},
        settings={"maximum_paid_calls": 1, "maximum_amount": "0"},
    )
    plan = read_json(str(tmp_path / "visual" / "visual_plan.json"))
    assert adapter.map_published_approval(plan).request_id == request.request_id
    operation = {
        "operation_id": "op_1", "provider_job_id": "job_1",
        "result_fingerprint": VisualStageAdapter.artifact_result_fingerprint("op_1", "job_1"),
    }
    assert adapter.complete(stage_run_id="visual_1", output_dir=output_dir, operation=operation).status == "completed"
    authority_path = tmp_path / "visual" / "visual_authority.json"
    authority = read_json(str(authority_path))
    authority["artifact"]["sha256"] = "0" * 64
    atomic_write_json(str(authority_path), authority)
    assert adapter.inspect(stage_run_id="visual_1", output_dir=output_dir).status == "failed"

    completed = adapter.complete(stage_run_id="visual_1", output_dir=output_dir, operation=operation)
    assert completed.status == "completed"
    authority = read_json(str(authority_path))
    authority["operation"]["operation_id"] = "op_other"
    atomic_write_json(str(authority_path), authority)
    assert adapter.inspect(stage_run_id="visual_1", output_dir=output_dir).status == "failed"


def test_mock_provider_submit_is_idempotent_across_restart_boundary():
    provider = MockVisualProvider()
    first = provider.submit("op_1")  # Provider accepts before call_submitted is durably appended.
    restarted_adapter = VisualStageAdapter(provider=provider)
    second = restarted_adapter.provider.submit("op_1")
    assert first == second
    assert provider.submit_counts["op_1"] == 1


def test_mock_failed_and_unknown_outcomes_have_terminal_paths(tmp_path):
    failed_service, _ = _granted_service(tmp_path / "failed", outcome="failed")
    assert failed_service.run_until_blocked().status == "failed"

    unknown_service, _ = _granted_service(tmp_path / "unknown", outcome="outcome_unknown")
    blocked = unknown_service.run_until_blocked()
    assert blocked.status == "blocked" and blocked.exit_code == 4
    latest = next(event for event in reversed(unknown_service.store.events.read()) if event["event_type"] == "call_settled")
    from manju.production.operations import OperationRecord
    submitted = OperationRecord.from_dict(latest["payload"]["operation"])
    recovered = submitted.reconcile(
        outcome="succeeded",
        result_fingerprint=VisualStageAdapter.artifact_result_fingerprint(
            submitted.operation_id, submitted.provider_job_id
        ),
        usage={"calls": "1"},
    )
    unknown_service.reconcile_operation(recovered, expected_last_event_hash=blocked.last_event_hash)
    assert unknown_service.run_until_blocked().status == "completed"


def test_crash_after_provider_acceptance_does_not_duplicate_mock_submit(tmp_path, monkeypatch):
    service, provider = _granted_service(tmp_path / "crash")
    service.advance()  # call_reserved
    original_append = service.store.events.append
    failed_once = {"value": False}

    def crash_before_call_submitted(event_type, **kwargs):
        if event_type == "call_submitted" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("simulated append crash after provider acceptance")
        return original_append(event_type, **kwargs)

    monkeypatch.setattr(service.store.events, "append", crash_before_call_submitted)
    try:
        try:
            service.advance()
        except RuntimeError:
            pass
    finally:
        monkeypatch.setattr(service.store.events, "append", original_append)
    assert sum(provider.submit_counts.values()) == 1
    assert service.run_until_blocked().status == "completed"
    assert sum(provider.submit_counts.values()) == 1


def test_expired_grant_blocks_reservation_submit_and_reconciliation_before_provider_effect(tmp_path, monkeypatch):
    real_utc_now = production_service.utc_now
    reserve_service, reserve_provider = _granted_service(tmp_path / "expired-reserve")
    monkeypatch.setattr("manju.production.service.utc_now", lambda: "2100-01-01T00:00:00Z")
    try:
        reserve_service.advance()
        assert False, "expired grant must reject call_reserved"
    except ProductionError as exc:
        assert exc.code == ReasonCode.GRANT_CONTRACT_INVALID.value
    assert not reserve_provider.submit_counts
    assert not [event for event in reserve_service.store.events.read() if event["event_type"].startswith("call_")]

    monkeypatch.setattr("manju.production.service.utc_now", real_utc_now)
    submit_service, submit_provider = _granted_service(tmp_path / "expired-submit")
    monkeypatch.setattr("manju.production.service.utc_now", lambda: "2026-01-01T00:00:00Z")
    submit_service.advance()  # call_reserved while grant is valid
    monkeypatch.setattr("manju.production.service.utc_now", lambda: "2100-01-01T00:00:00Z")
    try:
        submit_service.advance()
        assert False, "expired grant must reject provider.submit"
    except ProductionError as exc:
        assert exc.code == ReasonCode.GRANT_CONTRACT_INVALID.value
    assert not submit_provider.submit_counts

    monkeypatch.setattr("manju.production.service.utc_now", real_utc_now)
    reconcile_service, reconcile_provider = _granted_service(tmp_path / "expired-reconcile")
    monkeypatch.setattr("manju.production.service.utc_now", lambda: "2026-01-01T00:00:00Z")
    reconcile_service.advance()  # call_reserved
    reconcile_service.advance()  # call_submitted
    monkeypatch.setattr("manju.production.service.utc_now", lambda: "2100-01-01T00:00:00Z")
    try:
        reconcile_service.advance()
        assert False, "expired grant must reject provider.reconcile"
    except ProductionError as exc:
        assert exc.code == ReasonCode.GRANT_CONTRACT_INVALID.value
    assert not reconcile_provider.reconciles


def test_completed_run_revalidates_storyboard_and_visual_authorities(tmp_path):
    service, _provider = _granted_service(tmp_path / "all-terminal-authorities")
    assert service.run_until_blocked().status == "completed"
    storyboard_event = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed" and event["payload"].get("stage") == "storyboard"
    )
    storyboard_path = Path(service.paths.root, storyboard_event["payload"]["artifacts"][0]["path"])
    atomic_write_json(str(storyboard_path), {"tampered": True})
    try:
        service.get_status()
        assert False, "completed status must retain storyboard integrity checks"
    except ProductionError as exc:
        assert exc.code == ReasonCode.STAGE_INTEGRITY_FAILED.value


def test_visual_output_cannot_be_rebound_after_rehashing_unsigned_terminal_events(tmp_path):
    service, _provider = _granted_service(tmp_path / "rebound-visual")
    assert service.run_until_blocked().status == "completed"
    events = service.store.events.read()
    terminal_index = next(
        index for index, event in enumerate(events)
        if event["event_type"] == "stage_completed" and event["payload"].get("stage") == "visual"
    )
    terminal = events[terminal_index]
    authority_path = Path(service.paths.root, terminal["payload"]["authority_path"])
    artifact_path = Path(service.paths.root, terminal["payload"]["artifacts"][0]["path"])
    forged = {"operation_id": "forged-op", "provider_job_id": "forged-job", "result_fingerprint": "sha256:forged"}
    artifact = read_json(str(artifact_path))
    artifact.update(forged)
    atomic_write_json(str(artifact_path), artifact)
    authority = read_json(str(authority_path))
    authority["operation"] = forged
    authority["artifact"]["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    atomic_write_json(str(authority_path), authority)
    terminal["payload"]["authority_hash"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    terminal["payload"]["authority_files"][0]["sha256"] = terminal["payload"]["authority_hash"]
    terminal["payload"]["artifacts"][0]["version_id"] = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    previous_hash = events[terminal_index - 1]["event_hash"]
    for event in events[terminal_index:]:
        event["previous_hash"] = previous_hash
        unsigned = {key: value for key, value in event.items() if key != "event_hash"}
        event["event_hash"] = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
        previous_hash = event["event_hash"]
    Path(service.paths.events_file).write_text(
        "".join(canonical_json(event) + "\n" for event in events), encoding="utf-8"
    )

    for probe in (service.get_status, service.advance):
        try:
            probe()
            assert False, "forged visual output must be rejected"
        except ProductionError as exc:
            assert exc.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    doctor = service.doctor()
    assert doctor["status"] == "failed"
    assert doctor["checks"][-1]["code"] == ReasonCode.STAGE_INTEGRITY_FAILED.value


def test_signed_result_fingerprint_commits_all_visual_artifact_bytes(tmp_path):
    service, _provider = _granted_service(tmp_path / "artifact-byte-commitment")
    assert service.run_until_blocked().status == "completed"
    events = service.store.events.read()
    terminal_index = next(
        index for index, event in enumerate(events)
        if event["event_type"] == "stage_completed" and event["payload"].get("stage") == "visual"
    )
    terminal = events[terminal_index]
    authority_path = Path(service.paths.root, terminal["payload"]["authority_path"])
    artifact_path = Path(service.paths.root, terminal["payload"]["artifacts"][0]["path"])
    settled = next(
        event for event in reversed(events)
        if event["event_type"] in {"call_settled", "call_reconciled"}
    )
    signed_operation = settled["payload"]["operation"]
    assert signed_operation["result_fingerprint"] == "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    # Keep every signed operation field unchanged.  Re-signing ordinary terminal
    # events and authority references must not make new public bytes acceptable.
    artifact = read_json(str(artifact_path))
    artifact["attacker_payload"] = "injected"
    atomic_write_json(str(artifact_path), artifact)
    authority = read_json(str(authority_path))
    authority["artifact"]["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    atomic_write_json(str(authority_path), authority)
    terminal["payload"]["authority_hash"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    terminal["payload"]["authority_files"][0]["sha256"] = terminal["payload"]["authority_hash"]
    terminal["payload"]["artifacts"][0]["version_id"] = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    previous_hash = events[terminal_index - 1]["event_hash"]
    for event in events[terminal_index:]:
        event["previous_hash"] = previous_hash
        unsigned = {key: value for key, value in event.items() if key != "event_hash"}
        event["event_hash"] = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
        previous_hash = event["event_hash"]
    Path(service.paths.events_file).write_text(
        "".join(canonical_json(event) + "\n" for event in events), encoding="utf-8"
    )

    for probe in (service.get_status, service.advance):
        try:
            probe()
            assert False, "changed visual bytes must not match the signed result"
        except ProductionError as exc:
            assert exc.code == ReasonCode.STAGE_INTEGRITY_FAILED.value
    doctor = service.doctor()
    assert doctor["status"] == "failed"
    assert doctor["checks"][-1]["code"] == ReasonCode.STAGE_INTEGRITY_FAILED.value
