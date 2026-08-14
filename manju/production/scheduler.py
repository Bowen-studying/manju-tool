"""Pure scheduling decisions for the M1 DAG."""

from __future__ import annotations

from typing import Any

from manju.production.graph import stage_event_state
from manju.production.models import M2_DAG_VERSION, ProductionSnapshot, ProductionStatus


class ProductionScheduler:
    def next_action(self, snapshot: ProductionSnapshot, events: list[dict[str, Any]]) -> str:
        if not snapshot.run_id:
            return "create_run"
        if snapshot.status in {
            ProductionStatus.AWAITING_APPROVAL.value,
            ProductionStatus.BLOCKED.value,
            ProductionStatus.COMPLETED.value,
            ProductionStatus.NEEDS_REVIEW.value,
            ProductionStatus.FAILED.value,
            ProductionStatus.CANCELLED.value,
            ProductionStatus.SUPERSEDED.value,
        }:
            return "stop"
        if not any(
            event.get("run_id") == snapshot.run_id and event.get("event_type") == "run_started"
            for event in events
        ):
            return "start_run"
        if snapshot.status == ProductionStatus.PAUSED.value:
            return "resume_run"
        storyboard_state = stage_event_state(events, snapshot.run_id, "storyboard")
        dag_version = next(
            (str((event.get("payload") or {}).get("dag_version") or "production-m1-v1")
             for event in events if event.get("run_id") == snapshot.run_id and event.get("event_type") == "run_created"),
            "production-m1-v1",
        )
        if storyboard_state == "completed":
            if dag_version == M2_DAG_VERSION:
                visual_state = stage_event_state(events, snapshot.run_id, "visual")
                if visual_state == "completed":
                    return "complete_run"
                if visual_state in {"needs_review", "failed"}:
                    return "stop"
                return "advance_visual"
            return "complete_run"
        if storyboard_state in {"needs_review", "failed"}:
            return "stop"
        return "advance_storyboard"
