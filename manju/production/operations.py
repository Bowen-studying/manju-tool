"""Deterministic lifecycle for one externally reconciled paid operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from manju.production.models import M2_CONTRACT_VERSION, ProductionError, ReasonCode


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    grant_id: str
    kind: str
    input_fingerprint: str
    provider_profile: str
    reservation_amount: str = "0"
    currency: str = "USD"
    status: str = "reserved"
    provider_job_id: str = ""
    result_fingerprint: str = ""
    outcome: str = ""
    usage: dict[str, str] | None = None
    provider_request: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not all((self.operation_id, self.grant_id, self.kind, self.input_fingerprint, self.provider_profile)):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "operation bindings are required")
        if self.status not in {"reserved", "submitted", "settled"} or not self.reservation_amount.isdigit() or not self.currency:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "invalid operation status")
        if self.status == "submitted" and not self.provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "submitted operation needs provider_job_id")
        if self.status == "settled" and self.outcome not in {"succeeded", "failed", "outcome_unknown"}:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "settled operation needs outcome")
        if self.usage is not None and (
            not isinstance(self.usage, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in self.usage.items())
        ):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "operation usage is invalid")
        if self.provider_request is not None and not isinstance(self.provider_request, dict):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "operation request is invalid")

    def to_dict(self) -> dict[str, str]:
        return {"schema_version": M2_CONTRACT_VERSION, "operation_id": self.operation_id, "grant_id": self.grant_id,
                "kind": self.kind, "input_fingerprint": self.input_fingerprint, "provider_profile": self.provider_profile,
                "reservation_amount": self.reservation_amount, "currency": self.currency, "status": self.status,
                "provider_job_id": self.provider_job_id, "result_fingerprint": self.result_fingerprint,
                "outcome": self.outcome, "usage": self.usage or {}, "provider_request": self.provider_request or {}}

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "OperationRecord":
        if not isinstance(value, dict) or value.get("schema_version") != M2_CONTRACT_VERSION:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "unsupported operation schema")
        args = {key: value.get(key, "") for key in (
            "operation_id", "grant_id", "kind", "input_fingerprint", "provider_profile",
            "reservation_amount", "currency", "status", "provider_job_id", "result_fingerprint", "outcome", "usage", "provider_request",
        )}
        if not isinstance(args["usage"], dict):
            args["usage"] = None if args["usage"] == "" else args["usage"]
        if args["provider_request"] == {}:
            args["provider_request"] = None
        elif not isinstance(args["provider_request"], dict):
            args["provider_request"] = None if args["provider_request"] == "" else args["provider_request"]
        return cls(**args)

    def submit(self, provider_job_id: str) -> "OperationRecord":
        if self.status != "reserved" or not provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "only reserved operation may be submitted")
        return OperationRecord(**{**self.__dict__, "status": "submitted", "provider_job_id": provider_job_id})

    def settle(self, *, outcome: str, result_fingerprint: str = "", usage: dict[str, str] | None = None) -> "OperationRecord":
        if self.status != "submitted":
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "only submitted operation may settle")
        return OperationRecord(**{**self.__dict__, "status": "settled", "outcome": outcome,
                                  "result_fingerprint": result_fingerprint, "usage": usage or {}})

    def reconcile(self, *, outcome: str, result_fingerprint: str = "", usage: dict[str, str] | None = None) -> "OperationRecord":
        if self.status != "settled" or self.outcome != "outcome_unknown" or outcome not in {"succeeded", "failed"}:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "only unknown settled operation may reconcile")
        return OperationRecord(**{**self.__dict__, "outcome": outcome, "result_fingerprint": result_fingerprint,
                                  "usage": usage or {}})
