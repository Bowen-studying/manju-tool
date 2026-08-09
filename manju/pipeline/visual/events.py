"""Versioned, hash-chained visual workflow events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import uuid

from manju.utils.runtime import content_fingerprint


EVENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class VisualEvent:
    sequence: int
    event_type: str
    payload: dict
    created_at: str
    event_id: str
    previous_checksum: str = ""
    checksum: str = ""
    schema_version: int = EVENT_SCHEMA_VERSION

    def unsigned_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "previous_checksum": self.previous_checksum,
            "payload": self.payload,
        }

    def with_checksum(self) -> "VisualEvent":
        checksum = content_fingerprint(self.unsigned_dict(), length=64)
        return VisualEvent(**self.unsigned_dict(), checksum=checksum)

    def to_dict(self) -> dict:
        return {**self.unsigned_dict(), "checksum": self.checksum}


def new_event(
    sequence: int,
    event_type: str,
    payload: dict,
    *,
    previous_checksum: str = "",
    event_id: str | None = None,
    created_at: str | None = None,
) -> VisualEvent:
    if sequence < 1:
        raise ValueError("event sequence must be positive")
    if not event_type.strip():
        raise ValueError("event_type is required")
    event = VisualEvent(
        sequence=sequence,
        event_type=event_type.strip(),
        payload=dict(payload),
        created_at=created_at or datetime.now().astimezone().isoformat(timespec="milliseconds"),
        event_id=event_id or uuid.uuid4().hex,
        previous_checksum=previous_checksum,
    )
    return event.with_checksum()


def event_from_dict(value: dict) -> VisualEvent:
    event = VisualEvent(
        sequence=int(value.get("sequence", 0)),
        event_type=str(value.get("event_type", "")),
        payload=dict(value.get("payload", {})),
        created_at=str(value.get("created_at", "")),
        event_id=str(value.get("event_id", "")),
        previous_checksum=str(value.get("previous_checksum", "")),
        checksum=str(value.get("checksum", "")),
        schema_version=int(value.get("schema_version", 0)),
    )
    if event.schema_version != EVENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported event schema version: {event.schema_version}")
    if not event.event_id or event.sequence < 1 or not event.event_type:
        raise ValueError("event envelope is incomplete")
    expected = content_fingerprint(event.unsigned_dict(), length=64)
    if event.checksum != expected:
        raise ValueError(f"event checksum mismatch at sequence {event.sequence}")
    return event


def event_json(event: VisualEvent) -> str:
    return json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
