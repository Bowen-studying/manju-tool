import hashlib
import os
from pathlib import Path

import pytest

from manju.production.adapters.base import StageResult
from manju.production.adapters.visual import VisualStageAdapter
from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import ProviderCapabilities, ProviderObservation
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.utils.runtime import atomic_write_json, read_json


class BinaryProvider:
    def __init__(self, *, amount="5", data=b"\x89PNG\r\n\x1a\nM2.3-binary"):
        self.amount, self.data = amount, data
        self.submits = 0
        self.reconciles = 0

    capabilities = ProviderCapabilities(True, True, True, True)

    def submit(self, operation_id, *, idempotency_key, request=None):
        self.submits += 1
        return "binary-job-1"

    def reconcile(self, provider_job_id):
        self.reconciles += 1
        return ProviderObservation(
            outcome="succeeded", provider_job_id=provider_job_id,
            result_fingerprint="sha256:" + hashlib.sha256(self.data).hexdigest(),
            artifact_bytes=self.data, artifact_media_type="image/png", actual_amount=self.amount,
            currency="USD", usage={"images": "1"},
        )


def _submitted_operation():
    return {
        "operation_id": "op_1", "provider_job_id": "binary-job-1", "result_fingerprint": "",
    }


def test_binary_receipt_is_reused_after_restart_and_published_byte_for_byte(tmp_path):
    provider = BinaryProvider()
    adapter = VisualStageAdapter(provider=provider)
    output = str(tmp_path / "visual")
    observed = adapter.observe_operation(stage_run_id="visual_1", output_dir=output, operation=_submitted_operation())
    assert provider.reconciles == 1
    restarted = VisualStageAdapter(provider=provider)
    cached = restarted.observe_operation(stage_run_id="visual_1", output_dir=output, operation=_submitted_operation())
    assert cached.artifact_bytes == observed.artifact_bytes and provider.reconciles == 1
    operation = {**_submitted_operation(), "result_fingerprint": observed.result_fingerprint}
    result = restarted.publish_result(stage_run_id="visual_1", output_dir=output, operation=operation)
    artifact = Path(result.artifacts[0]["path"])
    assert artifact.name == "visual_result.png" and artifact.read_bytes() == provider.data
    assert operation["result_fingerprint"] == "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert restarted.inspect(stage_run_id="visual_1", output_dir=output).status == "completed"


@pytest.mark.parametrize("mutation", ["path", "hash"])
def test_receipt_tampering_and_path_escape_are_rejected(tmp_path, mutation):
    adapter = VisualStageAdapter(provider=BinaryProvider())
    output = str(tmp_path / "visual")
    observed = adapter.observe_operation(stage_run_id="visual_1", output_dir=output, operation=_submitted_operation())
    receipt_path = Path(output, "visual_receipt.json")
    receipt = read_json(str(receipt_path))
    if mutation == "path":
        receipt["artifact"]["pending_path"] = "..\\outside.bin"
    else:
        receipt["artifact"]["sha256"] = "0" * 64
    atomic_write_json(str(receipt_path), receipt)
    with pytest.raises(ProductionError) as caught:
        adapter.publish_result(stage_run_id="visual_1", output_dir=output,
                               operation={**_submitted_operation(), "result_fingerprint": observed.result_fingerprint})
    assert caught.value.code == ReasonCode.STAGE_INTEGRITY_FAILED.value


@pytest.mark.parametrize("field, value", [
    ("usage", {"images": "999"}), ("actual_amount", "0"),
    ("currency", "EUR"), ("cost_status", "unknown"),
])
def test_signed_receipt_rejects_usage_and_cost_tampering_before_settlement(tmp_path, field, value):
    provider = BinaryProvider()
    adapter = VisualStageAdapter(provider=provider)
    adapter.configure_receipt_signer(key_id="test-key", key=b"offline-key")
    output = str(tmp_path / "visual")
    adapter.observe_operation(stage_run_id="visual_1", output_dir=output, operation=_submitted_operation())
    receipt_path = Path(output, "visual_receipt.json")
    receipt = read_json(str(receipt_path))
    if field == "usage":
        receipt["usage"] = value
    else:
        receipt["cost"][field] = value
    atomic_write_json(str(receipt_path), receipt)
    with pytest.raises(ProductionError) as caught:
        adapter.observe_operation(stage_run_id="visual_1", output_dir=output, operation=_submitted_operation())
    assert caught.value.code == ReasonCode.STAGE_INTEGRITY_FAILED.value and provider.reconciles == 1


class _Storyboard:
    contract_version = "m2-3-fixture"

    def execute(self, *, stage_run_id, output_dir, **_kwargs):
        os.makedirs(output_dir, exist_ok=True)
        artifact, authority = Path(output_dir, "storyboard.json"), Path(output_dir, "authority.json")
        atomic_write_json(str(artifact), {"schema_version": "1", "stage_run_id": stage_run_id})
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        atomic_write_json(str(authority), {"schema_version": "1", "artifact_sha256": digest})
        auth_digest = hashlib.sha256(authority.read_bytes()).hexdigest()
        return StageResult("completed", stage_run_id, artifacts=({"path": str(artifact), "version_id": "sha256:" + digest},),
                           authority_path=str(authority), authority_hash=auth_digest,
                           authority_files=({"path": str(authority), "sha256": auth_digest},))

    inspect = execute


def test_signed_actual_cost_over_reservation_blocks_without_discarding_receipt(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(source=str(source), source_type="script", output_dir=str(project), engine="agent", visual_enabled=True,
                       visual_maximum_paid_calls=1, visual_maximum_amount="5", hmac_key_id="test-key")
    provider = BinaryProvider(amount="6")
    service = ProductionService(str(project / "project.json"), storyboard_adapter=_Storyboard(),
                                visual_adapter=VisualStageAdapter(provider=provider),
                                hmac_key_provider=MappingHmacKeyProvider({"test-key": b"offline-key"}))
    service._configured_model = lambda: "mock-model"
    service.advance(); service.advance(); awaiting = service.advance()
    approved = service.decide_approval("approval-" + awaiting.run_id.removeprefix("run_"), decision="approve", reviewer="tester", expected_last_event_hash=awaiting.last_event_hash)
    service.issue_grant("approval-" + awaiting.run_id.removeprefix("run_"), grant_id="grant-1", issued_by="tester", expected_last_event_hash=approved.last_event_hash)
    blocked = service.run_until_blocked()
    assert blocked.reason.code == ReasonCode.BUDGET_EXCEEDED.value and blocked.status == "blocked"
    receipt = next(Path(project).rglob("visual_receipt.json"))
    assert read_json(str(receipt))["cost"]["actual_amount"] == "6"
