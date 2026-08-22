"""Frozen M2 approval and grant data-transfer contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from manju.production.events import sign_payload
from manju.production.models import M2_CONTRACT_VERSION, ProductionError, ReasonCode, fingerprint


_PUBLIC_PROVIDER_REQUEST_FIELDS = frozenset(
    {"prompt", "model", "size", "quality", "n", "response_format", "voice", "sample_rate"}
)


def _provider_request_binding(intent: dict[str, Any], code: ReasonCode) -> dict[str, Any]:
    if "provider_request" not in intent and "provider_request_fingerprint" not in intent:
        return {}
    request = intent.get("provider_request")
    request_fingerprint = intent.get("provider_request_fingerprint")
    if (
        not isinstance(request, dict) or not request or set(request) - _PUBLIC_PROVIDER_REQUEST_FIELDS
        or not all(isinstance(key, str) and isinstance(value, (str, int)) for key, value in request.items())
        or not isinstance(request_fingerprint, str)
    ):
        raise ProductionError(code.value, "provider request binding is invalid")
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if request_fingerprint != "sha256:" + hashlib.sha256(encoded).hexdigest():
        raise ProductionError(code.value, "provider request fingerprint is invalid")
    return {"provider_request": dict(request), "provider_request_fingerprint": request_fingerprint}


def _require(value: Any, name: str, code: ReasonCode) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductionError(code.value, f"missing or invalid {name}")
    return value


def _iso_utc(value: str, name: str, code: ReasonCode) -> str:
    _require(value, name, code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionError(code.value, f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise ProductionError(code.value, f"{name} must include timezone")
    return value


def _contractual_tariff(value: Any, *, maximum_amount: str, currency: str, code: ReasonCode) -> dict[str, str] | None:
    """Validate a signed internal price, never an upstream provider charge."""
    if value is None:
        return None
    legacy_keys = {"tariff_id", "amount", "currency", "tariff_sha256"}
    current_keys = {"tariff_id", "amount_minor", "currency", "amount_unit", "charge_policy", "pricing_scope", "tariff_sha256"}
    if not isinstance(value, dict) or (set(value) != legacy_keys and set(value) != current_keys):
        raise ProductionError(code.value, "contractual tariff is invalid")
    legacy = set(value) == legacy_keys
    amount_key = "amount" if legacy else "amount_minor"
    tariff_id, amount, tariff_currency, tariff_hash = (value.get(key) for key in ("tariff_id", amount_key, "currency", "tariff_sha256"))
    if not all(isinstance(item, str) and item for item in (tariff_id, amount, tariff_currency, tariff_hash)) or not amount.isdigit():
        raise ProductionError(code.value, "contractual tariff is incomplete")
    unsigned = {key: item for key, item in value.items() if key != "tariff_sha256"}
    if (
        tariff_hash != "sha256:" + fingerprint(unsigned)
        or tariff_currency != currency
        or int(amount) > int(maximum_amount)
        or (not legacy and (
            value.get("amount_unit") != "minor"
            or value.get("charge_policy") not in {"on_success", "per_attempt"}
            or value.get("pricing_scope") != "per_operation"
        ))
    ):
        raise ProductionError(code.value, "contractual tariff does not fit the approved budget")
    return dict(value)


def create_contractual_tariff(*, tariff_id: str, amount_minor: str, currency: str,
                              charge_policy: str = "on_success") -> dict[str, str]:
    """Create a public tariff in minor units for one successful operation."""
    unsigned = {"tariff_id": tariff_id, "amount_minor": amount_minor, "currency": currency,
                "amount_unit": "minor", "charge_policy": charge_policy, "pricing_scope": "per_operation"}
    return {**unsigned, "tariff_sha256": "sha256:" + fingerprint(unsigned)}


def contractual_tariff_usage(tariff: dict[str, str], outcome: str) -> dict[str, str]:
    """Produce the only permitted internal settlement usage for a contract tariff."""
    if "amount_minor" not in tariff:
        # M2.7 historical events were signed with this exact shape.  Keep
        # replay compatibility without silently treating new contracts as old.
        return {
            "actual_amount": str(tariff["amount"]), "currency": str(tariff["currency"]),
            "cost_status": "final", "cost_source": "contractual_tariff", "settlement_mode": "contractual_tariff",
            "tariff_id": str(tariff["tariff_id"]), "tariff_sha256": str(tariff["tariff_sha256"]),
            "cost_disclosure": "pre_agreed_price_not_upstream_actual_cost",
        }
    amount_minor = str(tariff.get("amount_minor", tariff.get("amount", "")))
    charge_policy = str(tariff.get("charge_policy", "per_attempt"))
    charged_amount = "0" if outcome == "failed" and charge_policy == "on_success" else amount_minor
    return {
        "actual_amount": charged_amount, "amount_unit": "minor", "currency": str(tariff["currency"]),
        "cost_status": "final", "cost_source": "contractual_tariff", "settlement_mode": "contractual_tariff",
        "tariff_id": str(tariff["tariff_id"]), "tariff_sha256": str(tariff["tariff_sha256"]),
        "tariff_amount_minor": amount_minor, "charge_policy": charge_policy,
        "pricing_scope": str(tariff.get("pricing_scope", "per_operation")),
        "cost_disclosure": "pre_agreed_price_not_upstream_actual_cost",
    }


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    project_id: str
    run_id: str
    stage: str
    stage_run_id: str
    kind: str
    state_fingerprint: str
    artifact_versions: tuple[dict[str, str], ...]
    operation_intents: tuple[dict[str, Any], ...]
    maximum_paid_calls: int
    maximum_amount: str
    currency: str
    provider_profile: str
    expires_at: str
    settlement_mode: str = "provider_evidence"
    contractual_tariff: dict[str, str] | None = None
    allowed_decisions: tuple[str, ...] = ("approve", "reject")
    reason_code: str = ReasonCode.PAID_VISUAL_BATCH_APPROVAL_REQUIRED.value

    def __post_init__(self) -> None:
        code = ReasonCode.APPROVAL_CONTRACT_INVALID
        for name in ("request_id", "project_id", "run_id", "stage", "stage_run_id", "kind", "state_fingerprint", "provider_profile"):
            _require(getattr(self, name), name, code)
        if (self.stage, self.kind) not in {
            ("visual", "paid_visual_batch"),
            ("voice_tts", "paid_voice_tts_batch"),
        }:
            raise ProductionError(code.value, "M2 only permits visual or voice-tts paid-batch approvals")
        if not self.artifact_versions or not all(
            isinstance(item, dict) and _require(item.get("artifact_id"), "artifact_id", code)
            and _require(item.get("version_id"), "version_id", code)
            for item in self.artifact_versions
        ):
            raise ProductionError(code.value, "artifact_versions must bind immutable artifacts")
        operation_ids: set[str] = set()
        for intent in self.operation_intents:
            if not isinstance(intent, dict):
                raise ProductionError(code.value, "operation intent must be an object")
            operation_id = _require(intent.get("operation_id"), "operation_id", code)
            _require(intent.get("input_fingerprint"), "input_fingerprint", code)
            _require(intent.get("kind"), "kind", code)
            _provider_request_binding(intent, code)
            if operation_id in operation_ids:
                raise ProductionError(code.value, "operation_id must be unique")
            operation_ids.add(operation_id)
        if not self.operation_intents or self.maximum_paid_calls != len(self.operation_intents):
            raise ProductionError(code.value, "maximum_paid_calls must exactly match intents")
        if self.maximum_paid_calls < 1 or not self.maximum_amount.isdigit() or not self.currency:
            raise ProductionError(code.value, "invalid budget")
        tariff = _contractual_tariff(self.contractual_tariff, maximum_amount=self.maximum_amount, currency=self.currency, code=code)
        if self.settlement_mode not in {"provider_evidence", "contractual_tariff"} or (
            (self.settlement_mode == "contractual_tariff") != (tariff is not None)
        ):
            raise ProductionError(code.value, "settlement mode and tariff must be explicitly consistent")
        if set(self.allowed_decisions) != {"approve", "reject"}:
            raise ProductionError(code.value, "allowed_decisions is fixed")
        _iso_utc(self.expires_at, "expires_at", code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": M2_CONTRACT_VERSION, "request_id": self.request_id,
            "project_id": self.project_id, "run_id": self.run_id, "stage": self.stage,
            "stage_run_id": self.stage_run_id, "kind": self.kind,
            "state_fingerprint": self.state_fingerprint,
            "artifact_versions": [dict(item) for item in self.artifact_versions],
            "operation_intents": [dict(item) for item in self.operation_intents],
            "maximum_paid_calls": self.maximum_paid_calls, "maximum_amount": self.maximum_amount,
            "currency": self.currency, "provider_profile": self.provider_profile,
            "expires_at": self.expires_at, "allowed_decisions": list(self.allowed_decisions),
            "reason_code": self.reason_code, "settlement_mode": self.settlement_mode,
            "contractual_tariff": dict(self.contractual_tariff) if self.contractual_tariff else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ApprovalRequest":
        if not isinstance(value, dict) or value.get("schema_version") != M2_CONTRACT_VERSION:
            raise ProductionError(ReasonCode.APPROVAL_CONTRACT_INVALID.value, "unsupported approval schema")
        values = {key: value.get(key) for key in (
                "request_id", "project_id", "run_id", "stage", "stage_run_id", "kind", "state_fingerprint",
                "maximum_paid_calls", "maximum_amount", "currency", "provider_profile", "expires_at", "reason_code",
            )}
        values["settlement_mode"] = value.get("settlement_mode", "provider_evidence")
        values["contractual_tariff"] = value.get("contractual_tariff")
        return cls(
            **values,
            artifact_versions=tuple(value.get("artifact_versions") or ()),
            operation_intents=tuple(value.get("operation_intents") or ()),
            allowed_decisions=tuple(value.get("allowed_decisions") or ()),
        )


@dataclass(frozen=True)
class Grant:
    grant_id: str
    request_id: str
    project_id: str
    run_id: str
    stage: str
    stage_run_id: str
    state_fingerprint: str
    operation_ids: tuple[str, ...]
    operation_bindings: tuple[dict[str, str], ...]
    artifact_versions: tuple[dict[str, str], ...]
    provider_profile: str
    maximum_paid_calls: int
    maximum_amount: str
    currency: str
    issued_by: str
    issued_at: str
    expires_at: str
    key_id: str
    signature: str
    status: str = "active"
    settlement_mode: str = "provider_evidence"
    contractual_tariff: dict[str, str] | None = None

    def unsigned_dict(self) -> dict[str, Any]:
        return {**self.to_dict(), "signature": ""}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": M2_CONTRACT_VERSION, "grant_id": self.grant_id, "request_id": self.request_id,
            "project_id": self.project_id, "run_id": self.run_id, "stage": self.stage, "stage_run_id": self.stage_run_id,
            "state_fingerprint": self.state_fingerprint, "operation_ids": list(self.operation_ids),
            "operation_bindings": [dict(item) for item in self.operation_bindings],
            "artifact_versions": [dict(item) for item in self.artifact_versions],
            "provider_profile": self.provider_profile, "maximum_paid_calls": self.maximum_paid_calls,
            "maximum_amount": self.maximum_amount, "currency": self.currency, "issued_by": self.issued_by,
            "issued_at": self.issued_at, "expires_at": self.expires_at, "key_id": self.key_id,
            "signature": self.signature, "status": self.status, "settlement_mode": self.settlement_mode,
            "contractual_tariff": dict(self.contractual_tariff) if self.contractual_tariff else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Grant":
        if not isinstance(value, dict) or value.get("schema_version") != M2_CONTRACT_VERSION:
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "unsupported grant schema")
        keys = (
            "grant_id", "request_id", "project_id", "run_id", "stage", "stage_run_id", "state_fingerprint", "provider_profile",
            "maximum_paid_calls", "maximum_amount", "currency", "issued_by", "issued_at", "expires_at",
            "key_id", "signature", "status", "settlement_mode", "contractual_tariff",
        )
        values = {key: value.get(key) for key in keys if key not in {"settlement_mode", "contractual_tariff"}}
        values["settlement_mode"] = value.get("settlement_mode", "provider_evidence")
        values["contractual_tariff"] = value.get("contractual_tariff")
        grant = cls(**values,
                    operation_ids=tuple(value.get("operation_ids") or ()),
                    operation_bindings=tuple(value.get("operation_bindings") or ()),
                    artifact_versions=tuple(value.get("artifact_versions") or ()))
        if not all((grant.grant_id, grant.request_id, grant.project_id, grant.run_id, grant.stage,
                    grant.stage_run_id, grant.provider_profile, grant.issued_by, grant.key_id, grant.signature)):
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "incomplete grant")
        _iso_utc(grant.issued_at, "issued_at", ReasonCode.GRANT_CONTRACT_INVALID)
        _iso_utc(grant.expires_at, "expires_at", ReasonCode.GRANT_CONTRACT_INVALID)
        tariff = _contractual_tariff(grant.contractual_tariff, maximum_amount=grant.maximum_amount, currency=grant.currency, code=ReasonCode.GRANT_CONTRACT_INVALID)
        if grant.settlement_mode not in {"provider_evidence", "contractual_tariff"} or ((grant.settlement_mode == "contractual_tariff") != (tariff is not None)):
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "grant settlement mode is invalid")
        return grant

    @classmethod
    def issue(cls, request: ApprovalRequest, *, grant_id: str, issued_by: str, issued_at: str,
              key_id: str, key: bytes) -> "Grant":
        values = dict(
            grant_id=grant_id, request_id=request.request_id, project_id=request.project_id, run_id=request.run_id,
            stage=request.stage, stage_run_id=request.stage_run_id, state_fingerprint=request.state_fingerprint,
            operation_ids=tuple(str(item["operation_id"]) for item in request.operation_intents),
            operation_bindings=tuple({"operation_id": str(item["operation_id"]),
                                      "input_fingerprint": str(item["input_fingerprint"]), "kind": str(item["kind"]),
                                      **_provider_request_binding(item, ReasonCode.APPROVAL_CONTRACT_INVALID)}
                                     for item in request.operation_intents),
            artifact_versions=request.artifact_versions, provider_profile=request.provider_profile,
            maximum_paid_calls=request.maximum_paid_calls, maximum_amount=request.maximum_amount,
            currency=request.currency, issued_by=issued_by, issued_at=issued_at, expires_at=request.expires_at,
            key_id=key_id, signature="", status="active", settlement_mode=request.settlement_mode,
            contractual_tariff=dict(request.contractual_tariff) if request.contractual_tariff else None,
        )
        if datetime.fromisoformat(issued_at.replace("Z", "+00:00")) > datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")):
            raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "cannot issue an expired grant")
        provisional = cls(**values)
        return cls(**{**values, "signature": sign_payload(provisional.unsigned_dict(), key)})

    def validate_against(self, request: ApprovalRequest, *, key: bytes, now: str | None = None) -> None:
        code = ReasonCode.GRANT_CONTRACT_INVALID
        if self.to_dict().get("schema_version") != M2_CONTRACT_VERSION or self.status != "active":
            raise ProductionError(code.value, "inactive or unsupported grant")
        if sign_payload(self.unsigned_dict(), key) != self.signature:
            raise ProductionError(code.value, "grant signature invalid")
        if not self.matches_request(request):
            raise ProductionError(code.value, "grant bindings differ from approval request")
        if now and datetime.fromisoformat(now.replace("Z", "+00:00")) > datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")):
            raise ProductionError(code.value, "grant expired")

    def matches_request(self, request: ApprovalRequest) -> bool:
        return (
            (self.request_id, self.project_id, self.run_id, self.stage, self.stage_run_id, self.state_fingerprint)
            == (request.request_id, request.project_id, request.run_id, request.stage, request.stage_run_id, request.state_fingerprint)
            and self.operation_ids == tuple(item["operation_id"] for item in request.operation_intents)
            and self.operation_bindings == tuple(
                {"operation_id": str(item["operation_id"]), "input_fingerprint": str(item["input_fingerprint"]),
                 "kind": str(item["kind"]),
                 **_provider_request_binding(item, ReasonCode.APPROVAL_CONTRACT_INVALID)}
                for item in request.operation_intents
            )
            and self.artifact_versions == request.artifact_versions
            and (self.provider_profile, self.maximum_paid_calls, self.maximum_amount, self.currency)
            == (request.provider_profile, request.maximum_paid_calls, request.maximum_amount, request.currency)
            and (self.settlement_mode, self.contractual_tariff) == (request.settlement_mode, request.contractual_tariff)
        )
