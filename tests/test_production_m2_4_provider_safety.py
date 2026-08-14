import pytest

from manju.production.adapters.visual import VisualStageAdapter
from manju.production.approvals import Grant
from manju.production.models import ProductionError, ReasonCode
from manju.production.adapters.visual import MockVisualProvider
from manju.production.providers import ProviderCapabilities, ProviderObservation, VisualProviderRegistry


class UnsafeSynchronousProvider:
    """Models the real-test pattern: paid generation happens in reconcile()."""

    capabilities = ProviderCapabilities(True, False, False, False)

    def __init__(self):
        self.submits = 0

    def submit(self, operation_id, *, idempotency_key, request=None):
        self.submits += 1
        return "synthetic-job"

    def reconcile(self, provider_job_id):
        raise AssertionError("unsafe provider must be rejected before a paid submit")


def test_synchronous_or_non_read_only_provider_is_rejected_before_paid_submit():
    provider = UnsafeSynchronousProvider()
    adapter = VisualStageAdapter(provider=provider)
    with pytest.raises(ProductionError) as caught:
        adapter.submit_operation({"operation_id": "op_1"})
    assert caught.value.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value
    assert provider.submits == 0


def test_unknown_cost_cannot_be_disguised_as_final_or_successful():
    with pytest.raises(ProductionError):
        ProviderObservation("succeeded", "job_1", "sha256:0", b"bytes", "image/png", "", "", {}, "unknown", "unknown")
    with pytest.raises(ProductionError):
        ProviderObservation("outcome_unknown", "job_1", actual_amount="2", currency="USD", cost_status="unknown", cost_source="unknown")


def test_final_cost_has_an_explicit_verifiable_source_kind():
    observation = ProviderObservation(
        "failed", "job_1", actual_amount="3", currency="USD", usage={"units": "1"},
        cost_status="final", cost_source="provider_ledger",
    )
    assert observation.settled_usage["cost_status"] == "final"
    assert observation.cost_source == "provider_ledger"


def test_provider_request_is_publicly_bound_to_approval_grant_and_input(tmp_path):
    adapter = VisualStageAdapter()
    first = adapter.plan(
        project_id="prj_1", run_id="run_1", stage_run_id="visual_1", output_dir=str(tmp_path / "first"),
        storyboard_artifact={"artifact_id": "storyboard", "version_id": "sha256:story"},
        settings={"maximum_paid_calls": 1, "maximum_amount": "5", "provider_request": {
            "prompt": "approved public test prompt", "model": "image-test", "size": "1024x1024", "quality": "standard", "n": 1,
        }},
    )
    changed = adapter.plan(
        project_id="prj_1", run_id="run_1", stage_run_id="visual_1", output_dir=str(tmp_path / "changed"),
        storyboard_artifact={"artifact_id": "storyboard", "version_id": "sha256:story"},
        settings={"maximum_paid_calls": 1, "maximum_amount": "5", "provider_request": {
            "prompt": "substituted prompt", "model": "image-test", "size": "1024x1024", "quality": "standard", "n": 1,
        }},
    )
    assert first.operation_intents[0]["input_fingerprint"] != changed.operation_intents[0]["input_fingerprint"]
    grant = Grant.issue(first, grant_id="gr_1", issued_by="tester", issued_at="2026-08-13T00:00:00Z", key_id="test", key=b"test-key")
    with pytest.raises(ProductionError):
        grant.validate_against(changed, key=b"test-key")


def test_provider_request_rejects_credentials_or_unapproved_controls(tmp_path):
    with pytest.raises(ProductionError) as caught:
        VisualStageAdapter().plan(
            project_id="prj_1", run_id="run_1", stage_run_id="visual_1", output_dir=str(tmp_path),
            storyboard_artifact={"artifact_id": "storyboard", "version_id": "sha256:story"},
            settings={"maximum_paid_calls": 1, "maximum_amount": "5", "provider_request": {"prompt": "x", "api_key": "secret"}},
        )
    assert caught.value.code == ReasonCode.APPROVAL_CONTRACT_INVALID.value


class RecordingSafeProvider:
    capabilities = ProviderCapabilities(True, True, True, True)

    def __init__(self):
        self.requests = []

    def submit(self, operation_id, *, idempotency_key, request=None):
        self.requests.append((operation_id, idempotency_key, request))
        return "job_1"

    def reconcile(self, provider_job_id):
        raise AssertionError("not used")


class UnverifiedFailedCostProvider:
    capabilities = ProviderCapabilities(True, True, True, False)

    def submit(self, operation_id, *, idempotency_key, request=None):
        return "job_1"

    def reconcile(self, provider_job_id):
        return ProviderObservation(
            "failed", provider_job_id, actual_amount="3", currency="USD",
            cost_status="final", cost_source="provider_response",
        )


class ForgedFixtureCostProvider(UnverifiedFailedCostProvider):
    def reconcile(self, provider_job_id):
        return ProviderObservation(
            "failed", provider_job_id, actual_amount="3", currency="USD",
            cost_status="final", cost_source="test_fixture",
        )


class VerifiedForgedFixtureCostProvider(ForgedFixtureCostProvider):
    capabilities = ProviderCapabilities(True, True, True, True)


class ForgedMockSubclass(MockVisualProvider):
    def reconcile(self, provider_job_id):
        return ProviderObservation(
            "failed", provider_job_id, actual_amount="3", currency="USD",
            cost_status="final", cost_source="test_fixture",
        )


def test_exact_approved_descriptor_is_passed_to_provider_submit():
    provider = RecordingSafeProvider()
    descriptor = {"prompt": "approved", "model": "image-test", "size": "1024x1024", "n": 1}
    job = VisualStageAdapter(provider_registry=VisualProviderRegistry({"approved-provider": provider})).submit_operation({
        "operation_id": "op_1", "provider_profile": "approved-provider", "provider_request": descriptor,
    })
    assert job == "job_1"
    assert provider.requests == [("op_1", "op_1", descriptor)]


def test_non_mock_profile_requires_an_explicit_process_owned_registry():
    with pytest.raises(ProductionError) as caught:
        VisualStageAdapter(provider=RecordingSafeProvider()).submit_operation({
            "operation_id": "op_1", "provider_profile": "approved-provider",
            "provider_request": {"prompt": "approved"},
        })
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_unverified_final_cost_is_blocked_for_failed_provider_outcome(tmp_path):
    provider = UnverifiedFailedCostProvider()
    adapter = VisualStageAdapter(provider_registry=VisualProviderRegistry({"approved-provider": provider}))
    with pytest.raises(ProductionError) as caught:
        adapter.observe_operation(
            stage_run_id="visual_1", output_dir=str(tmp_path),
            operation={"operation_id": "op_1", "provider_profile": "approved-provider", "provider_job_id": "job_1"},
        )
    assert caught.value.code == ReasonCode.OPERATION_OUTCOME_UNKNOWN.value


def test_non_mock_provider_cannot_self_declare_a_test_fixture_cost(tmp_path):
    provider = ForgedFixtureCostProvider()
    adapter = VisualStageAdapter(provider_registry=VisualProviderRegistry({"approved-provider": provider}))
    with pytest.raises(ProductionError) as caught:
        adapter.observe_operation(
            stage_run_id="visual_1", output_dir=str(tmp_path),
            operation={"operation_id": "op_1", "provider_profile": "approved-provider", "provider_job_id": "job_1"},
        )
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_verified_non_mock_provider_cannot_self_declare_a_test_fixture_cost(tmp_path):
    provider = VerifiedForgedFixtureCostProvider()
    adapter = VisualStageAdapter(provider_registry=VisualProviderRegistry({"approved-provider": provider}))
    with pytest.raises(ProductionError) as caught:
        adapter.observe_operation(
            stage_run_id="visual_1", output_dir=str(tmp_path),
            operation={"operation_id": "op_1", "provider_profile": "approved-provider", "provider_job_id": "job_1"},
        )
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_registry_rejects_an_unknown_signed_profile():
    adapter = VisualStageAdapter(provider_registry=VisualProviderRegistry({"approved-provider": RecordingSafeProvider()}))
    with pytest.raises(ProductionError) as caught:
        adapter.submit_operation({
            "operation_id": "op_1", "provider_profile": "other-provider",
            "provider_request": {"prompt": "approved"},
        })
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


@pytest.mark.parametrize("adapter, profile", [
    (VisualStageAdapter(provider=ForgedMockSubclass()), "mock"),
    (VisualStageAdapter(provider_registry=VisualProviderRegistry({"approved-provider": MockVisualProvider()})), "approved-provider"),
])
def test_fixture_cost_exemption_cannot_be_inherited_or_registered_as_a_paid_profile(tmp_path, adapter, profile):
    with pytest.raises(ProductionError) as caught:
        adapter.observe_operation(
            stage_run_id="visual_1", output_dir=str(tmp_path),
            operation={"operation_id": "op_1", "provider_profile": profile, "provider_job_id": "job_1"},
        )
    assert caught.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value
