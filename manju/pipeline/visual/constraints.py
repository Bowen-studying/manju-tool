"""Typed visual constraints and prompt/reference compilation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import json

from manju.utils.runtime import content_fingerprint


class ConstraintPriority(IntEnum):
    STYLE = 20
    SALIENCE = 30
    COMPOSITION = 40
    TEMPORAL_STATE = 60
    REQUIRED_ACTION = 70
    PHYSICAL_SCALE = 90
    GEOMETRY = 95
    IDENTITY = 100


@dataclass(frozen=True)
class VisualConstraint:
    constraint_id: str
    subject: str
    attribute: str
    value: object
    priority: ConstraintPriority
    source: str
    hard: bool
    asset_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "constraint_id": self.constraint_id,
            "subject": self.subject,
            "attribute": self.attribute,
            "value": self.value,
            "priority": int(self.priority),
            "priority_name": self.priority.name.lower(),
            "source": self.source,
            "hard": self.hard,
            "asset_ids": list(self.asset_ids),
        }


def _constraint(
    subject: str,
    attribute: str,
    value: object,
    priority: ConstraintPriority,
    source: str,
    hard: bool,
    asset_ids: tuple[str, ...] = (),
) -> VisualConstraint:
    constraint_id = "vc_" + content_fingerprint(
        subject, attribute, value, int(priority), source, asset_ids, length=20
    )
    return VisualConstraint(
        constraint_id, subject, attribute, value, priority, source, hard, asset_ids
    )


def compile_shot_constraints(
    shot: dict,
    foundation_assets: dict[str, dict],
) -> list[VisualConstraint]:
    shot_id = str(shot.get("shot_id", "")) or "unknown_shot"
    constraints: list[VisualConstraint] = []
    character_ids = tuple(sorted(set(map(str, shot.get("visible_character_ids", [])))))
    prop_ids = tuple(sorted(set(map(str, shot.get("visible_prop_ids", [])))))
    constraints.append(_constraint(
        shot_id, "visible_character_ids", list(character_ids),
        ConstraintPriority.IDENTITY, "storyboard.visible_character_ids", True,
    ))
    constraints.append(_constraint(
        shot_id, "visible_prop_ids", list(prop_ids),
        ConstraintPriority.IDENTITY, "storyboard.visible_prop_ids", True,
    ))
    for asset_id in map(str, shot.get("reference_asset_ids", [])):
        asset = foundation_assets.get(asset_id, {})
        if not isinstance(asset, dict):
            continue
        asset_type = str(asset.get("asset_type", ""))
        if asset_type in {
            "key_prop", "location_master", "character_identity",
            "character_turnaround", "character_expression_pose",
        }:
            constraints.append(_constraint(
                asset_id, "locked_geometry_and_identity", "preserve",
                ConstraintPriority.GEOMETRY if asset_type != "character_identity"
                else ConstraintPriority.IDENTITY,
                "locked_foundation_asset", True, (asset_id,),
            ))
        contract = asset.get("reference_contract", {})
        scale = contract.get("scale_contract", {}) if isinstance(contract, dict) else {}
        if isinstance(scale, dict) and scale.get("required") is True:
            constraints.append(_constraint(
                asset_id, "physical_scale", {
                    "source_cues": list(scale.get("source_cues", [])),
                    "shot_policy": str(scale.get("shot_policy", "")),
                }, ConstraintPriority.PHYSICAL_SCALE,
                "foundation.reference_contract.scale_contract", True, (asset_id,),
            ))
    description = str(shot.get("description", ""))
    if description:
        constraints.append(_constraint(
            shot_id, "required_action", description,
            ConstraintPriority.REQUIRED_ACTION, "storyboard.description", True,
        ))
    prompt = str(shot.get("prompt", ""))
    if prompt:
        constraints.append(_constraint(
            shot_id, "narrative_composition_context", prompt,
            ConstraintPriority.COMPOSITION, "storyboard.prompt", False,
        ))
    return sorted(
        constraints,
        key=lambda item: (-int(item.priority), item.constraint_id),
    )


def detect_constraint_conflicts(constraints: list[VisualConstraint]) -> list[dict]:
    grouped: dict[tuple[str, str], list[VisualConstraint]] = {}
    for item in constraints:
        grouped.setdefault((item.subject, item.attribute), []).append(item)
    conflicts: list[dict] = []
    for (subject, attribute), items in grouped.items():
        hard_values = {
            json.dumps(item.value, ensure_ascii=False, sort_keys=True)
            for item in items if item.hard
        }
        if len(hard_values) > 1:
            conflicts.append({
                "conflict_id": "conflict_" + content_fingerprint(
                    subject, attribute, sorted(hard_values), length=20
                ),
                "subject": subject,
                "attribute": attribute,
                "severity": "blocking",
                "constraint_ids": [item.constraint_id for item in items if item.hard],
                "reason": "multiple hard constraints assign different values",
            })
    return conflicts


def prompt_constraint_envelope(constraints: list[VisualConstraint]) -> str:
    hard = [item.to_dict() for item in constraints if item.hard]
    soft = [item.to_dict() for item in constraints if not item.hard]
    return (
        "VISUAL CONSTRAINT CONTRACT (NON-NEGOTIABLE AND HIGHER PRIORITY THAN THE NARRATIVE PROMPT): "
        + json.dumps({
            "hard_constraints": hard,
            "soft_context": soft,
            "precedence": [
                "identity", "geometry", "physical_scale", "required_action",
                "temporal_state", "composition", "salience", "style",
            ],
            "conflict_rule": (
                "A soft composition, prominence, visibility, focal-distance or style request may change "
                "readability but must never alter a hard identity, geometry, topology, orientation or "
                "physical-scale constraint."
            ),
        }, ensure_ascii=False, separators=(",", ":"))
    )


def compile_fallback_constraints(
    constraints: list[VisualConstraint],
    *,
    trigger: str,
) -> dict:
    """Compile a provider-friendly expression without weakening hard facts."""
    conflicts = detect_constraint_conflicts(constraints)
    invariant_hard_facts = [item.to_dict() for item in constraints if item.hard]
    degraded_expression: list[dict] = []
    for item in constraints:
        if not item.hard:
            continue
        if item.attribute == "physical_scale":
            mode = "bounded_natural_comparator_relation"
            instruction = (
                "Express the unchanged source-declared physical scale through a complete familiar "
                "in-scene comparator in direct contact or on the same support plane; numeric labels "
                "and rulers are optional and do not replace the relation."
            )
        elif item.attribute == "locked_geometry_and_identity":
            mode = "topology_and_silhouette_relation"
            instruction = (
                "Express the unchanged locked geometry through silhouette, connected parts, orientation "
                "and attachment relations instead of relying on prose labels or measurements."
            )
        else:
            mode = "direct_visible_relation"
            instruction = "Render the unchanged hard fact as one directly visible relationship."
        degraded_expression.append({
            "constraint_id": item.constraint_id,
            "expression_mode": mode,
            "original_value": item.value,
            "hard_fact_unchanged": True,
            "instruction": instruction,
        })
    relaxable_soft = [
        item.to_dict() for item in constraints
        if not item.hard and item.priority <= ConstraintPriority.COMPOSITION
    ]
    return {
        "schema_version": 1,
        "status": "blocked_hard_conflict" if conflicts else "ready",
        "trigger": str(trigger),
        "hard_facts_unchanged": True,
        "invariant_hard_facts": invariant_hard_facts,
        "degraded_expression": degraded_expression,
        "relaxable_soft_context": relaxable_soft,
        "verification_requirement": (
            "Re-review every invariant hard fact against image evidence; fallback expression never "
            "changes identity, geometry, topology, orientation or physical scale."
        ),
        "hard_conflicts": conflicts,
    }


def fallback_constraint_envelope(
    constraints: list[VisualConstraint],
    *,
    trigger: str,
) -> str:
    plan = compile_fallback_constraints(constraints, trigger=trigger)
    return (
        "PROVIDER FALLBACK EXPRESSION CONTRACT (HARD FACTS REMAIN UNCHANGED): "
        + json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    )


def prioritize_reference_assets(
    asset_ids: list[str],
    constraints: list[VisualConstraint],
    *,
    focus_asset_ids: list[str] | None = None,
) -> list[str]:
    scores: dict[str, int] = {str(asset_id): 0 for asset_id in asset_ids}
    for constraint in constraints:
        for asset_id in constraint.asset_ids:
            if asset_id in scores:
                scores[asset_id] = max(scores[asset_id], int(constraint.priority))
    focus = set(map(str, focus_asset_ids or []))
    return sorted(
        scores,
        key=lambda asset_id: (
            0 if asset_id in focus else 1,
            -scores[asset_id],
            list(map(str, asset_ids)).index(asset_id),
        ),
    )
