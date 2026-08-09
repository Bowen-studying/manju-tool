"""Immutable run identity separated from invocation compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import uuid

from manju.utils.runtime import content_fingerprint


IDENTITY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    invocation_contract_hash: str
    created_at: str
    run_kind: str = "production"
    parent_run_id: str = ""
    schema_version: int = IDENTITY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "invocation_contract_hash": self.invocation_contract_hash,
            "created_at": self.created_at,
            "run_kind": self.run_kind,
            "parent_run_id": self.parent_run_id,
        }


def invocation_contract_hash(contract: dict) -> str:
    payload = {
        key: value for key, value in contract.items()
        if key not in {"fingerprint", "created_at", "migrated_at"}
    }
    return content_fingerprint(payload, length=32)


def create_run_identity(
    contract: dict,
    *,
    run_kind: str = "production",
    parent_run_id: str = "",
    run_id: str | None = None,
    created_at: str | None = None,
) -> RunIdentity:
    return RunIdentity(
        run_id=run_id or uuid.uuid4().hex,
        invocation_contract_hash=invocation_contract_hash(contract),
        created_at=created_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        run_kind=run_kind,
        parent_run_id=parent_run_id,
    )


def identity_from_dict(value: dict | None) -> RunIdentity | None:
    if not isinstance(value, dict):
        return None
    run_id = str(value.get("run_id", ""))
    contract_hash = str(value.get("invocation_contract_hash", ""))
    if not run_id or not contract_hash:
        return None
    return RunIdentity(
        run_id=run_id,
        invocation_contract_hash=contract_hash,
        created_at=str(value.get("created_at", "")),
        run_kind=str(value.get("run_kind", "production")),
        parent_run_id=str(value.get("parent_run_id", "")),
        schema_version=int(value.get("schema_version", IDENTITY_SCHEMA_VERSION)),
    )


def compatibility_report(identity: RunIdentity, contract: dict) -> dict:
    actual = invocation_contract_hash(contract)
    return {
        "compatible": actual == identity.invocation_contract_hash,
        "run_id": identity.run_id,
        "stored_invocation_contract_hash": identity.invocation_contract_hash,
        "current_invocation_contract_hash": actual,
        "run_identity_unchanged": True,
    }
