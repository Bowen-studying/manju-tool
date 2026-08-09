"""Crash-safe event store and compatibility snapshot adapter."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile

from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json

from .events import VisualEvent, event_from_dict, event_json, new_event
from .identity import RunIdentity, identity_from_dict
from .reducer import replay_visual_events


EVENT_LOG_NAME = "events.jsonl"
SNAPSHOT_NAME = "snapshot.json"
IDENTITY_NAME = "run_identity.json"
CURRENT_RUN_NAME = "current_run.json"
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _atomic_write_text(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class VisualEventStore:
    def __init__(self, output_dir: str, run_id: str, *, pipeline_name: str = "visual_agent"):
        if (
            not run_id
            or run_id in {".", ".."}
            or os.path.isabs(run_id)
            or os.path.basename(run_id) != run_id
            or not _SAFE_PATH_COMPONENT.fullmatch(run_id)
        ):
            raise ValueError("run_id must be a single directory name")
        if (
            not pipeline_name
            or pipeline_name in {".", ".."}
            or os.path.basename(pipeline_name) != pipeline_name
            or not _SAFE_PATH_COMPONENT.fullmatch(pipeline_name)
        ):
            raise ValueError("pipeline_name must be a single directory name")
        self.output_dir = os.path.abspath(output_dir)
        self.run_id = run_id
        self.pipeline_name = pipeline_name
        self.root = os.path.join(
            self.output_dir, "stages", self.pipeline_name, "runs", self.run_id
        )
        self.event_path = os.path.join(self.root, EVENT_LOG_NAME)
        self.snapshot_path = os.path.join(self.root, SNAPSHOT_NAME)
        self.identity_path = os.path.join(self.root, IDENTITY_NAME)

    def load_events(self) -> list[VisualEvent]:
        if not os.path.isfile(self.event_path):
            return []
        events: list[VisualEvent] = []
        with open(self.event_path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid event JSON at line {line_number}: {exc}") from exc
                events.append(event_from_dict(value))
        replay_visual_events(events)
        return events

    def append(self, event_type: str, payload: dict) -> VisualEvent:
        events = self.load_events()
        previous = events[-1].checksum if events else ""
        event = new_event(
            len(events) + 1, event_type, payload, previous_checksum=previous
        )
        body = "".join(event_json(item) + "\n" for item in [*events, event])
        _atomic_write_text(self.event_path, body)
        return event

    def commit_state(self, state: dict, *, reason: str) -> VisualEvent | None:
        if str(state.get("run_id", "")) != self.run_id:
            raise ValueError("state run_id does not match event store")
        # Reducer metadata describes the event envelope, not workflow facts. It
        # must not make an unchanged recovered state look different next time.
        committed_state = copy.deepcopy(state)
        committed_state.pop("event_sequence", None)
        committed_state.pop("event_checksum", None)
        committed_state.pop("_projection", None)
        state_fingerprint = content_fingerprint(committed_state, length=64)
        events = self.load_events()
        if events:
            last = events[-1]
            if (
                last.event_type == "legacy_state_committed"
                and last.payload.get("state_fingerprint") == state_fingerprint
            ):
                return None
        event = self.append("legacy_state_committed", {
            "run_id": self.run_id,
            "reason": reason,
            "state_fingerprint": state_fingerprint,
            "state": committed_state,
        })
        committed = replay_visual_events(self.load_events())
        atomic_write_json(self.snapshot_path, {
            "schema_version": 1,
            "run_id": self.run_id,
            "event_sequence": event.sequence,
            "event_checksum": event.checksum,
            "state_fingerprint": state_fingerprint,
            "state": committed,
        })
        return event

    def recover_state(self) -> dict | None:
        events = self.load_events()
        if events:
            return replay_visual_events(events)
        snapshot = read_json(self.snapshot_path)
        if isinstance(snapshot, dict) and isinstance(snapshot.get("state"), dict):
            return snapshot["state"]
        return None

    def write_identity(self, identity: RunIdentity) -> None:
        if identity.run_id != self.run_id:
            raise ValueError("identity run_id does not match event store")
        existing = identity_from_dict(read_json(self.identity_path))
        if existing and existing != identity:
            raise ValueError("run identity is immutable")
        atomic_write_json(self.identity_path, identity.to_dict())
        pointer_path = os.path.join(
            self.output_dir, "stages", self.pipeline_name, CURRENT_RUN_NAME
        )
        atomic_write_json(pointer_path, {
            "schema_version": 1,
            "run_id": identity.run_id,
            "identity_path": os.path.relpath(self.identity_path, self.output_dir),
        })


def read_current_run_id(output_dir: str, *, pipeline_name: str = "visual_agent") -> str:
    pointer = read_json(os.path.join(
        os.path.abspath(output_dir), "stages", pipeline_name, CURRENT_RUN_NAME
    ))
    return str(pointer.get("run_id", "")) if isinstance(pointer, dict) else ""


def recover_current_state(output_dir: str, *, pipeline_name: str = "visual_agent") -> dict | None:
    run_id = read_current_run_id(output_dir, pipeline_name=pipeline_name)
    return (
        VisualEventStore(output_dir, run_id, pipeline_name=pipeline_name).recover_state()
        if run_id else None
    )
