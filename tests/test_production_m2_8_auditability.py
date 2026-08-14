"""M2.8: explicit contract-price semantics, terminal worker claims, and audit snapshots."""

import json

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production.approvals import contractual_tariff_usage
from manju.production.audit import _write_manifest, export_audit_snapshot, verify_audit_snapshot
from manju.production.manual_operations import ManualDispatchPackage
from manju.production.manual_worker import claim_dispatch, execute_fixture, inspect_claim
from manju.production.models import ProductionError, fingerprint
from manju.production.security import MappingHmacKeyProvider
from tests.test_production_m2_7_contractual_tariff import KEY, _import_success, _service


def test_current_tariff_is_signed_in_minor_units_with_explicit_success_policy(tmp_path, monkeypatch):
    service, _granted, project = _service(tmp_path, monkeypatch)
    tariff = json.loads((project / "project.json").read_text(encoding="utf-8"))["production"]["visual"]["contractual_tariff"]
    assert set(tariff) == {"tariff_id", "amount_minor", "currency", "amount_unit", "charge_policy", "pricing_scope", "tariff_sha256"}
    assert tariff["amount_minor"] == "3" and tariff["amount_unit"] == "minor"
    assert tariff["charge_policy"] == "on_success" and tariff["pricing_scope"] == "per_operation"


def test_legacy_m2_7_tariff_usage_remains_replay_compatible():
    unsigned = {"tariff_id": "legacy", "amount": "3", "currency": "USD"}
    tariff = {**unsigned, "tariff_sha256": "sha256:" + fingerprint(unsigned)}
    assert contractual_tariff_usage(tariff, "failed") == {
        "actual_amount": "3", "currency": "USD", "cost_status": "final", "cost_source": "contractual_tariff",
        "settlement_mode": "contractual_tariff", "tariff_id": "legacy", "tariff_sha256": tariff["tariff_sha256"],
        "cost_disclosure": "pre_agreed_price_not_upstream_actual_cost",
    }


def test_failed_contractual_result_records_zero_charge_under_on_success_policy(tmp_path, monkeypatch):
    service, granted, _project = _service(tmp_path, monkeypatch)
    prepared = service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)
    dispatch = ManualDispatchPackage.from_dict(prepared["dispatch"])
    result, _claim = execute_fixture(dispatch, state_dir=str(tmp_path / "state"), output_dir=str(tmp_path / "result"), outcome="failed")
    imported = service.import_manual_result(result, package_dir=str(tmp_path / "result"), expected_last_event_hash=prepared["last_event_hash"])
    settled = service.settle_manual_contractual_tariff(operation_id=dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)
    assert settled.status == "running"
    reconciled = next(event["payload"]["operation"] for event in service.store.events.read() if event["event_type"] == "call_reconciled")
    assert reconciled["usage"] == contractual_tariff_usage(
        next(event["payload"]["grant"]["contractual_tariff"] for event in service.store.events.read() if event["event_type"] == "grant_issued"),
        "failed",
    )
    assert reconciled["usage"]["actual_amount"] == "0"


def test_successful_worker_claim_records_result_and_cannot_be_reused(tmp_path, monkeypatch):
    service, granted, _project = _service(tmp_path, monkeypatch)
    dispatch = ManualDispatchPackage.from_dict(service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)["dispatch"])
    result_dir = tmp_path / "result"
    result, claim = execute_fixture(dispatch, state_dir=str(tmp_path / "state"), output_dir=str(result_dir))
    claim_value = json.loads(open(claim, encoding="utf-8").read())
    assert claim_value["state"] == "result_written" and claim_value["outcome"] == "succeeded"
    assert claim_value["operation_id"] == dispatch.operation_id and len(claim_value["result_sha256"]) == 64
    status = inspect_claim(dispatch, state_dir=str(tmp_path / "state"))
    assert status["state"] == "result_written" and status["next_action"] == "import_result"
    assert status["result_file"] == "manual_result.json"
    with pytest.raises(ProductionError):
        claim_dispatch(dispatch, state_dir=str(tmp_path / "state"))
    assert result.outcome == "succeeded"


def test_claim_dto_distinguishes_unclaimed_and_started_without_authorizing_retry(tmp_path, monkeypatch):
    service, granted, _project = _service(tmp_path, monkeypatch)
    dispatch = ManualDispatchPackage.from_dict(service.prepare_manual_dispatch(expected_last_event_hash=granted.last_event_hash)["dispatch"])
    assert inspect_claim(dispatch, state_dir=str(tmp_path / "state"))["next_action"] == "execute_once"
    claim = claim_dispatch(dispatch, state_dir=str(tmp_path / "state"))
    value = json.loads(open(claim, encoding="utf-8").read())
    value["state"] = "dispatch_started"
    value["started_at"] = "2026-08-14T00:00:00Z"
    with open(claim, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    status = inspect_claim(dispatch, state_dir=str(tmp_path / "state"))
    assert status["state"] == "dispatch_started" and status["next_action"] == "reconcile_provider"


def test_audit_snapshot_verifies_manifest_and_external_hmac_without_credentials(tmp_path, monkeypatch):
    service, granted, project = _service(tmp_path, monkeypatch)
    _prepared, _dispatch, imported = _import_success(service, granted, tmp_path)
    service.settle_manual_contractual_tariff(operation_id=_dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)
    assert service.run_until_blocked().status == "completed"
    snapshot = export_audit_snapshot(project_json=str(project / "project.json"), destination=str(tmp_path / "audit"),
                                     worker_result_dir=str(tmp_path / "result"), worker_state_dir=str(tmp_path / "worker-state"),
                                     key_provider=MappingHmacKeyProvider({"test-key": KEY}))
    assert snapshot["bundle_type"] == "evidence_snapshot" and snapshot["hmac_verification"] == "requires_external_key"
    assert verify_audit_snapshot(destination=str(tmp_path / "audit"))["manifest_valid"] is True
    with pytest.raises(ProductionError):
        verify_audit_snapshot(destination=str(tmp_path / "audit"), verify_hmac=True)
    verified = verify_audit_snapshot(destination=str(tmp_path / "audit"), key_provider=MappingHmacKeyProvider({"test-key": KEY}), verify_hmac=True)
    assert verified["hmac_verified"] is True and verified["event_count"] > 0
    (tmp_path / "audit" / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ProductionError):
        verify_audit_snapshot(destination=str(tmp_path / "audit"))


def test_audit_hmac_rejects_recomputed_manifest_and_unsafe_worker_file(tmp_path, monkeypatch):
    service, granted, project = _service(tmp_path, monkeypatch)
    _prepared, _dispatch, imported = _import_success(service, granted, tmp_path)
    service.settle_manual_contractual_tariff(operation_id=_dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)
    assert service.run_until_blocked().status == "completed"
    provider = MappingHmacKeyProvider({"test-key": KEY})
    export_audit_snapshot(project_json=str(project / "project.json"), destination=str(tmp_path / "signed-audit"), key_provider=provider)
    audit_json = tmp_path / "signed-audit" / "audit.json"
    audit_data = json.loads(audit_json.read_text(encoding="utf-8"))
    audit_data["bundle_type"] = "forged"
    audit_json.write_text(json.dumps(audit_data), encoding="utf-8")
    _write_manifest(str(tmp_path / "signed-audit"))
    with pytest.raises(ProductionError):
        verify_audit_snapshot(destination=str(tmp_path / "signed-audit"), key_provider=provider, verify_hmac=True)
    unsafe_result = tmp_path / "unsafe-result"
    unsafe_result.mkdir()
    (unsafe_result / ".env.local").write_text("token=do-not-export", encoding="utf-8")
    with pytest.raises(ProductionError):
        export_audit_snapshot(project_json=str(project / "project.json"), destination=str(tmp_path / "unsafe-audit"),
                              worker_result_dir=str(unsafe_result), key_provider=provider)


def test_audit_cli_exports_and_verifies_completed_snapshot(tmp_path, monkeypatch):
    service, granted, project = _service(tmp_path, monkeypatch)
    _prepared, dispatch, imported = _import_success(service, granted, tmp_path)
    service.settle_manual_contractual_tariff(operation_id=dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)
    assert service.run_until_blocked().status == "completed"
    runner = CliRunner()
    exported = runner.invoke(cli, ["audit", "export", str(project / "project.json"), "--destination", str(tmp_path / "cli-audit"), "--json"])
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["bundle_type"] == "evidence_snapshot"
    verified = runner.invoke(cli, ["audit", "verify", str(tmp_path / "cli-audit"), "--verify-hmac", "--json"])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["hmac_verified"] is True
