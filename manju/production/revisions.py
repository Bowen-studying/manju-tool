"""Revision facts and successor-run validation rebuilt from the event ledger."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from manju.production.artifacts import ArtifactGraph, ArtifactRef
from manju.production.models import ProductionError, ReasonCode, fingerprint


REVISION_SCHEMA_VERSION = "production-revision-m3-1-v1"


def _invalid(message: str) -> ProductionError:
    return ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, message)


def _refs(value: Any) -> tuple[ArtifactRef, ...]:
    if not isinstance(value, list):
        raise _invalid("revision artifact list is invalid")
    result = tuple(ArtifactRef.from_dict(item) for item in value)
    if len(set(result)) != len(result):
        raise _invalid("revision artifact list contains duplicates")
    return result


@dataclass(frozen=True)
class RevisionRecord:
    revision_id: str
    predecessor_run_id: str
    successor_run_id: str
    requested_by: str
    reason: str
    changed: tuple[ArtifactRef, ...]
    affected: tuple[ArtifactRef, ...]
    reused: tuple[ArtifactRef, ...]
    preview_fingerprint: str
    status: str = "created"

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "predecessor_run_id": self.predecessor_run_id,
            "successor_run_id": self.successor_run_id,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "changed": [item.to_dict() for item in self.changed],
            "affected": [item.to_dict() for item in self.affected],
            "reuse_manifest": [item.to_dict() for item in self.reused],
            "preview_fingerprint": self.preview_fingerprint,
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, value: Any) -> "RevisionRecord":
        if not isinstance(value, dict):
            raise _invalid("revision payload is invalid")
        fields = ("revision_id", "predecessor_run_id", "successor_run_id", "requested_by", "reason", "preview_fingerprint")
        if any(not isinstance(value.get(field), str) or not value[field].strip() for field in fields):
            raise _invalid("revision identity is invalid")
        changed = _refs(value.get("changed"))
        affected = _refs(value.get("affected"))
        reused = _refs(value.get("reuse_manifest"))
        if not changed or set(affected) & set(reused):
            raise _invalid("revision scope is invalid")
        return cls(value["revision_id"], value["predecessor_run_id"], value["successor_run_id"],
                   value["requested_by"], value["reason"], changed, affected, reused,
                   value["preview_fingerprint"])


class RevisionProjection:
    def __init__(self) -> None:
        self._records: dict[str, RevisionRecord] = {}
        self._runs: set[str] = set()
        self._active_run_id = ""

    @property
    def active_run_id(self) -> str:
        return self._active_run_id

    @classmethod
    def from_events(cls, events: Iterable[dict[str, Any]]) -> "RevisionProjection":
        projection = cls()
        prefix: list[dict[str, Any]] = []
        for event in events:
            event_type = event.get("event_type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            run_id = str(event.get("run_id", ""))
            if event_type == "run_created":
                revision_id = str(payload.get("revision_id", ""))
                if not projection._runs:
                    if revision_id or payload.get("predecessor_run_id") or payload.get("revision"):
                        raise _invalid("initial run cannot have a revision predecessor")
                else:
                    record = RevisionRecord.from_payload(payload.get("revision"))
                    if (record.revision_id != revision_id or run_id != record.successor_run_id
                            or payload.get("predecessor_run_id") != record.predecessor_run_id
                            or record.revision_id in projection._records):
                        raise _invalid("successor run does not match a revision")
                    if record.predecessor_run_id != projection._active_run_id:
                        raise _invalid("revision predecessor is not the active run")
                    graph = ArtifactGraph.from_events(prefix)
                    current = {ArtifactRef(item["logical_id"], item["version_id"]) for item in graph.to_dict()["current"]}
                    all_records = set(graph._records)
                    invalidated = {ref for ref, state in graph._states.items() if state == "invalidated"}
                    if (not set(record.changed).issubset(current) or not set(record.affected).issubset(all_records)
                            or set(record.affected) != invalidated):
                        raise _invalid("revision references unavailable artifact versions")
                    if set(record.reused) != current - set(record.changed):
                        raise _invalid("revision reuse manifest is not the current unaffected set")
                    expected = fingerprint({
                        "predecessor_run_id": record.predecessor_run_id,
                        "changed": [item.to_dict() for item in record.changed],
                        "affected": [item.to_dict() for item in record.affected],
                        "reuse_manifest": [item.to_dict() for item in record.reused],
                        "last_event_hash": prefix[-1]["event_hash"] if prefix else "",
                    })
                    if record.preview_fingerprint != expected:
                        raise _invalid("revision preview fingerprint is invalid")
                    projection._records[revision_id] = replace(record, status="superseded_predecessor")
                if not run_id or run_id in projection._runs:
                    raise _invalid("run identity is invalid")
                projection._runs.add(run_id)
                projection._active_run_id = run_id
            prefix.append(event)
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REVISION_SCHEMA_VERSION,
            "active_run_id": self._active_run_id,
            "revisions": [record.to_dict() for _, record in sorted(self._records.items())],
        }
