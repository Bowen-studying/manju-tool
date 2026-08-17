"""Pure scheduling decisions for the M1 DAG."""

from __future__ import annotations

from typing import Any

from manju.production.graph import stage_event_state
from manju.production.models import ProductionSnapshot, ProductionStatus, stages_for_dag


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
        created = next(
            (event for event in events
             if event.get("run_id") == snapshot.run_id and event.get("event_type") == "run_created"),
            None,
        )
        payload = (created or {}).get("payload") or {}
        stages = stages_for_dag(
            str(payload.get("dag_version") or "production-m1-v1"), payload.get("stage_sequence"),
        )
        for stage in stages:
            state = stage_event_state(events, snapshot.run_id, stage)
            if state == "completed":
                continue
            if state in {"needs_review", "failed"}:
                return "stop"
            return f"advance_{stage}"
        return "complete_run"
