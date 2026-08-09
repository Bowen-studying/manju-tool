"""Single-owner rules for durable visual-production facts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FactOwnership:
    fact: str
    owner: str
    projections: tuple[str, ...]
    recovery_reads_projection: bool = False


FACT_OWNERSHIP = (
    FactOwnership(
        "workflow_state", "event_store",
        ("state.json", "visual_agent_run.json", "visual_review.json"),
    ),
    FactOwnership(
        "paid_authorization_and_usage", "paid_ledger",
        ("cost_plan.json", "visual_agent_run.json"),
    ),
    FactOwnership(
        "human_decisions", "approval_store",
        ("approvals/current.json", "visual_agent_run.json"),
    ),
    FactOwnership(
        "binary_artifacts", "artifact_store",
        ("sidecars", "visual_plan.json", "visual_review.json"),
    ),
    FactOwnership(
        "review_verdicts", "review_events",
        ("visual_review.json", "visual_repair_plan.json"),
    ),
    FactOwnership(
        "run_identity", "run_identity_store",
        ("visual_agent_run.json", "state.json"),
    ),
)

PROJECTION_FILES = frozenset(
    projection
    for item in FACT_OWNERSHIP
    for projection in item.projections
    if projection.endswith(".json")
)


def ownership_manifest() -> dict:
    return {
        "schema_version": 1,
        "rule": "one authoritative owner per fact; projections are never recovery authorities",
        "facts": [
            {
                "fact": item.fact,
                "owner": item.owner,
                "projections": list(item.projections),
                "recovery_reads_projection": item.recovery_reads_projection,
            }
            for item in FACT_OWNERSHIP
        ],
    }
