"""Pure visual workflow state reducer."""

from __future__ import annotations

import copy

from .commands import STAGE_COMMANDS
from .events import VisualEvent


def _require_run(state: dict, payload: dict) -> None:
    event_run_id = str(payload.get("run_id", ""))
    current_run_id = str(state.get("run_id", ""))
    if current_run_id and event_run_id and current_run_id != event_run_id:
        raise ValueError(
            f"event run_id {event_run_id!r} does not match state run_id {current_run_id!r}"
        )


def reduce_visual_state(state: dict | None, event: VisualEvent) -> dict:
    current = copy.deepcopy(state or {})
    payload = copy.deepcopy(event.payload)
    event_type = event.event_type
    if event_type == "run_created":
        if current:
            raise ValueError("run_created requires an empty state")
        run_id = str(payload.get("run_id", ""))
        if not run_id:
            raise ValueError("run_created requires run_id")
        current = {
            "run_id": run_id,
            "status": "running",
            "stage": "new",
            "stop_reason": "",
            "run_identity": copy.deepcopy(payload.get("run_identity", {})),
        }
    elif event_type == "legacy_state_committed":
        snapshot = payload.get("state")
        if not isinstance(snapshot, dict):
            raise ValueError("legacy_state_committed requires a state snapshot")
        _require_run(current, snapshot)
        current = copy.deepcopy(snapshot)
    elif event_type == "stage_changed":
        _require_run(current, payload)
        stage = str(payload.get("stage", ""))
        if stage not in STAGE_COMMANDS and stage not in {
            "completed", "blocked_upstream", "revision_failure_without_ledger_evidence",
        }:
            raise ValueError(f"unknown workflow stage: {stage}")
        current["stage"] = stage
    elif event_type == "status_changed":
        _require_run(current, payload)
        status = str(payload.get("status", ""))
        if status not in {"running", "awaiting_approval", "needs_review", "failed", "completed"}:
            raise ValueError(f"unknown workflow status: {status}")
        current["status"] = status
        current["stop_reason"] = str(payload.get("stop_reason", ""))
    elif event_type == "approval_requested":
        _require_run(current, payload)
        request = payload.get("request")
        if not isinstance(request, dict) or not request.get("request_id"):
            raise ValueError("approval_requested requires a request")
        current["pending_approval"] = request
        current["status"] = "awaiting_approval"
        current["stop_reason"] = str(request.get("stage", "approval"))
    elif event_type == "approval_applied":
        _require_run(current, payload)
        if not str(payload.get("request_id", "")):
            raise ValueError("approval_applied requires request_id")
        current["pending_approval"] = {}
        current["last_approval"] = {
            "request_id": str(payload["request_id"]),
            "decision": str(payload.get("decision", "")),
        }
    else:
        raise ValueError(f"unsupported visual event type: {event_type}")
    current["event_sequence"] = event.sequence
    current["event_checksum"] = event.checksum
    return current


def replay_visual_events(events: list[VisualEvent]) -> dict:
    state: dict = {}
    previous_checksum = ""
    for expected_sequence, event in enumerate(events, 1):
        if event.sequence != expected_sequence:
            raise ValueError(
                f"event sequence gap: expected {expected_sequence}, got {event.sequence}"
            )
        if event.previous_checksum != previous_checksum:
            raise ValueError(f"event checksum chain mismatch at sequence {event.sequence}")
        state = reduce_visual_state(state, event)
        previous_checksum = event.checksum
    return state
