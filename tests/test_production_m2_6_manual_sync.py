"""Offline M2.6 safety tests.  No test starts an HTTP server or reads a real key."""

import json
import os

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production.manual_operations import ManualBillingEvidence, ManualResultPackage
from manju.production.manual_worker import claim_dispatch, execute_fixture, execute_http_once
from manju.production.models import ProductionError, ReasonCode, utc_now
from manju.production.runtime_profiles import PROFILE_CONFIG_ENV
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.adapters.visual import VisualStageAdapter
from tests.test_production_m2_1_mock_visual import FixtureStoryboardAdapter


KEY = b"m2-6-offline-only-key"


def _service(tmp_path, monkeypatch, *, maximum="5"):
    monkeypatch.setenv(PROFILE_CONFIG_ENV, json.dumps({"sync-image": {"mode": "manual_sync"}}))
    monkeypatch.setenv("MANJU_PRODUCTION_HMAC_KEY", KEY.decode("ascii"))
    source = tmp_path / "source.txt"
    source.write_text("manual fixture source", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(source=str(source), source_type="script", output_dir=str(project), engine="agent",
                       visual_enabled=True, visual_maximum_paid_calls=1, visual_maximum_amount=maximum,
                       visual_provider_profile="sync-image", visual_provider_request={"prompt": "approved fixture", "model": "image", "size": "1024x1024"},
                       visual_operation_kind="image_generation", hmac_key_id="test-key")
    service = ProductionService(str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
                                visual_adapter=VisualStageAdapter(), hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}))
    service._configured_model = lambda: "fixture"
    service.advance()
    service.advance()
    awaiting = service.advance()
    request_id = "approval-" + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="tester", expected_last_event_hash=awaiting.last_event_hash)
    granted = service.issue_grant(request_id, grant_id="manual-grant", issued_by="tester", expected_last_event_hash=approved.last_event_hash)
    return service, granted


def test_manual_sync_closed_loop_blocks_until_signed_billing_then_publishes(tmp_path, monkeypatch):
    service, granted = _service(tmp_path, monkeypatch)
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    dispatch = prepared["dispatch"]
    assert "api_key" not in json.dumps(dispatch).lower()
    assert service.store.snapshot().status == "blocked"

    dispatch_file = tmp_path / "dispatch.json"
    dispatch_file.write_text(json.dumps(dispatch), encoding="utf-8")
    from manju.production.manual_operations import ManualDispatchPackage
    parsed = ManualDispatchPackage.from_dict(dispatch)
    result_dir = tmp_path / "worker-result"
    result, claim = execute_fixture(parsed, state_dir=str(tmp_path / "worker-state"), output_dir=str(result_dir))
    assert os.path.isfile(claim)
    imported = service.import_manual_result(result, package_dir=str(result_dir), expected_last_event_hash=prepared["last_event_hash"])
    assert imported.status == "blocked" and imported.reason.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value

    invoice = result_dir / "invoice.json"
    invoice.write_text('{"actual_amount":"3","currency":"USD"}', encoding="utf-8")
    from manju.production.manual_operations import sha256_file
    evidence = ManualBillingEvidence(prepared["dispatch_sha256"], parsed.operation_id, parsed.claim_token, "succeeded", "3", "USD",
                                     "fixture-ledger-row-1", invoice.name, sha256_file(invoice), "reviewer", utc_now(), "test-key").sign(KEY)
    reconciled = service.reconcile_manual_cost(evidence, package_dir=str(result_dir), expected_last_event_hash=imported.last_event_hash)
    assert reconciled.status == "running"
    assert service.run_until_blocked().status == "completed"
    events = service.store.events.read()
    assert [event["event_type"] for event in events if event["event_type"].startswith("manual_")] == [
        "manual_dispatch_prepared", "manual_result_imported", "manual_cost_reconciled"
    ]


def test_worker_claim_is_durable_and_never_allows_a_second_attempt(tmp_path, monkeypatch):
    service, granted = _service(tmp_path, monkeypatch)
    dispatch = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)["dispatch"]
    from manju.production.manual_operations import ManualDispatchPackage
    parsed = ManualDispatchPackage.from_dict(dispatch)
    first = claim_dispatch(parsed, state_dir=str(tmp_path / "state"))
    with pytest.raises(ProductionError) as caught:
        claim_dispatch(parsed, state_dir=str(tmp_path / "state"))
    assert caught.value.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value
    value = json.loads(open(first, encoding="utf-8").read())
    assert value["state"] == "claimed"


def test_tampered_dispatch_result_and_billing_are_rejected_without_network(tmp_path, monkeypatch):
    service, granted = _service(tmp_path, monkeypatch)
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    from manju.production.manual_operations import ManualDispatchPackage
    dispatch = ManualDispatchPackage.from_dict(prepared["dispatch"])
    forged = {**dispatch.to_dict(), "provider_request": {"prompt": "substituted"}}
    with pytest.raises(ProductionError):
        execute_fixture(ManualDispatchPackage.from_dict(forged), state_dir=str(tmp_path / "state"), output_dir=str(tmp_path / "result"))
    result_dir = tmp_path / "result"
    result, _ = execute_fixture(dispatch, state_dir=str(tmp_path / "other-state"), output_dir=str(result_dir))
    forged_result = ManualResultPackage(**{**result.__dict__, "claim_token": "forged-" + result.claim_token})
    with pytest.raises(ProductionError):
        service.import_manual_result(forged_result, package_dir=str(result_dir), expected_last_event_hash=prepared["last_event_hash"])
    imported = service.import_manual_result(result, package_dir=str(result_dir), expected_last_event_hash=prepared["last_event_hash"])
    invoice = result_dir / "invoice.json"
    invoice.write_text('{"actual_amount":"1","currency":"EUR"}', encoding="utf-8")
    from manju.production.manual_operations import sha256_file
    evidence = ManualBillingEvidence(prepared["dispatch_sha256"], dispatch.operation_id, dispatch.claim_token, "succeeded", "1", "EUR",
                                     "fixture", invoice.name, sha256_file(invoice), "reviewer", utc_now(), "test-key").sign(KEY)
    with pytest.raises(ProductionError):
        service.reconcile_manual_cost(evidence, package_dir=str(result_dir), expected_last_event_hash=imported.last_event_hash)


def test_unknown_or_over_budget_manual_billing_does_not_publish(tmp_path, monkeypatch):
    service, granted = _service(tmp_path, monkeypatch, maximum="2")
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    from manju.production.manual_operations import ManualDispatchPackage
    dispatch = ManualDispatchPackage.from_dict(prepared["dispatch"])
    result_dir = tmp_path / "result"
    result, _ = execute_fixture(dispatch, state_dir=str(tmp_path / "state"), output_dir=str(result_dir))
    imported = service.import_manual_result(result, package_dir=str(result_dir), expected_last_event_hash=prepared["last_event_hash"])
    assert service.run_until_blocked().status == "blocked"
    invoice = result_dir / "invoice.json"
    invoice.write_text('{"actual_amount":"3","currency":"USD"}', encoding="utf-8")
    from manju.production.manual_operations import sha256_file
    evidence = ManualBillingEvidence(prepared["dispatch_sha256"], dispatch.operation_id, dispatch.claim_token, "succeeded", "3", "USD",
                                     "fixture", invoice.name, sha256_file(invoice), "reviewer", utc_now(), "test-key").sign(KEY)
    budget = service.reconcile_manual_cost(evidence, package_dir=str(result_dir), expected_last_event_hash=imported.last_event_hash)
    assert budget.status == "blocked" and budget.reason.code == ReasonCode.BUDGET_EXCEEDED.value


def test_real_worker_requires_explicit_confirmation_before_claim_or_network(tmp_path, monkeypatch):
    service, granted = _service(tmp_path, monkeypatch)
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    from manju.production.manual_operations import ManualDispatchPackage
    dispatch = ManualDispatchPackage.from_dict(prepared["dispatch"])
    with pytest.raises(ProductionError) as caught:
        execute_http_once(dispatch, state_dir=str(tmp_path / "state"), output_dir=str(tmp_path / "result"), confirmation="no")
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value
    assert not (tmp_path / "state").exists()


def test_fixed_nominal_price_without_billing_file_is_not_a_reconciliation(tmp_path, monkeypatch):
    service, granted = _service(tmp_path, monkeypatch)
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    from manju.production.manual_operations import ManualDispatchPackage
    dispatch = ManualDispatchPackage.from_dict(prepared["dispatch"])
    result_dir = tmp_path / "result"
    result, _ = execute_fixture(dispatch, state_dir=str(tmp_path / "state"), output_dir=str(result_dir))
    imported = service.import_manual_result(result, package_dir=str(result_dir), expected_last_event_hash=prepared["last_event_hash"])
    with pytest.raises(ProductionError):
        ManualBillingEvidence(prepared["dispatch_sha256"], dispatch.operation_id, dispatch.claim_token, "succeeded", "5", "USD",
                              "not-a-ledger", "", "", "reviewer", utc_now(), "test-key").sign(KEY)
    assert service.store.snapshot().last_event_hash == imported.last_event_hash


def test_cli_manual_profile_reaches_prepared_dispatch_without_provider_transport(tmp_path, monkeypatch):
    monkeypatch.setenv(PROFILE_CONFIG_ENV, json.dumps({"sync-image": {"mode": "manual_sync"}}))
    monkeypatch.setenv("MANJU_PRODUCTION_HMAC_KEY", KEY.decode("ascii"))
    source = tmp_path / "source.txt"
    request = tmp_path / "request.json"
    project = tmp_path / "project"
    source.write_text("cli manual source", encoding="utf-8")
    request.write_text('{"prompt":"approved","model":"image","size":"1024x1024"}', encoding="utf-8")
    runner = CliRunner()
    initialized = runner.invoke(cli, ["project", "init", "--source", str(source), "--source-type", "script", "--output-dir", str(project),
                                      "--visual-provider-profile", "sync-image", "--visual-request-file", str(request), "--visual-max-amount", "1", "--hmac-key-id", "test-key", "--json"])
    assert initialized.exit_code == 0, initialized.output
    # The real storyboard is not run in this test; patch it locally, while the
    # CLI service construction remains the actual user-facing path.
    from unittest.mock import patch
    from manju.production.service import ProductionService
    original = ProductionService.__init__
    def fixture_init(self, *args, **kwargs):
        kwargs["storyboard_adapter"] = FixtureStoryboardAdapter()
        return original(self, *args, **kwargs)
    with patch.object(ProductionService, "__init__", fixture_init), patch.object(ProductionService, "_configured_model", return_value="fixture"):
        first = runner.invoke(cli, ["run", str(project / "project.json"), "--json"])
        assert first.exit_code == 3, first.output
        awaiting = json.loads(first.output)
        request_id = "approval-" + awaiting["run_id"].removeprefix("run_")
        approved = runner.invoke(cli, ["approve", str(project / "project.json"), request_id, "--reviewer", "tester", "--expected-last-event-hash", awaiting["last_event_hash"], "--json"])
        assert approved.exit_code == 3, approved.output
        approved_value = json.loads(approved.output)
        granted = runner.invoke(cli, ["issue-grant", str(project / "project.json"), request_id, "--grant-id", "cli-grant", "--issued-by", "tester", "--expected-last-event-hash", approved_value["last_event_hash"], "--json"])
        assert granted.exit_code == 0, granted.output
        granted_value = json.loads(granted.output)
        prepared = runner.invoke(cli, ["prepare-manual", str(project / "project.json"), "--expected-last-event-hash", granted_value["last_event_hash"], "--json"])
        events_before = (project / "production" / "events.jsonl").read_text(encoding="utf-8")
        stopped = runner.invoke(cli, ["run", str(project / "project.json"), "--json"])
    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.output)["dispatch"]["provider_profile"] == "sync-image"
    assert stopped.exit_code == 4, stopped.output
    assert json.loads(stopped.output)["status"] == "blocked"
    assert (project / "production" / "events.jsonl").read_text(encoding="utf-8") == events_before
