"""Signed, offline-transfer contracts for a manually executed synchronous provider.

The project service creates and verifies these records.  A separate worker may
perform one explicitly confirmed POST, but a project never retries that POST.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Any

from manju.production.events import sign_payload
from manju.production.models import ProductionError, ReasonCode, canonical_json, utc_now


MANUAL_SCHEMA_VERSION = "manual-sync-m2-6-v1"
_MEDIA_TYPES = frozenset({"application/json", "application/octet-stream", "image/png", "image/jpeg", "image/webp"})


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _invalid(message: str) -> ProductionError:
    return ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, message)


def _require(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"manual package missing {name}")
    return value


def _name_only(value: Any, name: str) -> str:
    value = _require(value, name)
    if os.path.basename(value) != value or value in {".", ".."}:
        raise _invalid(f"manual package {name} must be a file name")
    return value


@dataclass(frozen=True)
class ManualDispatchPackage:
    package_id: str
    project_id: str
    run_id: str
    stage_run_id: str
    operation_id: str
    grant_id: str
    provider_profile: str
    operation_kind: str
    input_fingerprint: str
    provider_request: dict[str, Any]
    maximum_amount: str
    currency: str
    claim_token: str
    created_at: str
    key_id: str
    signature: str = ""

    def __post_init__(self) -> None:
        for name in ("package_id", "project_id", "run_id", "stage_run_id", "operation_id", "grant_id", "provider_profile", "operation_kind", "input_fingerprint", "claim_token", "created_at", "key_id"):
            _require(getattr(self, name), name)
        if len(self.claim_token) < 32 or not self.maximum_amount.isdigit() or not _require(self.currency, "currency"):
            raise _invalid("manual dispatch budget or claim token is invalid")
        if not isinstance(self.provider_request, dict) or not self.provider_request:
            raise _invalid("manual dispatch request is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": MANUAL_SCHEMA_VERSION, "package_id": self.package_id, "project_id": self.project_id,
                "run_id": self.run_id, "stage_run_id": self.stage_run_id, "operation_id": self.operation_id,
                "grant_id": self.grant_id, "provider_profile": self.provider_profile, "operation_kind": self.operation_kind,
                "input_fingerprint": self.input_fingerprint, "provider_request": dict(self.provider_request),
                "maximum_amount": self.maximum_amount, "currency": self.currency, "claim_token": self.claim_token,
                "created_at": self.created_at, "key_id": self.key_id, "signature": self.signature}

    def unsigned_dict(self) -> dict[str, Any]:
        return {**self.to_dict(), "signature": ""}

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    def sign(self, key: bytes) -> "ManualDispatchPackage":
        return ManualDispatchPackage(**{**self.__dict__, "signature": sign_payload(self.unsigned_dict(), key)})

    def verify(self, key: bytes) -> None:
        if not self.signature or not hmac.compare_digest(self.signature, sign_payload(self.unsigned_dict(), key)):
            raise _invalid("manual dispatch signature is invalid")

    @classmethod
    def from_dict(cls, value: Any) -> "ManualDispatchPackage":
        names = {"schema_version", "package_id", "project_id", "run_id", "stage_run_id", "operation_id", "grant_id", "provider_profile", "operation_kind", "input_fingerprint", "provider_request", "maximum_amount", "currency", "claim_token", "created_at", "key_id", "signature"}
        if not isinstance(value, dict) or set(value) != names or value.get("schema_version") != MANUAL_SCHEMA_VERSION:
            raise _invalid("manual dispatch schema is invalid")
        return cls(**{name: value[name] for name in names - {"schema_version"}})

    @classmethod
    def create(cls, *, project_id: str, run_id: str, stage_run_id: str, operation_id: str, grant_id: str,
               provider_profile: str, operation_kind: str, input_fingerprint: str, provider_request: dict[str, Any],
               maximum_amount: str, currency: str, key_id: str) -> "ManualDispatchPackage":
        return cls("manual-" + secrets.token_hex(16), project_id, run_id, stage_run_id, operation_id, grant_id,
                   provider_profile, operation_kind, input_fingerprint, dict(provider_request), maximum_amount, currency,
                   secrets.token_urlsafe(32), utc_now(), key_id)


@dataclass(frozen=True)
class ManualResultPackage:
    dispatch_sha256: str
    package_id: str
    operation_id: str
    claim_token: str
    started_at: str
    finished_at: str
    outcome: str
    artifact_path: str = ""
    artifact_sha256: str = ""
    artifact_media_type: str = ""
    artifact_size: int = 0
    raw_response_sha256: str = ""
    worker_id: str = ""
    key_id: str = ""
    signature: str = ""

    def __post_init__(self) -> None:
        for name in ("dispatch_sha256", "package_id", "operation_id", "claim_token", "started_at", "finished_at", "raw_response_sha256", "worker_id", "key_id"):
            _require(getattr(self, name), name)
        if self.outcome not in {"succeeded", "failed", "outcome_unknown"}:
            raise _invalid("manual result outcome is invalid")
        artifact_fields = (self.artifact_path, self.artifact_sha256, self.artifact_media_type, self.artifact_size)
        if self.outcome == "succeeded":
            if not _name_only(self.artifact_path, "artifact_path") or len(self.artifact_sha256) != 64 or self.artifact_media_type not in _MEDIA_TYPES or not isinstance(self.artifact_size, int) or self.artifact_size < 1:
                raise _invalid("manual result artifact is invalid")
        elif any(artifact_fields):
            raise _invalid("non-success manual result cannot contain an artifact")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": MANUAL_SCHEMA_VERSION, "dispatch_sha256": self.dispatch_sha256, "package_id": self.package_id,
                "operation_id": self.operation_id, "claim_token": self.claim_token, "started_at": self.started_at,
                "finished_at": self.finished_at, "outcome": self.outcome, "artifact_path": self.artifact_path,
                "artifact_sha256": self.artifact_sha256, "artifact_media_type": self.artifact_media_type,
                "artifact_size": self.artifact_size, "raw_response_sha256": self.raw_response_sha256,
                "worker_id": self.worker_id, "key_id": self.key_id, "signature": self.signature}

    def unsigned_dict(self) -> dict[str, Any]:
        return {**self.to_dict(), "signature": ""}

    def sign(self, key: bytes) -> "ManualResultPackage":
        return ManualResultPackage(**{**self.__dict__, "signature": sign_payload(self.unsigned_dict(), key)})

    def verify(self, key: bytes) -> None:
        if not self.signature or not hmac.compare_digest(self.signature, sign_payload(self.unsigned_dict(), key)):
            raise _invalid("manual result signature is invalid")

    @classmethod
    def from_dict(cls, value: Any) -> "ManualResultPackage":
        names = {"schema_version", "dispatch_sha256", "package_id", "operation_id", "claim_token", "started_at", "finished_at", "outcome", "artifact_path", "artifact_sha256", "artifact_media_type", "artifact_size", "raw_response_sha256", "worker_id", "key_id", "signature"}
        if not isinstance(value, dict) or set(value) != names or value.get("schema_version") != MANUAL_SCHEMA_VERSION:
            raise _invalid("manual result schema is invalid")
        return cls(**{name: value[name] for name in names - {"schema_version"}})


@dataclass(frozen=True)
class ManualBillingEvidence:
    dispatch_sha256: str
    operation_id: str
    claim_token: str
    outcome: str
    actual_amount: str
    currency: str
    provider_reference: str
    evidence_path: str
    evidence_sha256: str
    reviewer: str
    reviewed_at: str
    key_id: str
    signature: str = ""

    def __post_init__(self) -> None:
        for name in ("dispatch_sha256", "operation_id", "claim_token", "actual_amount", "currency", "provider_reference", "reviewer", "reviewed_at", "key_id"):
            _require(getattr(self, name), name)
        if self.outcome not in {"succeeded", "failed"} or not self.actual_amount.isdigit():
            raise _invalid("manual billing outcome or amount is invalid")
        _name_only(self.evidence_path, "evidence_path")
        if len(self.evidence_sha256) != 64:
            raise _invalid("manual billing evidence hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": MANUAL_SCHEMA_VERSION, **self.__dict__}

    def unsigned_dict(self) -> dict[str, Any]:
        return {**self.to_dict(), "signature": ""}

    def sign(self, key: bytes) -> "ManualBillingEvidence":
        return ManualBillingEvidence(**{**self.__dict__, "signature": sign_payload(self.unsigned_dict(), key)})

    def verify(self, key: bytes) -> None:
        if not self.signature or not hmac.compare_digest(self.signature, sign_payload(self.unsigned_dict(), key)):
            raise _invalid("manual billing signature is invalid")

    @classmethod
    def from_dict(cls, value: Any) -> "ManualBillingEvidence":
        names = {"schema_version", "dispatch_sha256", "operation_id", "claim_token", "outcome", "actual_amount", "currency", "provider_reference", "evidence_path", "evidence_sha256", "reviewer", "reviewed_at", "key_id", "signature"}
        if not isinstance(value, dict) or set(value) != names or value.get("schema_version") != MANUAL_SCHEMA_VERSION:
            raise _invalid("manual billing schema is invalid")
        return cls(**{name: value[name] for name in names - {"schema_version"}})
