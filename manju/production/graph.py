"""The deterministic M1 production DAG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DagNode:
    node_id: str
    stage: str
    dependencies: tuple[str, ...] = ()


M1_NODES = (
    DagNode(node_id="storyboard", stage="storyboard", dependencies=("source",)),
)
M2_NODES = (
    DagNode(node_id="storyboard", stage="storyboard", dependencies=("source",)),
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)


def stage_event_state(events: list[dict[str, Any]], run_id: str, stage: str) -> str:
    state = "pending"
    for event in events:
        if event.get("run_id") != run_id:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("stage") != stage:
            continue
        event_type = event.get("event_type")
        if event_type in {"stage_scheduled", "stage_run_attached"}:
            state = "running"
        elif event_type == "stage_completed":
            state = "completed"
        elif event_type == "stage_needs_review":
            state = "needs_review"
        elif event_type == "stage_failed":
            state = "failed"
    return state


def has_event(events: list[dict[str, Any]], run_id: str, event_type: str, stage: str = "") -> bool:
    for event in events:
        if event.get("run_id") != run_id or event.get("event_type") != event_type:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not stage or payload.get("stage") == stage:
            return True
    return False
