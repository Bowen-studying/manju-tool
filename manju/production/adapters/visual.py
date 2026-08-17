"""Visual-provider boundary with durable receipts and atomic artifact publication."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from manju.production.adapters.base import StageResult
from manju.production.approvals import ApprovalRequest, contractual_tariff_usage
from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import ProviderCapabilities, ProviderObservation, VisualProvider, VisualProviderRegistry
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_bytes, atomic_write_json, read_json


_MIME_EXTENSIONS = {
    "application/json": "mock_image.json",
    "application/octet-stream": "visual_result.bin",
    "image/png": "visual_result.png",
    "image/jpeg": "visual_result.jpg",
    "image/webp": "visual_result.webp",
}

_PROVIDER_REQUEST_FIELDS = frozenset({"prompt", "model", "size", "quality", "n", "response_format"})


def validate_provider_request(value: Any) -> dict[str, str | int]:
    """Allow only public generation controls; credentials never enter approval."""
    if not isinstance(value, dict) or not value or set(value) - _PROVIDER_REQUEST_FIELDS:
        raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "provider request must be a public approved descriptor")
    if not all(isinstance(key, str) and isinstance(item, (str, int)) for key, item in value.items()):
        raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "provider request descriptor is invalid")
    return dict(value)


def _public_artifact_payload(operation_id: str, provider_job_id: str) -> dict[str, str]:
    return {"schema_version": "1", "operation_id": operation_id, "provider_job_id": provider_job_id}


def _public_artifact_bytes(operation_id: str, provider_job_id: str) -> bytes:
    return json.dumps(_public_artifact_payload(operation_id, provider_job_id), ensure_ascii=False, indent=2).encode("utf-8")


def _fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _prompt_material(path: str, *, label: str) -> str:
    """Return public, deterministic artifact content for the approved prompt."""
    try:
        data = open(path, "rb").read()
    except OSError as exc:
        raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, f"{label} input is unavailable") from exc
    if len(data) > 64 * 1024:
        raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, f"{label} input is too large for an approved prompt")
    try:
        value = json.loads(data.decode("utf-8"))
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value,
                                  f"{label} input must be a public text or JSON artifact") from exc


def _render_provider_request(settings: dict[str, Any], *, storyboard_path: str = "", style_path: str = "") -> dict[str, str | int] | None:
    value = settings.get("provider_request")
    if value is None:
        return None
    request = validate_provider_request(value)
    if not storyboard_path:
        return request
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value,
                              "content-bound visual requests require a prompt template")
    material = [f"Storyboard artifact:\n{_prompt_material(storyboard_path, label='storyboard')}" ]
    if style_path:
        material.append(f"Style reference:\n{_prompt_material(style_path, label='style')}" )
    request["prompt"] = prompt.rstrip() + "\n\n" + "\n\n".join(material)
    return request


def _receipt_binding(value: dict[str, Any]) -> str:
    """Hash every observed field before it can enter signed settlement."""
    unsigned = {key: item for key, item in value.items() if key not in {"observation_sha256", "receipt_hmac", "receipt_hmac_key_id"}}
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_mac(value: dict[str, Any], key: bytes) -> str:
    unsigned = {item: content for item, content in value.items() if item != "receipt_hmac"}
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class MockVisualProvider:
    """Offline deterministic provider used only by acceptance tests."""

    outcomes: dict[str, str] | None = None
    jobs: dict[str, str] | None = None
    submit_counts: dict[str, int] | None = None
    idempotency_keys: dict[str, str] | None = None
    reconciles: list[str] | None = None
    default_outcome: str = "succeeded"
    capabilities = ProviderCapabilities(True, True, True, False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "jobs", self.jobs if self.jobs is not None else {})
        object.__setattr__(self, "submit_counts", self.submit_counts if self.submit_counts is not None else {})
        object.__setattr__(self, "idempotency_keys", self.idempotency_keys if self.idempotency_keys is not None else {})
        object.__setattr__(self, "reconciles", self.reconciles if self.reconciles is not None else [])

    def submit(self, operation_id: str, *, idempotency_key: str = "", request: dict[str, Any] | None = None) -> str:
        if not operation_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "mock operation id is missing")
        if idempotency_key:
            existing = self.idempotency_keys.get(operation_id)
            if existing and existing != idempotency_key:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "mock idempotency key changed")
            self.idempotency_keys[operation_id] = idempotency_key
        if operation_id in self.jobs:
            return self.jobs[operation_id]
        job_id = "mock-job-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:16]
        self.jobs[operation_id] = job_id
        self.submit_counts[operation_id] = self.submit_counts.get(operation_id, 0) + 1
        return job_id

    def reconcile(self, provider_job_id: str) -> ProviderObservation:
        self.reconciles.append(provider_job_id)
        operation_id = next((key for key, value in self.jobs.items() if value == provider_job_id), "")
        outcome = (self.outcomes or {}).get(operation_id, self.default_outcome)
        if outcome == "outcome_unknown":
            return ProviderObservation(
                outcome=outcome, provider_job_id=provider_job_id, actual_amount="", currency="",
                cost_status="unknown", cost_source="unknown",
            )
        if outcome != "succeeded":
            return ProviderObservation(
                outcome=outcome, provider_job_id=provider_job_id, cost_source="test_fixture",
            )
        artifact = _public_artifact_bytes(operation_id, provider_job_id)
        return ProviderObservation(
            outcome="succeeded", provider_job_id=provider_job_id, result_fingerprint=_fingerprint(artifact),
            artifact_bytes=artifact, artifact_media_type="application/json", usage={"calls": "1"}, cost_source="test_fixture",
        )


class VisualStageAdapter:
    """The only layer allowed to invoke a ``VisualProvider``.

    A successful reconcile is first made durable as a private byte file and a
    receipt.  Publishing uses that receipt only, so a restart after receipt
    creation cannot re-download or alter the provider's artifact.
    """

    contract_version = "visual-adapter-m3-4-v1"
    receipt_name = "visual_receipt.json"

    def __init__(self, provider: VisualProvider | None = None, *, provider_registry: VisualProviderRegistry | None = None):
        if provider is not None and provider_registry is not None:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "visual provider and registry are mutually exclusive")
        self.provider = provider or MockVisualProvider()
        self._provider_registry = provider_registry
        self._receipt_key: bytes | None = None
        self._receipt_key_id = ""

    def _provider_for_profile(self, profile: Any) -> VisualProvider:
        if self._provider_registry is not None:
            return self._provider_registry.resolve(str(profile))
        if profile not in {"", "mock"}:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "signed visual provider profile has no registry")
        return self.provider

    def _is_local_test_fixture(self, *, profile: Any, provider: VisualProvider) -> bool:
        """Keep nominal offline costs inside the built-in mock boundary only."""
        return (
            self._provider_registry is None
            and profile in {"", "mock"}
            and type(provider) is MockVisualProvider
        )

    def _validate_observation_cost(self, *, profile: Any, provider: VisualProvider, observed: ProviderObservation) -> None:
        is_local_test_fixture = self._is_local_test_fixture(profile=profile, provider=provider)
        # ``cost_source`` is supplied by the Provider.  It must never grant a
        # production Provider the local fixture exception, even if that
        # Provider claims verified_cost=True.
        if observed.cost_source == "test_fixture" and not is_local_test_fixture:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider cannot claim a local test fixture cost")
        if (
            observed.cost_status == "final" and not is_local_test_fixture
            and (not isinstance(getattr(provider, "capabilities", None), ProviderCapabilities) or not provider.capabilities.verified_cost)
        ):
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "provider final cost is not verifiable")

    def configure_receipt_signer(self, *, key_id: str, key: bytes) -> None:
        if not key_id or not isinstance(key, bytes) or not key:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "receipt signing key is unavailable")
        self._receipt_key, self._receipt_key_id = key, key_id

    def _seal_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        receipt["observation_sha256"] = _receipt_binding(receipt)
        if self._receipt_key is not None:
            receipt["receipt_hmac_key_id"] = self._receipt_key_id
            receipt["receipt_hmac"] = _receipt_mac(receipt, self._receipt_key)
        return receipt

    @staticmethod
    def artifact_result_fingerprint(operation_id: str, provider_job_id: str) -> str:
        return _fingerprint(_public_artifact_bytes(operation_id, provider_job_id))

    @staticmethod
    def _safe_output(output_dir: str, name: str) -> str:
        root = os.path.realpath(output_dir)
        candidate = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(candidate) != root:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual stage path escaped its directory")
        return candidate

    @staticmethod
    def _operation_fields(operation: dict[str, Any], *, require_result: bool = True) -> dict[str, str]:
        fields = ("operation_id", "provider_job_id", "result_fingerprint")
        value = {key: operation.get(key, "") for key in fields}
        required = fields if require_result else fields[:2]
        if not all(isinstance(value[key], str) and value[key] for key in required) or not isinstance(value["result_fingerprint"], str):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "visual operation binding is incomplete")
        return value

    def _pending_name(self, operation_id: str) -> str:
        return ".visual-pending-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest() + ".bin"

    def _receipt_path(self, output_dir: str) -> str:
        return self._safe_output(output_dir, self.receipt_name)

    def _load_receipt(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any]) -> dict[str, Any] | None:
        operation_fields = self._operation_fields(operation, require_result=False)
        path = self._receipt_path(output_dir)
        value = read_json(path)
        if value is None:
            return None
        artifact = value.get("artifact") if isinstance(value.get("artifact"), dict) else {}
        expected_keys = {"schema_version", "stage_run_id", "operation", "artifact", "usage", "cost", "observation_sha256"}
        if self._receipt_key is not None:
            expected_keys |= {"receipt_hmac_key_id", "receipt_hmac"}
        if (
            set(value) != expected_keys or value.get("schema_version") != "1" or value.get("stage_run_id") != stage_run_id
            or not isinstance(value.get("operation"), dict) or any(value["operation"].get(key) != operation_fields[key] for key in ("operation_id", "provider_job_id"))
            or set(artifact) != {"pending_path", "sha256", "media_type", "size"}
            or not isinstance(value.get("usage"), dict) or set(value.get("cost") if isinstance(value.get("cost"), dict) else {}) not in ({"actual_amount", "currency", "cost_status", "cost_source"}, {"actual_amount", "currency", "cost_status", "cost_source", "settlement_mode", "tariff_id", "tariff_sha256", "cost_disclosure"}, {"actual_amount", "amount_unit", "currency", "cost_status", "cost_source", "settlement_mode", "tariff_id", "tariff_sha256", "tariff_amount_minor", "charge_policy", "pricing_scope", "cost_disclosure"})
            or not isinstance(artifact.get("pending_path"), str) or os.path.basename(artifact["pending_path"]) != artifact["pending_path"]
            or artifact.get("media_type") not in _MIME_EXTENSIONS or not isinstance(artifact.get("sha256"), str)
            or not isinstance(artifact.get("size"), int) or artifact["size"] < 1
            or not isinstance(value.get("observation_sha256"), str) or value["observation_sha256"] != _receipt_binding(value)
            or (self._receipt_key is not None and (value.get("receipt_hmac_key_id") != self._receipt_key_id
                or not isinstance(value.get("receipt_hmac"), str) or not hmac.compare_digest(value["receipt_hmac"], _receipt_mac(value, self._receipt_key))))
        ):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual receipt is invalid")
        pending = self._safe_output(output_dir, artifact["pending_path"])
        if not os.path.isfile(pending) or os.path.getsize(pending) != artifact["size"] or sha256_file(pending) != artifact["sha256"]:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual receipt artifact is missing or changed")
        receipt_fingerprint = value["operation"].get("result_fingerprint")
        if not isinstance(receipt_fingerprint, str) or receipt_fingerprint != "sha256:" + artifact["sha256"]:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "receipt does not match signed result")
        if operation_fields["result_fingerprint"] and operation_fields["result_fingerprint"] != receipt_fingerprint:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "receipt does not match operation result")
        return value

    def settle_contractual_tariff_receipt(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any], tariff: dict[str, str]) -> None:
        """Replace the unknown manual cost with the Grant-frozen contractual price."""
        receipt = self._load_receipt(stage_run_id=stage_run_id, output_dir=output_dir, operation=operation)
        if receipt is None:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "manual result receipt is unavailable")
        expected_cost = contractual_tariff_usage(tariff, str(operation.get("outcome", "")))
        if receipt["cost"] == expected_cost:
            return
        if receipt["cost"].get("cost_status") != "unknown":
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "manual result receipt was already settled")
        receipt["cost"] = expected_cost
        atomic_write_json(self._receipt_path(output_dir), self._seal_receipt(receipt))

    def map_published_approval(self, record: dict[str, Any]) -> ApprovalRequest:
        if not isinstance(record, dict) or set(record) != {"published_approval", "adapter_contract_version"}:
            raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "visual adapter only accepts published_approval")
        if record.get("adapter_contract_version") not in {self.contract_version, "visual-adapter-m2-3-v1"}:
            raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "visual adapter contract version mismatch")
        value = record["published_approval"]
        if not isinstance(value, dict) or "state" in value or "paid_ledger" in value:
            raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "private visual state is not a top-level authority")
        return ApprovalRequest.from_dict(value)

    def plan(self, *, project_id: str, run_id: str, stage_run_id: str, output_dir: str,
              storyboard_artifact: dict[str, str], settings: dict[str, Any],
              artifact_versions: tuple[dict[str, str], ...] = (), storyboard_path: str = "",
              style_path: str = "") -> ApprovalRequest:
        os.makedirs(output_dir, exist_ok=True)
        operation_id = "visual-" + run_id.removeprefix("run_")
        provider_request = _render_provider_request(
            settings, storyboard_path=storyboard_path, style_path=style_path,
        )
        request_fingerprint = ""
        if provider_request is not None:
            encoded_request = json.dumps(provider_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            request_fingerprint = "sha256:" + hashlib.sha256(encoded_request).hexdigest()
        approved_artifacts = (dict(storyboard_artifact), *tuple(dict(item) for item in artifact_versions))
        input_material = json.dumps({
            "operation_id": operation_id, "artifacts": approved_artifacts,
            "provider_request_fingerprint": request_fingerprint,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request = ApprovalRequest(
            request_id="approval-" + run_id.removeprefix("run_"), project_id=project_id, run_id=run_id,
            stage="visual", stage_run_id=stage_run_id, kind="paid_visual_batch",
            state_fingerprint="sha256:" + hashlib.sha256(json.dumps(approved_artifacts, sort_keys=True).encode("utf-8")).hexdigest(),
            artifact_versions=approved_artifacts,
            operation_intents=({"operation_id": operation_id, "input_fingerprint": "sha256:" + hashlib.sha256(input_material.encode("utf-8")).hexdigest(), "kind": str(settings.get("operation_kind", "mock_image")),
                                **({"provider_request": provider_request, "provider_request_fingerprint": request_fingerprint} if provider_request is not None else {})},),
             maximum_paid_calls=settings["maximum_paid_calls"], maximum_amount=settings["maximum_amount"],
            currency=str(settings.get("currency", "USD")), provider_profile=str(settings.get("provider_profile", "mock")), expires_at="2099-01-01T00:00:00Z",
            settlement_mode=str(settings.get("settlement_mode", "provider_evidence")),
            contractual_tariff=settings.get("contractual_tariff"),
        )
        atomic_write_json(self._safe_output(output_dir, "visual_plan.json"), {"published_approval": request.to_dict(), "adapter_contract_version": self.contract_version})
        return request

    def submit_operation(self, operation: dict[str, Any]) -> str:
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "visual operation id is missing")
        provider = self._provider_for_profile(operation.get("provider_profile", "mock"))
        capabilities = getattr(provider, "capabilities", None)
        if not isinstance(capabilities, ProviderCapabilities) or not capabilities.automatic_recovery_safe:
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "provider cannot safely support automatic paid recovery")
        request = operation.get("provider_request")
        if request is None:
            request = {}
        if operation.get("provider_profile") != "mock" and not request:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "paid provider requires an approved request descriptor")
        request = validate_provider_request(request) if request else {}
        return provider.submit(operation_id, idempotency_key=operation_id, request=request)

    def observe_operation(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any]) -> ProviderObservation:
        """Return a cached success receipt or reconcile once and persist it."""
        profile = operation.get("provider_profile", "mock")
        cached = self._load_receipt(stage_run_id=stage_run_id, output_dir=output_dir, operation=operation)
        if cached is not None:
            artifact = cached["artifact"]
            with open(self._safe_output(output_dir, artifact["pending_path"]), "rb") as handle:
                data = handle.read()
            cost = cached["cost"]
            observed = ProviderObservation("succeeded", operation["provider_job_id"], cached["operation"]["result_fingerprint"], data,
                                           artifact["media_type"], str(cost["actual_amount"]), str(cost["currency"]),
                                           dict(cached["usage"]), str(cost["cost_status"]), str(cost["cost_source"]))
            # A signed receipt is already authoritative for real final cost;
            # only enforce that an imported receipt cannot mislabel a paid
            # profile as the local fixture without a provider call.
            if observed.cost_source == "test_fixture" and not self._is_local_test_fixture(profile=profile, provider=self.provider):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "receipt cannot claim a local test fixture cost")
            return observed
        provider = self._provider_for_profile(profile)
        observed = provider.reconcile(str(operation.get("provider_job_id", "")))
        if observed.provider_job_id != operation.get("provider_job_id"):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider observation job does not match operation")
        self._validate_observation_cost(profile=profile, provider=provider, observed=observed)
        if observed.outcome != "succeeded":
            return observed
        if observed.artifact_media_type not in _MIME_EXTENSIONS:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact media type is not supported")
        if observed.result_fingerprint != _fingerprint(observed.artifact_bytes):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider artifact commitment is invalid")
        pending_name = self._pending_name(str(operation["operation_id"]))
        pending = self._safe_output(output_dir, pending_name)
        atomic_write_bytes(pending, observed.artifact_bytes)
        receipt = {
            "schema_version": "1", "stage_run_id": stage_run_id,
            "operation": {"operation_id": operation["operation_id"], "provider_job_id": operation["provider_job_id"], "result_fingerprint": observed.result_fingerprint},
            "artifact": {"pending_path": pending_name, "sha256": sha256_file(pending), "media_type": observed.artifact_media_type, "size": len(observed.artifact_bytes)},
            "usage": observed.usage or {},
            "cost": {"actual_amount": observed.actual_amount, "currency": observed.currency, "cost_status": observed.cost_status, "cost_source": observed.cost_source},
        }
        atomic_write_json(self._receipt_path(output_dir), self._seal_receipt(receipt))
        return observed

    def publish_result(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any]) -> StageResult:
        receipt = self._load_receipt(stage_run_id=stage_run_id, output_dir=output_dir, operation=operation)
        if receipt is None:
            # Old M2.1 offline reconciliation records predate receipts.  They
            # can be upgraded only when their signed fingerprint proves the
            # deterministic mock payload; arbitrary historical bytes are never
            # reconstructed here.
            fields = self._operation_fields(operation)
            legacy = _public_artifact_bytes(fields["operation_id"], fields["provider_job_id"])
            if fields["result_fingerprint"] != _fingerprint(legacy):
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual result receipt is required before publish")
            pending_name = self._pending_name(fields["operation_id"])
            pending = self._safe_output(output_dir, pending_name)
            atomic_write_bytes(pending, legacy)
            legacy_receipt = {"schema_version": "1", "stage_run_id": stage_run_id, "operation": fields,
                              "artifact": {"pending_path": pending_name, "sha256": sha256_file(pending), "media_type": "application/json", "size": len(legacy)},
                              "usage": {"calls": "1"}, "cost": {"actual_amount": "0", "currency": "USD", "cost_status": "final", "cost_source": "test_fixture"}}
            atomic_write_json(self._receipt_path(output_dir), self._seal_receipt(legacy_receipt))
            receipt = self._load_receipt(stage_run_id=stage_run_id, output_dir=output_dir, operation=operation)
        artifact_data = receipt["artifact"]
        pending = self._safe_output(output_dir, artifact_data["pending_path"])
        artifact_name = _MIME_EXTENSIONS[artifact_data["media_type"]]
        artifact = self._safe_output(output_dir, artifact_name)
        with open(pending, "rb") as handle:
            atomic_write_bytes(artifact, handle.read())
        if sha256_file(artifact) != artifact_data["sha256"]:
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "published bytes do not match receipt")
        authority = self._safe_output(output_dir, "visual_authority.json")
        cost = receipt["cost"]
        settlement = {key: cost[key] for key in (
            "cost_source", "settlement_mode", "tariff_id", "tariff_sha256", "amount_unit",
            "tariff_amount_minor", "charge_policy", "pricing_scope", "cost_disclosure",
        ) if key in cost}
        atomic_write_json(authority, {
            "schema_version": "2", "stage_run_id": stage_run_id, "artifact": {"path": artifact_name, "sha256": sha256_file(artifact), "media_type": artifact_data["media_type"]},
            "receipt": {"path": self.receipt_name, "sha256": sha256_file(self._receipt_path(output_dir))},
            "operation": self._operation_fields(operation), "adapter_contract_version": self.contract_version, "settlement": settlement,
        })
        return self.inspect(stage_run_id=stage_run_id, output_dir=output_dir) or StageResult(status="failed", stage_run_id=stage_run_id)

    def inspect(self, *, stage_run_id: str, output_dir: str) -> StageResult | None:
        authority = self._safe_output(output_dir, "visual_authority.json")
        value = read_json(authority)
        if value is None:
            return None
        artifact = value.get("artifact") if isinstance(value.get("artifact"), dict) else {}
        receipt_ref = value.get("receipt") if isinstance(value.get("receipt"), dict) else {}
        operation = value.get("operation") if isinstance(value.get("operation"), dict) else {}
        if (
            set(value) != {"schema_version", "stage_run_id", "artifact", "receipt", "operation", "adapter_contract_version", "settlement"}
            or value.get("schema_version") != "2" or value.get("stage_run_id") != stage_run_id
            or value.get("adapter_contract_version") not in {self.contract_version, "visual-adapter-m2-3-v1"}
            or set(artifact) != {"path", "sha256", "media_type"} or artifact.get("media_type") not in _MIME_EXTENSIONS
            or artifact.get("path") != _MIME_EXTENSIONS[artifact.get("media_type")] or set(receipt_ref) != {"path", "sha256"}
            or receipt_ref.get("path") != self.receipt_name or set(operation) != {"operation_id", "provider_job_id", "result_fingerprint"}
            or not isinstance(value.get("settlement"), dict)
        ):
            return StageResult(status="failed", stage_run_id=stage_run_id, reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value, message="visual public authority invalid")
        output = self._safe_output(output_dir, artifact["path"])
        receipt_path = self._receipt_path(output_dir)
        if not os.path.isfile(output) or not os.path.isfile(receipt_path) or artifact["sha256"] != sha256_file(output) or receipt_ref["sha256"] != sha256_file(receipt_path) or operation["result_fingerprint"] != "sha256:" + sha256_file(output):
            return StageResult(status="failed", stage_run_id=stage_run_id, reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value, message="visual authority binding failed")
        try:
            receipt = self._load_receipt(stage_run_id=stage_run_id, output_dir=output_dir, operation=operation)
            cost = receipt["cost"] if receipt else {}
            expected_settlement = {key: cost[key] for key in (
                "cost_source", "settlement_mode", "tariff_id", "tariff_sha256", "amount_unit",
                "tariff_amount_minor", "charge_policy", "pricing_scope", "cost_disclosure",
            ) if key in cost}
            if value.get("settlement") != expected_settlement:
                raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "visual authority settlement binding failed")
        except ProductionError:
            return StageResult(status="failed", stage_run_id=stage_run_id, reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value, message="visual receipt binding failed")
        return StageResult(status="completed", stage_run_id=stage_run_id,
                           artifacts=({"path": output, "version_id": "sha256:" + sha256_file(output)},),
                           authority_path=authority, authority_hash=sha256_file(authority),
                           authority_files=({"path": authority, "sha256": sha256_file(authority)}, {"path": receipt_path, "sha256": sha256_file(receipt_path)}))

    def complete(self, *, stage_run_id: str, output_dir: str, operation: dict[str, Any]) -> StageResult:
        """Compatibility helper for offline callers; production uses observe/publish."""
        fields = self._operation_fields(operation)
        data = _public_artifact_bytes(fields["operation_id"], fields["provider_job_id"])
        if fields["result_fingerprint"] != _fingerprint(data):
            raise ProductionError(ReasonCode.STAGE_INTEGRITY_FAILED.value, "signed visual result does not commit legacy mock bytes")
        pending = self._safe_output(output_dir, self._pending_name(fields["operation_id"]))
        atomic_write_bytes(pending, data)
        legacy_receipt = {"schema_version": "1", "stage_run_id": stage_run_id, "operation": fields,
                          "artifact": {"pending_path": os.path.basename(pending), "sha256": sha256_file(pending), "media_type": "application/json", "size": len(data)},
                          "usage": {"calls": "1"}, "cost": {"actual_amount": "0", "currency": "USD", "cost_status": "final", "cost_source": "test_fixture"}}
        atomic_write_json(self._receipt_path(output_dir), self._seal_receipt(legacy_receipt))
        return self.publish_result(stage_run_id=stage_run_id, output_dir=output_dir, operation=fields)
