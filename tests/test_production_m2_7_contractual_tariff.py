"""M2.7 contractual-tariff settlement: a signed price, never upstream cost."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production.approvals import contractual_tariff_usage
from manju.production.manual_operations import ManualBillingEvidence, ManualDispatchPackage
from manju.production.manual_worker import execute_fixture
from manju.production.models import ProductionError
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.adapters.visual import VisualStageAdapter
from tests.test_production_m2_1_mock_visual import FixtureStoryboardAdapter


KEY = b"m2-7-contractual-tariff-only"


def _service(tmp_path, monkeypatch, *, maximum="5", tariff_amount="3"):
    monkeypatch.setenv("MANJU_PRODUCTION_VISUAL_PROFILES_JSON", json.dumps({"sync-image": {"mode": "manual_sync"}}))
    monkeypatch.setenv("MANJU_PRODUCTION_HMAC_KEY", KEY.decode("ascii"))
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.txt"
    source.write_text("contractual tariff fixture", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(source=str(source), source_type="script", output_dir=str(project), engine="agent",
                       visual_enabled=True, visual_maximum_paid_calls=1, visual_maximum_amount=maximum,
                       visual_provider_profile="sync-image", visual_provider_request={"prompt": "approved", "model": "image", "size": "1024x1024"},
                       visual_operation_kind="image_generation", visual_settlement_mode="contractual_tariff",
                       visual_contractual_tariff_id="image-contract-2026-08", visual_contractual_tariff_amount=tariff_amount,
                       hmac_key_id="test-key")
    service = ProductionService(str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(), visual_adapter=VisualStageAdapter(),
                                hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}))
    service._configured_model = lambda: "fixture"
    service.advance(); service.advance()
    awaiting = service.advance()
    request_id = "approval-" + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="reviewer", expected_last_event_hash=awaiting.last_event_hash)
    granted = service.issue_grant(request_id, grant_id="contract-grant", issued_by="issuer", expected_last_event_hash=approved.last_event_hash)
    return service, granted, project


def _import_success(service, granted, tmp_path):
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    dispatch = ManualDispatchPackage.from_dict(prepared["dispatch"])
    result_dir = tmp_path / "result"
    result, _ = execute_fixture(dispatch, state_dir=str(tmp_path / "worker-state"), output_dir=str(result_dir))
    imported = service.import_manual_result(result, package_dir=str(result_dir), expected_last_event_hash=prepared["last_event_hash"])
    return prepared, dispatch, imported


def test_contractual_tariff_is_signed_before_execution_and_settles_without_billing_file(tmp_path, monkeypatch):
    service, granted, project = _service(tmp_path, monkeypatch)
    frozen = json.loads((project / "project.json").read_text(encoding="utf-8"))["production"]["visual"]["contractual_tariff"]
    prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    settled = service.settle_manual_contractual_tariff(operation_id=dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)
    assert settled.status == "running"
    assert service.run_until_blocked().status == "completed"
    events = service.store.events.read()
    grant = next(event["payload"]["grant"] for event in events if event["event_type"] == "grant_issued")
    assert grant["settlement_mode"] == "contractual_tariff" and grant["contractual_tariff"] == frozen
    reconciled = next(event["payload"]["operation"] for event in events if event["event_type"] == "call_reconciled")
    usage = reconciled["usage"]
    assert usage["cost_source"] == "contractual_tariff"
    assert usage["actual_amount"] == "3" and usage["currency"] == "USD"
    assert usage["amount_unit"] == "minor" and usage["tariff_amount_minor"] == "3"
    assert usage["charge_policy"] == "on_success" and usage["pricing_scope"] == "per_operation"
    assert usage["cost_disclosure"] == "pre_agreed_price_not_upstream_actual_cost"
    assert "provider_reference" not in usage and "evidence" not in json.dumps(events)
    stage_dir = Path(service.paths.visual_dir(settled.run_id, "visual-" + settled.run_id.removeprefix("run_")))
    receipt = json.loads((stage_dir / "visual_receipt.json").read_text(encoding="utf-8"))
    authority = json.loads((stage_dir / "visual_authority.json").read_text(encoding="utf-8"))
    assert receipt["cost"]["cost_source"] == "contractual_tariff"
    assert receipt["cost"]["cost_disclosure"] == "pre_agreed_price_not_upstream_actual_cost"
    assert authority["settlement"] == {key: receipt["cost"][key] for key in authority["settlement"]}


def test_contractual_tariff_rejects_tampering_missing_tariff_and_provider_evidence_mode(tmp_path, monkeypatch):
    with pytest.raises(ProductionError):
        _service(tmp_path / "too-large", monkeypatch, maximum="2", tariff_amount="3")
    service, granted, project = _service(tmp_path / "valid", monkeypatch)
    prepared, dispatch, imported = _import_success(service, granted, tmp_path / "valid")
    events = service.store.events.read()
    grant_event = next(event for event in events if event["event_type"] == "grant_issued")
    tampered = json.loads(json.dumps(grant_event["payload"]["grant"]))
    tampered["contractual_tariff"]["amount_minor"] = "1"
    with pytest.raises(ProductionError):
        from manju.production.approvals import Grant
        Grant.from_dict(tampered)
    project_data = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert project_data["production"]["visual"]["settlement_mode"] == "contractual_tariff"
    with pytest.raises(ProductionError):
        service.settle_manual_contractual_tariff(operation_id="other", expected_last_event_hash=imported.last_event_hash)


def test_provider_evidence_mode_cannot_use_contractual_tariff_command(tmp_path, monkeypatch):
    from tests.test_production_m2_6_manual_sync import _service as evidence_service
    service, granted = evidence_service(tmp_path, monkeypatch)
    _prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    with pytest.raises(ProductionError):
        service.settle_manual_contractual_tariff(operation_id=dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)


def test_contractual_tariff_mode_rejects_provider_billing_and_generic_reconciliation(tmp_path, monkeypatch):
    service, granted, _project = _service(tmp_path, monkeypatch)
    prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    invoice = tmp_path / "result" / "invoice.json"
    invoice.write_text('{"amount":"1"}', encoding="utf-8")
    from manju.production.manual_operations import sha256_file
    evidence = ManualBillingEvidence(prepared["dispatch_sha256"], dispatch.operation_id, dispatch.claim_token, "succeeded", "1", "USD",
                                     "forged-ledger", invoice.name, sha256_file(invoice), "reviewer", "2026-08-14T00:00:00Z", "test-key").sign(KEY)
    with pytest.raises(ProductionError):
        service.reconcile_manual_cost(evidence, package_dir=str(invoice.parent), expected_last_event_hash=imported.last_event_hash)
    previous = service._latest_operation(service.store.events.read(), imported.run_id, dispatch.operation_id)
    forged = previous.reconcile(outcome="succeeded", result_fingerprint=previous.result_fingerprint,
                                usage={"actual_amount": "1", "currency": "USD", "cost_status": "final", "cost_source": "provider_ledger"})
    with pytest.raises(ProductionError):
        service.reconcile_operation(forged, expected_last_event_hash=imported.last_event_hash)


def test_cli_contractual_tariff_settlement_takes_no_amount_or_evidence_arguments(tmp_path, monkeypatch):
    service, granted, project = _service(tmp_path, monkeypatch)
    prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    runner = CliRunner()
    args = ["settle-manual-contractual-tariff", str(project / "project.json"), "--operation-id", dispatch.operation_id,
            "--expected-last-event-hash", imported.last_event_hash, "--json"]
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["status"] == "running"
    rejected = runner.invoke(cli, args + ["--actual-amount", "1"])
    assert rejected.exit_code != 0


def test_contractual_tariff_event_forgery_is_rejected_by_reducer(tmp_path, monkeypatch):
    service, granted, _project = _service(tmp_path, monkeypatch)
    prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    events = service.store.events.read()
    grant = next(event["payload"]["grant"] for event in events if event["event_type"] == "grant_issued")
    tariff = dict(grant["contractual_tariff"])
    tariff["amount_minor"] = "1"
    key_id, _key = service._manual_key(service.store.load_project())
    service.store.events.append("manual_contractual_tariff_settled", project_id=service.store.load_project()["project_id"], run_id=imported.run_id,
                                payload={"operation_id": dispatch.operation_id, "dispatch_sha256": prepared["dispatch_sha256"],
                                         "tariff": tariff, "outcome": "succeeded", "key_id": key_id})
    with pytest.raises(ProductionError):
        service.store.snapshot()


def test_contractual_tariff_settlement_recovers_after_receipt_and_manual_event(tmp_path, monkeypatch):
    service, granted, _project = _service(tmp_path, monkeypatch)
    prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    events = service.store.events.read()
    grant = service._active_visual_grant(events, service.store.load_project(), imported.run_id)
    operation = service._latest_operation(events, imported.run_id, dispatch.operation_id)
    result = next(event["payload"]["result"] for event in events if event["event_type"] == "manual_result_imported")
    usage = contractual_tariff_usage(grant.contractual_tariff, result["outcome"])
    reconciled = operation.reconcile(outcome=result["outcome"], result_fingerprint=operation.result_fingerprint, usage=usage)
    service._configure_visual_receipt_signer(service.store.load_project())
    service.visual_adapter.settle_contractual_tariff_receipt(
        stage_run_id=dispatch.stage_run_id,
        output_dir=service.paths.visual_dir(imported.run_id, dispatch.stage_run_id),
        operation=reconciled.to_dict(), tariff=grant.contractual_tariff,
    )
    key_id, _ = service._manual_key(service.store.load_project())
    service.store.events.append("manual_contractual_tariff_settled", project_id=service.store.load_project()["project_id"], run_id=imported.run_id,
                                payload={"operation_id": dispatch.operation_id, "dispatch_sha256": prepared["dispatch_sha256"],
                                         "tariff": dict(grant.contractual_tariff), "outcome": result["outcome"], "key_id": key_id})
    recovered = service.settle_manual_contractual_tariff(operation_id=dispatch.operation_id, expected_last_event_hash=service.store.snapshot().last_event_hash)
    assert recovered.status == "running"
    assert [event["event_type"] for event in service.store.events.read()].count("manual_contractual_tariff_settled") == 1
    assert [event["event_type"] for event in service.store.events.read()].count("call_reconciled") == 1
