"""Provider-escalation plans for reviewed, non-converging constraints."""

from __future__ import annotations

from collections import defaultdict
import copy

from manju.utils.runtime import content_fingerprint


ESCALATION_SCHEMA_VERSION = 1
ESCALATION_STRATEGY_VERSION = 1

TARGET_PRIORITY = {
    "character_identity": 100,
    "prop_geometry": 95,
    "location_structure": 95,
    "effect_alignment": 75,
    "temporal_state": 70,
    "shot_composition": 40,
    "artifact": 35,
    "other": 30,
}


def issue_priority(issue: dict, scale_asset_ids: set[str] | None = None) -> int:
    focus = set(map(str, issue.get("focus_asset_ids", [])))
    scale_bound = bool(focus.intersection(scale_asset_ids or set()))
    if scale_bound and str(issue.get("correction_target", "")) == "prop_geometry":
        return 98
    return TARGET_PRIORITY.get(str(issue.get("correction_target", "other")), 30)


def provider_requirements(issue: dict, scale_asset_ids: set[str] | None = None) -> list[str]:
    target = str(issue.get("correction_target", "other"))
    focus = set(map(str, issue.get("focus_asset_ids", [])))
    requirements = {"image_edit", "preserve_unaffected_pixels"}
    if target == "character_identity":
        requirements.update({"identity_reference_fidelity", "multi_reference_or_identity_adapter"})
    elif target in {"prop_geometry", "location_structure"}:
        requirements.update({"geometry_reference_fidelity", "topology_preservation"})
    elif target == "effect_alignment":
        requirements.update({"source_target_spatial_relation", "connected_effect_edit"})
    elif target == "temporal_state":
        requirements.update({"state_conditioned_edit", "same_object_state_preservation"})
    if focus.intersection(scale_asset_ids or set()):
        requirements.update({"comparator_scale_transfer", "measurable_relative_size"})
    return sorted(requirements)


def build_constraint_isolation_tasks(
    issues: list[dict],
    *,
    scale_asset_ids: set[str] | None = None,
) -> list[dict]:
    """Select one highest-priority reviewed blocker per shot for one paid pass."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("blocking") is not True:
            continue
        shot_id = str(issue.get("shot_id", ""))
        issue_id = str(issue.get("issue_id", ""))
        verdict = issue.get("constraint_verdict", {})
        if not shot_id or not issue_id:
            continue
        if (
            not isinstance(verdict, dict)
            or verdict.get("verdict") != "fail"
            or not isinstance(verdict.get("evidence"), list)
            or not verdict.get("evidence")
            or float(verdict.get("confidence", 0) or 0) < 0.75
        ):
            continue
        grouped[shot_id].append(copy.deepcopy(issue))

    tasks: list[dict] = []
    for shot_id, candidates in sorted(grouped.items()):
        ranked = sorted(
            candidates,
            key=lambda item: (
                -issue_priority(item, scale_asset_ids),
                -float(item.get("constraint_verdict", {}).get("confidence", 0) or 0),
                str(item.get("issue_id", "")),
            ),
        )
        active = ranked[0]
        active_id = str(active["issue_id"])
        focus_ids = list(dict.fromkeys(map(str, active.get("focus_asset_ids", []))))
        reference_ids = list(dict.fromkeys(map(str, active.get("reference_asset_ids", []))))
        task = {
            "task_id": "escalation_" + content_fingerprint(
                shot_id,
                active_id,
                active.get("correction_target", ""),
                focus_ids,
                ESCALATION_STRATEGY_VERSION,
                length=20,
            ),
            "shot_id": shot_id,
            "active_issue_id": active_id,
            "deferred_issue_ids": [str(item["issue_id"]) for item in ranked[1:]],
            "correction_target": str(active.get("correction_target", "other")),
            "priority": issue_priority(active, scale_asset_ids),
            "focus_asset_ids": focus_ids,
            "reference_asset_ids": reference_ids,
            "problem": str(active.get("problem", "")),
            "instruction": str(active.get("instruction", "")),
            "constraint_verdict": copy.deepcopy(active.get("constraint_verdict", {})),
            "required_provider_capabilities": provider_requirements(active, scale_asset_ids),
        }
        tasks.append(task)
    return tasks


def isolation_prompt_envelope(task: dict) -> str:
    return (
        "CONSTRAINT-ISOLATED EDIT CONTRACT: "
        f"active_issue_id={task.get('active_issue_id', '')}; "
        f"correction_target={task.get('correction_target', 'other')}; "
        f"authoritative_focus_assets={task.get('focus_asset_ids', [])}; "
        f"observed_failure={task.get('problem', '')}; "
        f"required_visible_change={task.get('instruction', '')}; "
        f"deferred_issue_ids={task.get('deferred_issue_ids', [])}. "
        "Change only the active constraint in this paid pass. Preserve every unaffected pixel-level fact, "
        "object count, identity, geometry, physical scale, location and composition from the edit target. "
        "The focused locked asset is authoritative only for the active correction; never copy its reference "
        "canvas framing or create another object instance. Deferred constraints are reviewed after this pass."
    )
