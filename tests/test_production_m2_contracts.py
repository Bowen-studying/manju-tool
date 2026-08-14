import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production.adapters.visual import VisualStageAdapter
from manju.production.approvals import ApprovalRequest, Grant
from manju.production.events import EventStore
from manju.production.models import ProductionError, ProductionStatus, ReasonCode
from manju.production.models import ProductionReason, ProductionSnapshot
from manju.production.operations import OperationRecord
from manju.production.reducer import reduce_events
from manju.production.scheduler import ProductionScheduler
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService


KEY_ID = "test-key"
KEY = b"offline-test-key"


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        request_id="apr_1", project_id="prj_1", run_id="run_1", stage="visual", stage_run_id="visual_1",
        kind="paid_visual_batch", state_fingerprint="sha256:state",
        artifact_versions=({"artifact_id": "storyboard", "version_id": "sha256:storyboard"},),
        operation_intents=({"operation_id": "op_1", "input_fingerprint": "sha256:input", "kind": "image"},),
        maximum_paid_calls=1, maximum_amount="0", currency="USD", provider_profile="mock",
        expires_at="2099-01-01T00:00:00Z",
    )


def _store(tmp_path):
    return EventStore(str(tmp_path / "events.jsonl"), key_provider=MappingHmacKeyProvider({KEY_ID: KEY}))


def _awaiting_events(store):
    request = _request()
    store.append("project_initialized", project_id="prj_1")
    store.append("run_created", project_id="prj_1", run_id="run_1")
    store.append("run_started", project_id="prj_1", run_id="run_1")
    store.append("approval_requested", project_id="prj_1", run_id="run_1",
                 payload={"approval_request": request.to_dict(), "key_id": KEY_ID})
    return request


def test_signed_sensitive_event_tampering_is_detected(tmp_path):
    store = _store(tmp_path)
    _awaiting_events(store)
    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["key_id"] == KEY_ID and event["hmac"]
    event["hmac"] = "0" * 64
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps(event, ensure_ascii=False, sort_keys=True)
    # Hash-chain check notices this even without a signer; re-signing the hash must still fail HMAC.
    event["event_hash"] = __import__("hashlib").sha256(
        __import__("manju.production.models", fromlist=["canonical_json"]).canonical_json(
            {k: v for k, v in event.items() if k != "event_hash"}
        ).encode("utf-8")
    ).hexdigest()
    lines[-1] = json.dumps(event, ensure_ascii=False, sort_keys=True)
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ProductionError) as caught:
        store.read()
    assert caught.value.code == ReasonCode.SENSITIVE_EVENT_SIGNATURE_INVALID.value


def test_sensitive_append_requires_ephemeral_key(tmp_path):
    store = EventStore(str(tmp_path / "events.jsonl"))
    with pytest.raises(ProductionError) as caught:
        store.append("approval_requested", project_id="prj_1", payload={"key_id": KEY_ID})
    assert caught.value.code == ReasonCode.HMAC_KEY_UNAVAILABLE.value
    assert not (tmp_path / "events.jsonl").exists()


def test_unsigned_sensitive_event_is_rejected_even_in_hash_only_read_mode(tmp_path):
    store = EventStore(str(tmp_path / "events.jsonl"))
    event = {"event_version": "1", "sequence": 1, "occurred_at": "2026-08-11T00:00:00Z",
             "event_type": "approval_requested", "project_id": "prj_1", "run_id": "run_1",
             "payload": {}, "previous_hash": "0" * 64}
    from manju.production.models import canonical_json
    import hashlib
    event["event_hash"] = hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(ProductionError) as caught:
        store.read()
    assert caught.value.code == ReasonCode.SENSITIVE_EVENT_SIGNATURE_INVALID.value


def test_reducer_returns_awaiting_approval_exit_three_and_grant_resumes(tmp_path):
    store = _store(tmp_path)
    request = _awaiting_events(store)
    snapshot = reduce_events(store.read())
    assert snapshot.status == ProductionStatus.AWAITING_APPROVAL.value
    assert snapshot.exit_code == 3
    assert {item["action"] for item in snapshot.next_actions} == {"approve", "reject"}
    store.append("approval_approved", project_id="prj_1", run_id="run_1",
                 payload={"request_id": request.request_id, "reviewer": "reviewer", "key_id": KEY_ID})
    grant = Grant.issue(request, grant_id="gr_1", issued_by="reviewer", issued_at="2026-08-11T00:00:00Z",
                        key_id=KEY_ID, key=KEY)
    store.append("grant_issued", project_id="prj_1", run_id="run_1",
                 payload={"grant": grant.to_dict(), "key_id": KEY_ID})
    snapshot = reduce_events(store.read())
    assert snapshot.status == ProductionStatus.RUNNING.value


def test_grant_and_operation_bindings_are_immutable():
    request = _request()
    grant = Grant.issue(request, grant_id="gr_1", issued_by="reviewer", issued_at="2026-08-11T00:00:00Z",
                        key_id=KEY_ID, key=KEY)
    grant.validate_against(request, key=KEY, now="2026-08-11T01:00:00Z")
    with pytest.raises(ProductionError):
        Grant.issue(request, grant_id="gr_expired", issued_by="reviewer", issued_at="2100-01-01T00:00:00Z",
                    key_id=KEY_ID, key=KEY)
    mismatched = ApprovalRequest(**{**request.__dict__, "run_id": "run_2"})
    with pytest.raises(ProductionError) as caught:
        grant.validate_against(mismatched, key=KEY)
    assert caught.value.code == ReasonCode.GRANT_CONTRACT_INVALID.value
    changed_artifact = ApprovalRequest(**{**request.__dict__, "artifact_versions": ({"artifact_id": "storyboard", "version_id": "sha256:other"},)})
    with pytest.raises(ProductionError):
        grant.validate_against(changed_artifact, key=KEY)
    changed_fingerprint = ApprovalRequest(**{**request.__dict__, "operation_intents": ({"operation_id": "op_1", "input_fingerprint": "sha256:other", "kind": "image"},)})
    with pytest.raises(ProductionError):
        grant.validate_against(changed_fingerprint, key=KEY)
    reserved = OperationRecord("op_1", "gr_1", "image", "sha256:input", "mock")
    submitted = reserved.submit("job_1")
    settled = submitted.settle(outcome="succeeded", result_fingerprint="sha256:result")
    assert settled.status == "settled"
    with pytest.raises(ProductionError):
        reserved.settle(outcome="succeeded")
    assert submitted.settle(outcome="outcome_unknown").outcome == "outcome_unknown"


def test_visual_adapter_accepts_only_published_public_contract():
    adapter = VisualStageAdapter()
    assert adapter.map_published_approval({"published_approval": _request().to_dict(), "adapter_contract_version": adapter.contract_version}).request_id == "apr_1"
    with pytest.raises(ProductionError):
        adapter.map_published_approval({"state": {"pending_approval": _request().to_dict()}})


def test_expired_approval_and_skipped_operation_transition_are_rejected(tmp_path):
    store = _store(tmp_path)
    request = ApprovalRequest(**{**_request().__dict__, "expires_at": "2020-01-01T00:00:00Z"})
    store.append("project_initialized", project_id="prj_1")
    store.append("run_created", project_id="prj_1", run_id="run_1")
    store.append("run_started", project_id="prj_1", run_id="run_1")
    store.append("approval_requested", project_id="prj_1", run_id="run_1",
                 payload={"approval_request": request.to_dict(), "key_id": KEY_ID})
    with pytest.raises(ProductionError):
        reduce_events(store.read())


def test_unknown_outcome_blocks_all_work_until_signed_reconciliation(tmp_path):
    store = _store(tmp_path)
    request = ApprovalRequest(**{
        **_request().__dict__,
        "operation_intents": (
            {"operation_id": "op_1", "input_fingerprint": "sha256:input-1", "kind": "image"},
            {"operation_id": "op_2", "input_fingerprint": "sha256:input-2", "kind": "image"},
        ),
        "maximum_paid_calls": 2,
    })
    store.append("project_initialized", project_id="prj_1")
    store.append("run_created", project_id="prj_1", run_id="run_1")
    store.append("run_started", project_id="prj_1", run_id="run_1")
    store.append("approval_requested", project_id="prj_1", run_id="run_1", payload={"approval_request": request.to_dict(), "key_id": KEY_ID})
    store.append("approval_approved", project_id="prj_1", run_id="run_1", payload={"request_id": "apr_1", "reviewer": "reviewer", "key_id": KEY_ID})
    grant = Grant.issue(request, grant_id="gr_1", issued_by="reviewer", issued_at="2026-08-11T00:00:00Z", key_id=KEY_ID, key=KEY)
    store.append("grant_issued", project_id="prj_1", run_id="run_1", payload={"grant": grant.to_dict(), "key_id": KEY_ID})
    first = OperationRecord("op_1", "gr_1", "image", "sha256:input-1", "mock")
    second = OperationRecord("op_2", "gr_1", "image", "sha256:input-2", "mock")
    for operation in (first, second):
        store.append("call_reserved", project_id="prj_1", run_id="run_1", payload={"operation": operation.to_dict(), "key_id": KEY_ID})
    submitted = first.submit("job_1")
    unknown = submitted.settle(outcome="outcome_unknown")
    store.append("call_submitted", project_id="prj_1", run_id="run_1", payload={"operation": submitted.to_dict(), "key_id": KEY_ID})
    store.append("call_settled", project_id="prj_1", run_id="run_1", payload={"operation": unknown.to_dict(), "key_id": KEY_ID})
    assert reduce_events(store.read()).status == ProductionStatus.BLOCKED.value
    store.append("call_submitted", project_id="prj_1", run_id="run_1", payload={"operation": second.submit("job_2").to_dict(), "key_id": KEY_ID})
    with pytest.raises(ProductionError):
        reduce_events(store.read())


def test_blocked_run_stops_with_exit_four_and_cli_does_not_advance(tmp_path, monkeypatch):
    snapshot = ProductionSnapshot(
        project_id="prj_1", run_id="run_1", status=ProductionStatus.BLOCKED.value,
        reason=ProductionReason(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value), last_event_hash="event-hash",
    )
    assert ProductionScheduler().next_action(snapshot, []) == "stop"
    service = object.__new__(ProductionService)
    service.get_status = lambda: snapshot
    service.advance = lambda: pytest.fail("blocked run must not advance")
    assert service.run_until_blocked() == snapshot

    project_file = tmp_path / "project.json"
    project_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("manju.cli._production_service", lambda _: service)
    result = CliRunner().invoke(cli, ["run", str(project_file), "--json"])
    assert result.exit_code == 4
    assert json.loads(result.output)["reason"]["code"] == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value


def test_reconciled_unknown_operation_resumes_running(tmp_path):
    store = _store(tmp_path)
    request = _request()
    _awaiting_events(store)
    store.append("approval_approved", project_id="prj_1", run_id="run_1",
                 payload={"request_id": request.request_id, "reviewer": "reviewer", "key_id": KEY_ID})
    grant = Grant.issue(request, grant_id="gr_1", issued_by="reviewer", issued_at="2026-08-11T00:00:00Z",
                        key_id=KEY_ID, key=KEY)
    store.append("grant_issued", project_id="prj_1", run_id="run_1",
                 payload={"grant": grant.to_dict(), "key_id": KEY_ID})
    reserved = OperationRecord("op_1", "gr_1", "image", "sha256:input", "mock")
    submitted = reserved.submit("job_1")
    unknown = submitted.settle(outcome="outcome_unknown")
    for event_type, operation in (("call_reserved", reserved), ("call_submitted", submitted), ("call_settled", unknown)):
        store.append(event_type, project_id="prj_1", run_id="run_1",
                     payload={"operation": operation.to_dict(), "key_id": KEY_ID})
    assert reduce_events(store.read()).status == ProductionStatus.BLOCKED.value
    reconciled = unknown.reconcile(outcome="succeeded", result_fingerprint="sha256:result", usage={"calls": "1"})
    store.append("call_reconciled", project_id="prj_1", run_id="run_1",
                 payload={"operation": reconciled.to_dict(), "key_id": KEY_ID})
    snapshot = reduce_events(store.read())
    assert snapshot.status == ProductionStatus.RUNNING.value
    assert snapshot.exit_code == 0


def test_reconcile_service_rejects_nonmatching_operation_before_append(tmp_path):
    unknown = OperationRecord("op_1", "gr_1", "image", "sha256:input", "mock").submit("job_1").settle(
        outcome="outcome_unknown"
    )
    blocked = ProductionSnapshot(
        project_id="prj_1", run_id="run_1", status=ProductionStatus.BLOCKED.value,
        reason=ProductionReason(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value), last_event_hash="event-hash",
    )

    class FakeEvents:
        def __init__(self):
            self.appended = []

        def read(self):
            return [{"run_id": "run_1", "event_type": "call_settled", "event_hash": "event-hash", "payload": {"operation": unknown.to_dict()}}]

        def append(self, *args, **kwargs):
            self.appended.append((args, kwargs))

    events = FakeEvents()
    service = object.__new__(ProductionService)
    service.paths = SimpleNamespace(lock_file=str(tmp_path / "project.lock"))
    service.store = SimpleNamespace(
        events=events,
        load_project=lambda: {"project_id": "prj_1", "integrity": {"hmac_key_id": KEY_ID}},
        snapshot=lambda: blocked,
    )
    service._snapshot_and_project = lambda: ProductionSnapshot(
        project_id="prj_1", run_id="run_1", status=ProductionStatus.RUNNING.value
    )
    mismatched = OperationRecord("op_2", "gr_1", "image", "sha256:other", "mock", status="settled", outcome="succeeded")
    with pytest.raises(ProductionError):
        service.reconcile_operation(mismatched, expected_last_event_hash="event-hash")
    assert events.appended == []

    reconciled = unknown.reconcile(outcome="succeeded", result_fingerprint="sha256:result")
    result = service.reconcile_operation(reconciled, expected_last_event_hash="event-hash")
    assert result.status == ProductionStatus.RUNNING.value
    assert events.appended[0][0][0] == "call_reconciled"

    store = _store(tmp_path / "second")
    request = _request()
    _awaiting_events(store)
    store.append("approval_approved", project_id="prj_1", run_id="run_1",
                 payload={"request_id": request.request_id, "reviewer": "reviewer", "key_id": KEY_ID})
    grant = Grant.issue(request, grant_id="gr_1", issued_by="reviewer", issued_at="2026-08-11T00:00:00Z",
                        key_id=KEY_ID, key=KEY)
    store.append("grant_issued", project_id="prj_1", run_id="run_1", payload={"grant": grant.to_dict(), "key_id": KEY_ID})
    skipped = OperationRecord("op_1", "gr_1", "image", "sha256:input", "mock", status="settled",
                              outcome="succeeded")
    store.append("call_reserved", project_id="prj_1", run_id="run_1", payload={"operation": skipped.to_dict(), "key_id": KEY_ID})
    with pytest.raises(ProductionError):
        reduce_events(store.read())
