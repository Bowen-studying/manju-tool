"""Immutable artifact versions and deterministic invalidation projections."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Iterable

from manju.production.models import ProductionError, ReasonCode


ARTIFACT_GRAPH_SCHEMA_VERSION = "production-artifact-graph-v1"


def _invalid(message: str) -> ProductionError:
    return ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, message)


@dataclass(frozen=True, order=True)
class ArtifactRef:
    logical_id: str
    version_id: str

    def to_dict(self) -> dict[str, str]:
        return {"logical_id": self.logical_id, "version_id": self.version_id}

    @classmethod
    def from_dict(cls, value: Any) -> "ArtifactRef":
        if not isinstance(value, dict):
            raise _invalid("artifact reference must be an object")
        logical_id = value.get("logical_id")
        version_id = value.get("version_id")
        if not isinstance(logical_id, str) or not logical_id.strip():
            raise _invalid("artifact logical_id is invalid")
        if not isinstance(version_id, str) or len(version_id) != 71 or not version_id.startswith("sha256:"):
            raise _invalid("artifact version_id is invalid")
        try:
            int(version_id[7:], 16)
        except ValueError as exc:
            raise _invalid("artifact version_id is invalid") from exc
        return cls(logical_id=logical_id, version_id=version_id)


@dataclass(frozen=True)
class ArtifactRecord:
    ref: ArtifactRef
    path: str
    producer_stage: str
    producer_run_id: str
    depends_on: tuple[ArtifactRef, ...]

    def to_dict(self, *, state: str) -> dict[str, Any]:
        return {
            **self.ref.to_dict(),
            "path": self.path,
            "producer": {"stage": self.producer_stage, "run_id": self.producer_run_id},
            "depends_on": [item.to_dict() for item in self.depends_on],
            "state": state,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ArtifactRecord":
        if not isinstance(value, dict):
            raise _invalid("artifact record must be an object")
        ref = ArtifactRef.from_dict(value)
        path = value.get("path")
        producer = value.get("producer")
        dependencies = value.get("depends_on", [])
        if (not isinstance(path, str) or not path or os.path.splitdrive(path)[0]
                or path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/")):
            raise _invalid("artifact path must be a relative project path")
        if not isinstance(producer, dict) or not isinstance(producer.get("stage"), str) or not producer["stage"].strip():
            raise _invalid("artifact producer stage is invalid")
        if not isinstance(producer.get("run_id", ""), str) or not isinstance(dependencies, list):
            raise _invalid("artifact producer or dependencies are invalid")
        refs = tuple(ArtifactRef.from_dict(item) for item in dependencies)
        if len(set(refs)) != len(refs) or ref in refs:
            raise _invalid("artifact dependencies must be unique and cannot reference itself")
        return cls(ref=ref, path=path.replace("\\", "/"), producer_stage=producer["stage"],
                   producer_run_id=producer.get("run_id", ""), depends_on=refs)


class ArtifactGraph:
    """Projection rebuilt solely from append-only artifact events."""

    def __init__(self) -> None:
        self._records: dict[ArtifactRef, ArtifactRecord] = {}
        self._states: dict[ArtifactRef, str] = {}
        self._current: dict[str, ArtifactRef] = {}

    @classmethod
    def from_events(cls, events: Iterable[dict[str, Any]]) -> "ArtifactGraph":
        graph = cls()
        for event in events:
            event_type = event.get("event_type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "artifact_registered":
                graph.register(ArtifactRecord.from_dict(payload.get("artifact")))
            elif event_type == "artifact_version_selected":
                graph.select(
                    logical_id=payload.get("logical_id"),
                    version_id=payload.get("version_id"),
                    previous_version_id=payload.get("previous_version_id", ""),
                    recorded_invalidated=payload.get("invalidated"),
                )
            elif event_type == "run_created" and isinstance(payload.get("revision"), dict):
                # M3.3 makes candidate selection part of the successor's only
                # authoritative creation event.  Historical M3.1 revisions do
                # not carry this field and retain their original projection.
                selection = payload["revision"].get("successor_selection")
                changed = payload["revision"].get("changed")
                if selection is not None:
                    graph.apply_revision_selection(changed)
        return graph

    def register(self, record: ArtifactRecord) -> None:
        if record.ref in self._records:
            raise _invalid("artifact version is already registered")
        for dependency in record.depends_on:
            if dependency not in self._records or self._states[dependency] != "current":
                raise _invalid("artifact dependency must be the current version")
        self._records[record.ref] = record
        self._states[record.ref] = "available"

    def invalidated_by(self, ref: ArtifactRef) -> tuple[ArtifactRef, ...]:
        affected: set[ArtifactRef] = {ref}
        changed = True
        while changed:
            changed = False
            for candidate, record in self._records.items():
                if candidate in affected:
                    continue
                if any(dependency in affected for dependency in record.depends_on):
                    affected.add(candidate)
                    changed = True
        affected.remove(ref)
        return tuple(sorted(affected))

    def revision_scope(self, changed: Iterable[ArtifactRef]) -> tuple[tuple[ArtifactRef, ...], tuple[ArtifactRef, ...], tuple[ArtifactRef, ...], tuple[ArtifactRef, ...]]:
        """Return predecessor, affected, successor and reusable selections.

        Candidate versions remain ``available`` until this scope is committed by
        a successor ``run_created`` event, so a completed predecessor is never
        retroactively selected or credited as their producer.
        """
        requested = tuple(changed)
        current = tuple(sorted(self._current.values()))
        if not requested or len(set(requested)) != len(requested):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision candidates are invalid")
        previous_by_id = {item.logical_id: item for item in current}
        if any(
            item.logical_id not in previous_by_id
            or item not in self._records
            or self._states[item] != "available"
            for item in requested
        ):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "revision candidates must be available replacements")
        replacement_ids = {item.logical_id for item in requested}
        if any(
            dependency.logical_id in replacement_ids
            for item in requested
            for dependency in self._records[item].depends_on
        ):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value,
                                  "revision candidates cannot depend on a version replaced in the same revision")
        affected = tuple(sorted({
            downstream
            for item in requested
            for downstream in self.invalidated_by(previous_by_id[item.logical_id])
        }))
        if set(requested) & set(affected):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value,
                                  "revision candidate depends on its own invalidation closure")
        successor = set(current)
        successor.difference_update(previous_by_id[item.logical_id] for item in requested)
        successor.difference_update(affected)
        successor.update(requested)
        successor_selection = tuple(sorted(successor))
        reused = tuple(sorted(set(successor_selection) - set(requested)))
        return current, affected, successor_selection, reused

    def apply_revision_selection(self, changed: Any) -> tuple[ArtifactRef, ...]:
        """Apply the atomic M3.3 successor selection encoded in run_created."""
        if not isinstance(changed, list):
            raise _invalid("revision changed artifacts are invalid")
        requested = tuple(ArtifactRef.from_dict(item) for item in changed)
        _current, affected, successor, _reused = self.revision_scope(requested)
        for item in sorted(requested):
            previous = self._current[item.logical_id]
            invalidated = self.invalidated_by(previous)
            self.select(
                logical_id=item.logical_id,
                version_id=item.version_id,
                previous_version_id=previous.version_id,
                recorded_invalidated=[ref.to_dict() for ref in invalidated],
            )
        if tuple(sorted(self._current.values())) != successor:
            raise _invalid("atomic revision selection does not match the successor snapshot")
        if any(
            self._states[dependency] != "current"
            for ref in self._current.values()
            for dependency in self._records[ref].depends_on
        ):
            raise _invalid("atomic revision selection leaves a current artifact with an invalid dependency")
        return affected

    def select(self, *, logical_id: Any, version_id: Any, previous_version_id: Any,
               recorded_invalidated: Any) -> tuple[ArtifactRef, ...]:
        if not isinstance(logical_id, str) or not isinstance(version_id, str) or not isinstance(previous_version_id, str):
            raise _invalid("artifact selection is invalid")
        target = ArtifactRef.from_dict({"logical_id": logical_id, "version_id": version_id})
        if target not in self._records or self._states[target] != "available":
            raise _invalid("selected artifact must be an available registered version")
        if any(self._states[dependency] != "current" for dependency in self._records[target].depends_on):
            raise _invalid("selected artifact has a non-current dependency")
        previous = self._current.get(logical_id)
        if (previous.version_id if previous else "") != previous_version_id:
            raise _invalid("artifact selection previous version does not match current version")
        expected = self.invalidated_by(previous) if previous else ()
        if not isinstance(recorded_invalidated, list):
            raise _invalid("artifact selection invalidated list is missing")
        recorded = tuple(sorted(ArtifactRef.from_dict(item) for item in recorded_invalidated))
        if len(set(recorded)) != len(recorded) or recorded != expected:
            raise _invalid("artifact selection invalidated set is not deterministic")
        if previous:
            self._states[previous] = "superseded"
        for item in expected:
            self._states[item] = "invalidated"
            if self._current.get(item.logical_id) == item:
                del self._current[item.logical_id]
        self._states[target] = "current"
        self._current[logical_id] = target
        return expected

    def get(self, logical_id: str, version_id: str) -> ArtifactRecord:
        ref = ArtifactRef.from_dict({"logical_id": logical_id, "version_id": version_id})
        try:
            return self._records[ref]
        except KeyError as exc:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact version is not registered") from exc

    def current_version(self, logical_id: str) -> str:
        current = self._current.get(logical_id)
        return current.version_id if current else ""

    def to_dict(self) -> dict[str, Any]:
        records = [record.to_dict(state=self._states[ref]) for ref, record in sorted(self._records.items())]
        current = [ref.to_dict() for _, ref in sorted(self._current.items())]
        return {"schema_version": ARTIFACT_GRAPH_SCHEMA_VERSION, "artifacts": records, "current": current}
