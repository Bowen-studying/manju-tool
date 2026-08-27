"""Append-only, hash-chained ProductionRun event storage."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Iterable, Protocol

from manju.production.models import (
    EVENT_VERSION,
    ProductionError,
    ReasonCode,
    canonical_json,
    utc_now,
)


GENESIS_HASH = "0" * 64
SENSITIVE_EVENT_TYPES = frozenset({
    "approval_requested", "approval_approved", "approval_rejected", "grant_issued",
    "grant_revoked", "call_reserved", "call_submitted", "call_settled", "call_reconciled",
    "manual_dispatch_prepared", "manual_result_imported", "manual_cost_reconciled", "manual_contractual_tariff_settled",
    "m8_visual_evidence_attested",
})


class HmacKeyProvider(Protocol):
    def get_key(self, key_id: str) -> bytes | None: ...


def _signature_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key not in {"event_hash", "hmac"}}


def sign_payload(value: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, canonical_json(value).encode("utf-8"), hashlib.sha256).hexdigest()


class EventStore:
    def __init__(self, path: str, *, key_provider: HmacKeyProvider | None = None):
        self.path = os.path.abspath(path)
        self.key_provider = key_provider

    def read(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        events: list[dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.endswith("\n"):
                        raise ValueError(f"truncated event at line {line_number}")
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError(f"event {line_number} is not an object")
                    events.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProductionError(
                ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                f"无法读取项目事件链: {exc}",
            ) from exc
        self.verify(events, key_provider=self.key_provider)
        return events

    @staticmethod
    def verify(events: Iterable[dict[str, Any]], *, key_provider: HmacKeyProvider | None = None) -> None:
        previous_hash = GENESIS_HASH
        expected_sequence = 1
        for event in events:
            if event.get("event_version") != EVENT_VERSION:
                raise ProductionError(
                    ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value,
                    f"不支持事件版本: {event.get('event_version')!r}",
                )
            if event.get("sequence") != expected_sequence:
                raise ProductionError(
                    ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                    f"事件序号不连续: expected {expected_sequence}",
                )
            if event.get("previous_hash") != previous_hash:
                raise ProductionError(
                    ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                    f"事件前序哈希不匹配: sequence {expected_sequence}",
                )
            recorded_hash = event.get("event_hash")
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            calculated_hash = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
            if recorded_hash != calculated_hash:
                raise ProductionError(
                    ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                    f"事件内容哈希不匹配: sequence {expected_sequence}",
                )
            if event.get("event_type") in SENSITIVE_EVENT_TYPES:
                key_id = str(event.get("key_id", ""))
                signature = str(event.get("hmac", ""))
                if not key_id or not signature:
                    raise ProductionError(ReasonCode.SENSITIVE_EVENT_SIGNATURE_INVALID.value)
                if key_provider is None:
                    previous_hash = recorded_hash
                    expected_sequence += 1
                    continue
                key = key_provider.get_key(key_id)
                if not key:
                    raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value)
                if not signature or not hmac.compare_digest(
                    signature, sign_payload(_signature_payload(event), key)
                ):
                    raise ProductionError(ReasonCode.SENSITIVE_EVENT_SIGNATURE_INVALID.value)
                if event.get("event_type") == "grant_issued":
                    from manju.production.approvals import Grant
                    grant = Grant.from_dict((event.get("payload") or {}).get("grant", {}))
                    if grant.key_id != key_id or not hmac.compare_digest(
                        grant.signature, sign_payload(grant.unsigned_dict(), key)
                    ):
                        raise ProductionError(ReasonCode.GRANT_CONTRACT_INVALID.value, "grant signature invalid")
            previous_hash = recorded_hash
            expected_sequence += 1

    def append(
        self,
        event_type: str,
        *,
        project_id: str,
        run_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = self.read()
        event = {
            "event_version": EVENT_VERSION,
            "sequence": len(events) + 1,
            "occurred_at": utc_now(),
            "event_type": event_type,
            "project_id": project_id,
            "run_id": run_id,
            "payload": payload or {},
            "previous_hash": events[-1]["event_hash"] if events else GENESIS_HASH,
        }
        if event_type in SENSITIVE_EVENT_TYPES:
            key_id = str((payload or {}).get("key_id", ""))
            key = self.key_provider.get_key(key_id) if self.key_provider and key_id else None
            if not key:
                raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value)
            event["key_id"] = key_id
            event["hmac"] = sign_payload(_signature_payload(event), key)
        event["event_hash"] = hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event
