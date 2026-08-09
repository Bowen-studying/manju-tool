"""Offline, deterministic storyboard-to-hybrid-scene planning.

The planner deliberately accepts only structured visual facts.  It may carry
model-supplied composition preferences, but a local solver owns the final
placement and an independent verifier re-checks its output before a scene is
made available to the existing hybrid renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
from fractions import Fraction
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable

from PIL import Image, ImageChops

from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json

from .hybrid import (
    HYBRID_PIPELINE_NAME,
    HYBRID_SCHEMA_VERSION,
    HYBRID_SIGNING_KEY_ENV,
    scene_from_dict,
)
from .asset_intake import AssetIntakeError, verify_asset_promotion, verify_asset_promotion_evidence
from .store import VisualEventStore


PLANNER_SCHEMA_VERSION = 1
PLANNER_VERSION = "4.1.0-hybrid-rc4-hf3"
PLANNER_POLICY_VERSION = "1"
DEFAULT_CANVAS = {"width": 1024, "height": 576}
DEFAULT_SOLVER_TIMEOUT_SECONDS = 5.0
DEFAULT_SOLVER_SEED = 41027
SUPPORTED_LAYOUT_PREFERENCES = frozenset({"keep_subjects_close", "center_subjects", "spread_subjects"})
SUPPORTED_CONSTRAINT_KINDS = frozenset({
    "presence", "count", "relative_size", "direction", "containment",
    "safe_area", "z_order", "minimum_visibility", "forbidden_occlusion",
})
_SAFE_SHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
READY = "ready"
NEEDS_ASSETS = "needs_assets"
NEEDS_CONSTRAINT_CONFIRMATION = "needs_constraint_confirmation"
NEEDS_HUMAN_REVIEW = "needs_human_review"
BLOCKED_CONFLICT = "blocked_conflict"
BLOCKED_UNSUPPORTED = "blocked_unsupported"
BLOCKED_SOLVER_TIMEOUT = "blocked_solver_timeout"
BLOCKED_CONTINUITY = "blocked_continuity"
BLOCKED_INTEGRITY = "blocked_integrity"
SCENE_CONTRACT_REVISION_REQUIRED = "scene_contract_revision_required"
MIN_RENDERED_DIMENSION = 16
MIN_RENDERED_AREA = 256
_CONTAINMENT_VISIBILITY_MODES = frozenset({"subject_in_front", "transparent_window", "layered_container"})


class PlannerError(ValueError):
    """A deterministic planning error suitable for user-facing diagnostics."""


class PlannerDependencyError(PlannerError):
    """Raised when the optional local solver is unavailable."""


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    revision: str
    asset_type: str
    lifecycle: str
    image_path: str
    image_sha256: str
    width: int
    height: int
    source_kind: str = "local"
    promotion_sha256: str = ""
    promotion_evidence: dict | None = None

    @property
    def identity(self) -> str:
        return f"{self.asset_id}@{self.revision}"

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "revision": self.revision,
            "identity": self.identity,
            "asset_type": self.asset_type,
            "lifecycle": self.lifecycle,
            "image_path": self.image_path,
            "image_sha256": self.image_sha256,
            "width": self.width,
            "height": self.height,
            "source_kind": self.source_kind,
            "promotion_sha256": self.promotion_sha256,
            "promotion_evidence": copy.deepcopy(self.promotion_evidence) if self.promotion_evidence else {},
        }


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_object(path: str, label: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlannerError(f"{label} must be a readable JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise PlannerError(f"{label} must contain a JSON object")
    return value


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PlannerError(f"{field} is required")
    return text


def _safe_relative_path(value: object, base_dir: str, field: str) -> str:
    raw = _required_text(value, field)
    candidate = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
    return os.path.realpath(os.path.abspath(candidate))


def _inside_any_root(path: str, roots: Iterable[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _integer(value: object, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlannerError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise PlannerError(f"{field} must be at least {minimum}")
    return value


def _number(value: object, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise PlannerError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlannerError(f"{field} must be a number") from exc
    if not math.isfinite(number):
        raise PlannerError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise PlannerError(f"{field} must be at least {minimum}")
    return number


def _read_asset_registry(payload: dict, *, base_dir: str) -> tuple[dict[str, AssetRecord], list[str]]:
    if int(payload.get("schema_version", 0) or 0) != PLANNER_SCHEMA_VERSION:
        raise PlannerError(f"asset registry requires schema_version {PLANNER_SCHEMA_VERSION}")
    roots_value = payload.get("asset_roots")
    if not isinstance(roots_value, list) or not roots_value:
        raise PlannerError("asset registry requires a non-empty asset_roots list")
    roots: list[str] = []
    for index, value in enumerate(roots_value):
        root = _safe_relative_path(value, base_dir, f"asset_roots[{index}]")
        if not os.path.isdir(root):
            raise PlannerError(f"asset_roots[{index}] is not a directory")
        roots.append(root)
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise PlannerError("asset registry requires assets")
    assets: dict[str, AssetRecord] = {}
    logical_ids: set[str] = set()
    for index, item in enumerate(raw_assets):
        if not isinstance(item, dict):
            raise PlannerError(f"assets[{index}] must be an object")
        asset_id = _required_text(item.get("asset_id"), f"assets[{index}].asset_id")
        revision = _required_text(item.get("revision"), f"assets[{index}].revision")
        identity = f"{asset_id}@{revision}"
        if identity in assets:
            raise PlannerError(f"duplicate asset identity: {identity}")
        image_path = _safe_relative_path(item.get("image_path"), base_dir, f"assets[{index}].image_path")
        if not os.path.isfile(image_path):
            raise PlannerError(f"assets[{index}].image_path does not exist")
        if not _inside_any_root(image_path, roots):
            raise PlannerError(f"assets[{index}].image_path escapes declared asset_roots")
        with Image.open(image_path) as image_file:
            width, height = image_file.size
        if width < 1 or height < 1:
            raise PlannerError(f"assets[{index}] has an empty image")
        lifecycle = str(item.get("lifecycle", "formal")).strip().lower()
        if lifecycle not in {"formal", "candidate", "fixture"}:
            raise PlannerError(f"assets[{index}].lifecycle must be formal, candidate, or fixture")
        source_kind = str(item.get("source_kind", "local")).strip().lower()
        if source_kind not in {"provider", "local", "fixture"}:
            raise PlannerError(f"assets[{index}].source_kind must be provider, local, or fixture")
        image_sha256 = _file_sha256(image_path)
        promotion_sha256 = ""
        promotion_evidence: dict | None = None
        if source_kind == "provider" and lifecycle == "formal":
            promotion_path = _safe_relative_path(item.get("promotion_path"), base_dir, f"assets[{index}].promotion_path")
            if not os.path.isfile(promotion_path):
                raise PlannerError(f"assets[{index}].promotion_path does not exist")
            try:
                promotion_evidence = verify_asset_promotion(
                    promotion_path,
                    asset_id=asset_id,
                    revision=revision,
                    asset_type=str(item.get("asset_type", "overlay")).strip().lower() or "overlay",
                    image_path=image_path,
                    image_sha256=image_sha256,
                    width=width,
                    height=height,
                    derivation_roots=roots,
                )
            except AssetIntakeError as exc:
                raise PlannerError(str(exc)) from exc
            promotion_sha256 = _file_sha256(promotion_path)
        record = AssetRecord(
            asset_id=asset_id,
            revision=revision,
            asset_type=str(item.get("asset_type", "overlay")).strip().lower() or "overlay",
            lifecycle=lifecycle,
            image_path=image_path,
            image_sha256=image_sha256,
            width=width,
            height=height,
            source_kind=source_kind,
            promotion_sha256=promotion_sha256,
            promotion_evidence=promotion_evidence,
        )
        assets[identity] = record
        logical_ids.add(asset_id)
    return assets, roots


def _asset_by_reference(reference: object, assets: dict[str, AssetRecord]) -> AssetRecord | None:
    text = str(reference or "").strip()
    if not text:
        return None
    if text in assets:
        return assets[text]
    matches = [item for item in assets.values() if item.asset_id == text]
    return matches[0] if len(matches) == 1 else None


def _shot_canvas(storyboard: dict, shot: dict) -> tuple[int, int]:
    for candidate in (shot.get("canvas"), storyboard.get("canvas"), storyboard.get("planning", {}).get("canvas") if isinstance(storyboard.get("planning"), dict) else None):
        if isinstance(candidate, dict):
            return (
                _integer(candidate.get("width"), "canvas.width", minimum=1),
                _integer(candidate.get("height"), "canvas.height", minimum=1),
            )
    return DEFAULT_CANVAS["width"], DEFAULT_CANVAS["height"]


def _shot_visual(shot: dict) -> dict:
    value = shot.get("visual")
    return value if isinstance(value, dict) else {}


def _normalise_preferences(shot: dict) -> tuple[list[dict], bool]:
    raw = shot.get("layout_preferences", [])
    if raw is None:
        return [], False
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise PlannerError(f"shot {shot.get('shot_id', '')} layout_preferences must be a list of objects")
    preferences: list[dict] = []
    requires_human_review = False
    for index, item in enumerate(raw):
        preference = _required_text(item.get("preference"), f"layout_preferences[{index}].preference")
        if preference not in SUPPORTED_LAYOUT_PREFERENCES:
            raise PlannerError(f"layout_preferences[{index}].preference is not supported")
        reason = _required_text(item.get("reason"), f"layout_preferences[{index}].reason")
        source = _required_text(item.get("source", "model_layout_preference"), f"layout_preferences[{index}].source")
        priority = _integer(item.get("priority", 50), f"layout_preferences[{index}].priority", minimum=0)
        if priority > 100:
            raise PlannerError(f"layout_preferences[{index}].priority must be at most 100")
        review = item.get("requires_human_review", False)
        if not isinstance(review, bool):
            raise PlannerError(f"layout_preferences[{index}].requires_human_review must be a boolean")
        affected = item.get("affected_asset_ids", [])
        if not isinstance(affected, list) or not all(isinstance(value, str) and value.strip() for value in affected):
            raise PlannerError(f"layout_preferences[{index}].affected_asset_ids must be a list of non-empty strings")
        preferences.append({
            "preference": preference, "priority": priority, "reason": reason,
            "source": source, "affected_asset_ids": list(affected),
            "requires_human_review": review,
        })
        requires_human_review = requires_human_review or review
    return preferences, requires_human_review


def _shot_asset_references(shot: dict) -> list[str]:
    visual = _shot_visual(shot)
    values: list[str] = []
    for key in ("visible_character_ids", "visible_prop_ids"):
        raw = visual.get(key) if key == "visible_character_ids" else shot.get(key)
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
    for prop in visual.get("key_props", []) if isinstance(visual.get("key_props"), list) else []:
        if isinstance(prop, dict):
            value = prop.get("asset_id") or prop.get("prop_id")
            if str(value or "").strip():
                values.append(str(value).strip())
    return list(dict.fromkeys(values))


def _constraint_references(constraint: dict) -> list[str]:
    values: list[str] = []
    for key in ("subject_id", "reference_id", "source_id", "target_id", "container_id", "front_layer_id", "behind_id", "front_id"):
        if str(constraint.get(key, "")).strip():
            values.append(str(constraint[key]).strip())
    for key in ("subject_ids", "subjects"):
        value = constraint.get(key)
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
    return list(dict.fromkeys(values))


def _normalise_constraints(shot: dict, asset_map: dict[str, AssetRecord]) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (hard constraints, candidate constraints, unsupported diagnostics)."""
    raw = shot.get("visual_constraints", _shot_visual(shot).get("visual_constraints", []))
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise PlannerError(f"shot {shot.get('shot_id', '')} visual_constraints must be a list of objects")
    hard: list[dict] = []
    candidates: list[dict] = []
    unsupported: list[dict] = []
    for index, source in enumerate(raw):
        item = copy.deepcopy(source)
        kind = str(item.get("kind", "")).strip()
        constraint_id = str(item.get("constraint_id", "")).strip() or f"constraint-{index + 1}"
        item["constraint_id"] = constraint_id
        item["kind"] = kind
        hard_value = item.get("hard", True)
        if not isinstance(hard_value, bool):
            raise PlannerError(f"constraint {constraint_id}.hard must be a boolean")
        item["hard"] = hard_value
        item["provenance"] = item.get("provenance") or item.get("source")
        if not item["provenance"]:
            raise PlannerError(f"constraint {constraint_id} is missing provenance")
        if kind not in SUPPORTED_CONSTRAINT_KINDS:
            if item["hard"]:
                unsupported.append({
                    "constraint_id": constraint_id,
                    "reason": f"unsupported hard constraint kind: {kind or '<missing>'}",
                })
            continue
        if kind == "direction":
            tolerance = _number(item.get("tolerance_degrees", 15), f"{constraint_id}.tolerance_degrees", minimum=0.0)
            if _direction_name(item.get("expected_angle_degrees")) is None or tolerance > 45:
                if item["hard"]:
                    unsupported.append({
                        "constraint_id": constraint_id,
                        "reason": "planner direction supports explicit 45-degree compass angles with tolerance <= 45",
                    })
                continue
        if kind == "containment":
            mode = str(item.get("visibility_mode", "")).strip()
            if mode not in _CONTAINMENT_VISIBILITY_MODES:
                item["status"] = "needs_confirmation"
                item["confirmation_reason"] = (
                    "containment requires visibility_mode: subject_in_front, transparent_window, or layered_container"
                )
            elif mode == "layered_container" and not str(item.get("front_layer_id", "")).strip():
                item["status"] = "needs_confirmation"
                item["confirmation_reason"] = "layered_container requires a distinct front_layer_id"
        item["asset_references"] = _constraint_references(item)
        item["asset_ids"] = [
            record.identity for reference in item["asset_references"]
            if (record := _asset_by_reference(reference, asset_map)) is not None
        ]
        if str(item.get("status", "confirmed")).lower() in {"candidate", "needs_confirmation"}:
            candidates.append(item)
        elif item["hard"]:
            hard.append(item)
    return hard, candidates, unsupported


def _asset_request(reference: str, reason: str, *, shot_id: str) -> dict:
    return {
        "request_id": "asset_request_" + content_fingerprint(reference, reason, shot_id, length=20),
        "asset_reference": reference,
        "shot_id": shot_id,
        "reason": reason,
        "requirements": {
            "transparent_background": True,
            "formal_revision_required": True,
            "provider_neutral": True,
        },
    }


def _background_reference(shot: dict, scene: dict) -> str:
    visual = _shot_visual(shot)
    for value in (
        shot.get("background_asset_id"), visual.get("background_asset_id"),
        scene.get("background_asset_id"), scene.get("location_asset_id"),
    ):
        if str(value or "").strip():
            return str(value).strip()
    return ""


def _ensure_formal(record: AssetRecord, *, allow_fixtures: bool) -> str | None:
    if record.lifecycle == "formal":
        return None
    if record.lifecycle == "fixture" and allow_fixtures:
        return None
    return "formal asset revision is required"


def _direction_name(value: object) -> str | None:
    angle = _number(value, "expected_angle_degrees") % 360.0
    choices = [
        (0.0, "right"), (45.0, "right_below"), (90.0, "below"), (135.0, "left_below"),
        (180.0, "left"), (225.0, "left_above"), (270.0, "above"), (315.0, "right_above"),
    ]
    distance, name = min((min(abs(angle - candidate), 360.0 - abs(angle - candidate)), name) for candidate, name in choices)
    return name if distance <= 0.000001 else None


def _conservative_tangent_ratio(angle_degrees: float, *, rounding: str) -> tuple[int, int]:
    """Return a small rational tangent bound that cannot widen the requested cone."""
    value = math.tan(math.radians(angle_degrees))
    if abs(value) <= 1e-12:
        return 0, 1
    if abs(value - 1.0) <= 1e-12:
        return 1, 1
    fraction = Fraction(value).limit_denominator(10_000)
    if rounding == "below" and float(fraction) > value:
        fraction = Fraction(fraction.numerator - 1, fraction.denominator)
    elif rounding == "above" and float(fraction) < value:
        fraction = Fraction(fraction.numerator + 1, fraction.denominator)
    if fraction.numerator < 0:
        raise PlannerError("direction tangent ratio is invalid")
    return fraction.numerator, fraction.denominator


def _solver_module():
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:
        raise PlannerDependencyError(
            "hybrid planning requires the optional local dependency: pip install 'manju-tool[planner]'"
        ) from exc
    return cp_model


def _solver_version() -> str:
    try:
        import ortools
    except ImportError as exc:
        raise PlannerDependencyError(
            "hybrid planning requires the optional local dependency: pip install 'manju-tool[planner]'"
        ) from exc
    return str(getattr(ortools, "__version__", "unknown"))


def _minimum_render_dimensions(*, canvas_width: int, canvas_height: int) -> tuple[int, int]:
    if canvas_width < MIN_RENDERED_DIMENSION or canvas_height < MIN_RENDERED_DIMENSION:
        raise PlannerError("canvas is smaller than the minimum rendered dimensions")
    return MIN_RENDERED_DIMENSION, MIN_RENDERED_DIMENSION


def _solve_layout(
    assets: list[AssetRecord],
    constraints: list[dict],
    *, canvas_width: int,
    canvas_height: int,
    timeout_seconds: float,
    seed: int,
    preferences: list[dict] | None = None,
) -> tuple[dict[str, dict], list[dict], str | None]:
    """Solve screen-space constraints; returns placements, conflicts, timeout state."""
    cp_model = _solver_module()
    model = cp_model.CpModel()
    vars_by_id: dict[str, dict[str, Any]] = {}
    penalties = []
    count = max(1, len(assets))
    for index, asset in enumerate(assets):
        try:
            min_width, min_height = _minimum_render_dimensions(canvas_width=canvas_width, canvas_height=canvas_height)
        except PlannerError:
            return {}, [{
                "constraint_id": "minimum_render_size",
                "reason": f"{asset.identity} cannot meet the minimum rendered dimensions and area on this canvas",
            }], None
        width = model.NewIntVar(min_width, canvas_width, f"width_{index}")
        height = model.NewIntVar(min_height, canvas_height, f"height_{index}")
        # Preserve the original aspect ratio without using its GCD as a scale
        # unit. One source-pixel tolerance permits integer screen geometry.
        aspect_delta = model.NewIntVar(0, max(canvas_width * asset.height, canvas_height * asset.width), f"aspect_delta_{index}")
        model.AddAbsEquality(aspect_delta, width * asset.height - height * asset.width)
        model.Add(aspect_delta <= asset.height)
        x = model.NewIntVar(0, canvas_width - 1, f"x_{index}")
        y = model.NewIntVar(0, canvas_height - 1, f"y_{index}")
        model.Add(x + width <= canvas_width)
        model.Add(y + height <= canvas_height)
        default_height = max(min_height, min(canvas_height // max(3, count + 1), canvas_height))
        default_width = round(default_height * asset.width / asset.height)
        if default_width > canvas_width:
            default_width = canvas_width
            default_height = round(default_width * asset.height / asset.width)
        default_width = max(min_width, min(canvas_width, default_width))
        default_height = max(min_height, min(canvas_height, default_height))
        default_x = max(0, min(canvas_width - default_width, ((index + 1) * canvas_width // (count + 1)) - default_width // 2))
        default_y = max(0, min(canvas_height - default_height, canvas_height // 2 - default_height // 2))
        # This deterministic, already in-bounds composition is a solver hint,
        # not a contract relaxation.  It prevents broad legal angle cones from
        # spending the entire budget rediscovering an obvious first layout.
        model.AddHint(width, default_width)
        model.AddHint(height, default_height)
        model.AddHint(x, default_x)
        model.AddHint(y, default_y)
        for variable, target, name in ((width, default_width, "width"), (height, default_height, "height"), (x, default_x, "x"), (y, default_y, "y")):
            delta = model.NewIntVar(0, max(canvas_width, canvas_height), f"delta_{name}_{index}")
            model.AddAbsEquality(delta, variable - target)
            penalties.append(delta)
        vars_by_id[asset.identity] = {"asset": asset, "x": x, "y": y, "width": width, "height": height}

    conflicts: list[dict] = []
    no_overlap_pairs: list[tuple[str, str]] = []
    permitted_overlap_pairs: set[frozenset[str]] = set()
    z_edges: list[tuple[str, str, str]] = []
    for constraint_index, constraint in enumerate(constraints):
        kind = constraint["kind"]
        constraint_id = constraint["constraint_id"]
        refs = constraint.get("asset_ids", [])
        if kind in {"presence", "minimum_visibility"}:
            continue
        if kind == "count":
            expected = _integer(constraint.get("expected_count"), f"{constraint_id}.expected_count", minimum=0)
            subject_ids = constraint.get("asset_ids", [])
            if expected != len(set(subject_ids)):
                conflicts.append({
                    "constraint_id": constraint_id,
                    "reason": f"declared count {expected} does not match the {len(set(subject_ids))} registered planned assets",
                })
            continue
        if kind == "relative_size":
            subject = _asset_by_reference(constraint.get("subject_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            reference = _asset_by_reference(constraint.get("reference_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            if subject is None or reference is None:
                conflicts.append({"constraint_id": constraint_id, "reason": "relative size references a missing asset"})
                continue
            dimension = str(constraint.get("dimension", "width"))
            if dimension not in {"width", "height"}:
                conflicts.append({"constraint_id": constraint_id, "reason": "relative size dimension must be width or height"})
                continue
            minimum = _number(constraint.get("min_ratio"), f"{constraint_id}.min_ratio", minimum=0.0)
            maximum = _number(constraint.get("max_ratio"), f"{constraint_id}.max_ratio", minimum=minimum)
            lower, upper = round(minimum * 10000), round(maximum * 10000)
            model.Add(vars_by_id[subject.identity][dimension] * 10000 >= lower * vars_by_id[reference.identity][dimension])
            model.Add(vars_by_id[subject.identity][dimension] * 10000 <= upper * vars_by_id[reference.identity][dimension])
        elif kind == "direction":
            source = _asset_by_reference(constraint.get("source_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            target = _asset_by_reference(constraint.get("target_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            direction = _direction_name(constraint.get("expected_angle_degrees"))
            tolerance = _number(constraint.get("tolerance_degrees", 15), f"{constraint_id}.tolerance_degrees", minimum=0.0)
            if source is None or target is None or direction is None or tolerance > 45:
                conflicts.append({"constraint_id": constraint_id, "reason": "direction must reference assets and use a cardinal angle with tolerance <= 45"})
                continue
            left, right = vars_by_id[source.identity], vars_by_id[target.identity]
            dx = (2 * right["x"] + right["width"]) - (2 * left["x"] + left["width"])
            dy = (2 * right["y"] + right["height"]) - (2 * left["y"] + left["height"])
            # Centers use doubled integer coordinates.  Constrain the angle as
            # a cone rather than demanding exact alignment: odd/even asset
            # sizes can make a legal direction impossible to express exactly.
            tangent_numerator, tangent_denominator = _conservative_tangent_ratio(tolerance, rounding="below")
            if direction == "right":
                model.Add(dx >= 1)
                model.Add(dy * tangent_denominator <= tangent_numerator * dx)
                model.Add((-dy) * tangent_denominator <= tangent_numerator * dx)
            elif direction == "left":
                model.Add(dx <= -1)
                model.Add(dy * tangent_denominator <= tangent_numerator * (-dx))
                model.Add((-dy) * tangent_denominator <= tangent_numerator * (-dx))
            elif direction == "below":
                model.Add(dy >= 1)
                model.Add(dx * tangent_denominator <= tangent_numerator * dy)
                model.Add((-dx) * tangent_denominator <= tangent_numerator * dy)
            elif direction == "above":
                model.Add(dy <= -1)
                model.Add(dx * tangent_denominator <= tangent_numerator * (-dy))
                model.Add((-dx) * tangent_denominator <= tangent_numerator * (-dy))
            else:
                horizontal = dx if direction in {"right_below", "right_above"} else -dx
                vertical = dy if direction in {"right_below", "left_below"} else -dy
                model.Add(horizontal >= 0)
                model.Add(vertical >= 0)
                model.Add(horizontal + vertical >= 1)
                lower_numerator, lower_denominator = _conservative_tangent_ratio(45.0 - tolerance, rounding="above")
                if tolerance < 45.0:
                    upper_numerator, upper_denominator = _conservative_tangent_ratio(45.0 + tolerance, rounding="below")
                    model.Add(vertical * upper_denominator <= upper_numerator * horizontal)
                model.Add(vertical * lower_denominator >= lower_numerator * horizontal)
        elif kind == "containment":
            subject = _asset_by_reference(constraint.get("subject_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            container = _asset_by_reference(constraint.get("container_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            if subject is None or container is None:
                conflicts.append({"constraint_id": constraint_id, "reason": "containment references a missing asset"})
                continue
            child, parent = vars_by_id[subject.identity], vars_by_id[container.identity]
            permitted_overlap_pairs.add(frozenset({subject.identity, container.identity}))
            model.Add(child["x"] >= parent["x"])
            model.Add(child["y"] >= parent["y"])
            model.Add(child["x"] + child["width"] <= parent["x"] + parent["width"])
            model.Add(child["y"] + child["height"] <= parent["y"] + parent["height"])
            mode = str(constraint.get("visibility_mode", ""))
            if mode == "subject_in_front":
                model.Add(child["width"] + 1 <= parent["width"])
                model.Add(child["height"] + 1 <= parent["height"])
                z_edges.append((container.identity, subject.identity, constraint_id))
            elif mode == "transparent_window":
                z_edges.append((subject.identity, container.identity, constraint_id))
            elif mode == "layered_container":
                front = _asset_by_reference(constraint.get("front_layer_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
                if front is None:
                    conflicts.append({"constraint_id": constraint_id, "reason": "layered container references a missing front layer"})
                else:
                    front_layer = vars_by_id[front.identity]
                    permitted_overlap_pairs.add(frozenset({subject.identity, front.identity}))
                    permitted_overlap_pairs.add(frozenset({container.identity, front.identity}))
                    model.Add(front_layer["x"] == parent["x"])
                    model.Add(front_layer["y"] == parent["y"])
                    model.Add(front_layer["width"] == parent["width"])
                    model.Add(front_layer["height"] == parent["height"])
                    z_edges.append((container.identity, subject.identity, constraint_id))
                    z_edges.append((subject.identity, front.identity, constraint_id))
        elif kind == "safe_area":
            references = refs or [item.identity for item in assets]
            margin = _integer(constraint.get("margin_px", 0), f"{constraint_id}.margin_px", minimum=0)
            for reference in references:
                record = _asset_by_reference(reference, {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
                if record is None:
                    conflicts.append({"constraint_id": constraint_id, "reason": "safe area references a missing asset"})
                    continue
                item = vars_by_id[record.identity]
                model.Add(item["x"] >= margin)
                model.Add(item["y"] >= margin)
                model.Add(item["x"] + item["width"] <= canvas_width - margin)
                model.Add(item["y"] + item["height"] <= canvas_height - margin)
        elif kind == "forbidden_occlusion":
            if len(refs) != 2:
                conflicts.append({"constraint_id": constraint_id, "reason": "forbidden occlusion requires exactly two assets"})
            else:
                no_overlap_pairs.append((refs[0], refs[1]))
        elif kind == "z_order":
            lower = _asset_by_reference(constraint.get("behind_id") or constraint.get("subject_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            upper = _asset_by_reference(constraint.get("front_id") or constraint.get("reference_id"), {item["asset"].identity: item["asset"] for item in vars_by_id.values()})
            if lower is None or upper is None:
                conflicts.append({"constraint_id": constraint_id, "reason": "z order references a missing asset"})
            else:
                z_edges.append((lower.identity, upper.identity, constraint_id))
    if conflicts:
        return {}, conflicts, None
    # A non-overlapping composition is the default soft layout policy.  A
    # declared containment relation is the explicit exception; other intended
    # overlap must be represented as a dedicated future constraint rather than
    # occurring accidentally through a model preference.
    for first_index, first_asset in enumerate(assets):
        for second_asset in assets[first_index + 1:]:
            pair = (first_asset.identity, second_asset.identity)
            if frozenset(pair) not in permitted_overlap_pairs and pair not in no_overlap_pairs and (pair[1], pair[0]) not in no_overlap_pairs:
                no_overlap_pairs.append(pair)
    for first, second in no_overlap_pairs:
        left, right = vars_by_id[first], vars_by_id[second]
        x1_end = model.NewIntVar(1, canvas_width, f"xend_{first}")
        y1_end = model.NewIntVar(1, canvas_height, f"yend_{first}")
        x2_end = model.NewIntVar(1, canvas_width, f"xend_{second}")
        y2_end = model.NewIntVar(1, canvas_height, f"yend_{second}")
        model.Add(x1_end == left["x"] + left["width"])
        model.Add(y1_end == left["y"] + left["height"])
        model.Add(x2_end == right["x"] + right["width"])
        model.Add(y2_end == right["y"] + right["height"])
        model.AddNoOverlap2D(
            [model.NewIntervalVar(left["x"], left["width"], x1_end, f"xint_{first}"), model.NewIntervalVar(right["x"], right["width"], x2_end, f"xint_{second}")],
            [model.NewIntervalVar(left["y"], left["height"], y1_end, f"yint_{first}"), model.NewIntervalVar(right["y"], right["height"], y2_end, f"yint_{second}")],
        )
    planner_assets = {item["asset"].identity: item["asset"] for item in vars_by_id.values()}
    for preference_index, preference in enumerate(preferences or []):
        name = preference["preference"]
        weight = max(1, int(preference["priority"]) // 10)
        affected = [
            record.identity for reference in preference.get("affected_asset_ids", [])
            if (record := _asset_by_reference(reference, planner_assets)) is not None
        ]
        if not affected:
            affected = [asset.identity for asset in assets]
        if name == "keep_subjects_close" and len(affected) >= 2:
            first, second = vars_by_id[affected[0]], vars_by_id[affected[1]]
            for axis, extent, bound in (("x", "width", canvas_width * 2), ("y", "height", canvas_height * 2)):
                delta = model.NewIntVar(0, bound, f"preference_close_{preference_index}_{axis}")
                model.AddAbsEquality(delta, (2 * first[axis] + first[extent]) - (2 * second[axis] + second[extent]))
                penalties.append(delta * weight)
        elif name == "center_subjects":
            for asset_index, identity in enumerate(affected):
                item = vars_by_id[identity]
                delta = model.NewIntVar(0, canvas_width * 2, f"preference_center_{preference_index}_{asset_index}")
                model.AddAbsEquality(delta, 2 * item["x"] + item["width"] - canvas_width)
                penalties.append(delta * weight)
        elif name == "spread_subjects" and len(affected) >= 2:
            # The default composition already spreads subjects.  A bounded
            # reward is represented as a penalty for falling below half-canvas
            # separation, keeping the objective deterministic and linear.
            for pair_index, (first_id, second_id) in enumerate(zip(affected, affected[1:])):
                first, second = vars_by_id[first_id], vars_by_id[second_id]
                delta = model.NewIntVar(0, canvas_width * 2, f"preference_spread_{preference_index}_{pair_index}")
                model.AddAbsEquality(delta, (2 * first["x"] + first["width"]) - (2 * second["x"] + second["width"]))
                shortfall = model.NewIntVar(0, canvas_width * 2, f"preference_spread_shortfall_{preference_index}_{pair_index}")
                model.AddMaxEquality(shortfall, [0, canvas_width - delta])
                penalties.append(shortfall * weight)
    if penalties:
        model.Minimize(sum(penalties))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = timeout_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    status = solver.Solve(model)
    if status == cp_model.UNKNOWN:
        return {}, [], BLOCKED_SOLVER_TIMEOUT
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {}, [{"constraint_id": item["constraint_id"], "reason": "no feasible layout"} for item in constraints], None
    # Derive a deterministic z order after geometry solving, including real
    # cycle detection rather than letting a later edge silently overwrite an
    # earlier relation.
    successors = {asset.identity: set() for asset in assets}
    incoming = {asset.identity: 0 for asset in assets}
    edge_ids: dict[tuple[str, str], str] = {}
    for lower, upper, constraint_id in z_edges:
        if lower == upper:
            return {}, [{"constraint_id": constraint_id, "reason": "an asset cannot be both behind and in front of itself"}], None
        if upper not in successors[lower]:
            successors[lower].add(upper)
            incoming[upper] += 1
            edge_ids[(lower, upper)] = constraint_id
    ordered = [asset.identity for asset in assets if incoming[asset.identity] == 0]
    cursor = 0
    while cursor < len(ordered):
        identity = ordered[cursor]
        cursor += 1
        for successor in sorted(successors[identity]):
            incoming[successor] -= 1
            if incoming[successor] == 0:
                ordered.append(successor)
    if len(ordered) != len(assets):
        cyclic = [identity for identity, degree in incoming.items() if degree]
        related = [constraint_id for (lower, upper), constraint_id in edge_ids.items() if lower in cyclic and upper in cyclic]
        return {}, [{"constraint_id": constraint_id, "reason": "z order cycle has no valid front-to-back order"} for constraint_id in related], None
    z_scores = {identity: index for index, identity in enumerate(ordered)}
    placements: dict[str, dict] = {}
    for identity, item in vars_by_id.items():
        placements[identity] = {
            "x": solver.Value(item["x"]), "y": solver.Value(item["y"]),
            "width": solver.Value(item["width"]), "height": solver.Value(item["height"]),
            "z_index": z_scores[identity],
        }
    return placements, [], None


def _planned_visibility_coverages(scene: dict) -> dict[str, float]:
    """Measure planned alpha visibility before a scene can become ready."""
    canvas = scene["canvas"]
    canvas_size = (int(canvas["width"]), int(canvas["height"]))
    covered = Image.new("L", canvas_size, 0)
    coverages: dict[str, float] = {}
    for layer in sorted(scene["layers"], key=lambda value: (int(value["z_index"]), str(value["asset_id"])), reverse=True):
        try:
            with Image.open(str(layer["image_path"])) as source:
                raster = source.convert("RGBA").resize((int(layer["width"]), int(layer["height"])), Image.Resampling.LANCZOS)
        except (OSError, ValueError) as exc:
            raise PlannerError(f"planned visibility preflight cannot load {layer.get('asset_id')}: {exc}") from exc
        alpha = raster.getchannel("A")
        full = Image.new("L", canvas_size, 0)
        x, y = int(layer["x"]), int(layer["y"])
        left, top = max(0, x), max(0, y)
        right, bottom = min(canvas_size[0], x + alpha.width), min(canvas_size[1], y + alpha.height)
        if left < right and top < bottom:
            full.paste(alpha.crop((left - x, top - y, right - x, bottom - y)), (left, top))
        residual = ImageChops.multiply(full, ImageChops.invert(covered))
        total = max(1, alpha.width * alpha.height)
        coverages[str(layer["asset_id"])] = sum(1 for value in residual.tobytes() if value) / total
        covered = ImageChops.lighter(covered, full)
    return coverages


def _planned_alpha_at(layer: dict, *, center_x: float, center_y: float) -> int:
    """Sample a planned layer's rendered alpha at a canvas-space position."""
    try:
        with Image.open(str(layer["image_path"])) as source:
            raster = source.convert("RGBA").resize(
                (int(layer["width"]), int(layer["height"])), Image.Resampling.LANCZOS,
            )
    except (OSError, ValueError) as exc:
        raise PlannerError(f"planned containment preflight cannot load {layer.get('asset_id')}: {exc}") from exc
    sample_x = int(math.floor(center_x - int(layer["x"])))
    sample_y = int(math.floor(center_y - int(layer["y"])))
    if not 0 <= sample_x < raster.width or not 0 <= sample_y < raster.height:
        return 0
    return int(raster.getchannel("A").getpixel((sample_x, sample_y)))


def _hybrid_constraints(constraints: list[dict], assets: list[AssetRecord]) -> list[dict]:
    ids = {asset.asset_id: asset.identity for asset in assets}
    output: list[dict] = []
    for constraint in constraints:
        kind = constraint["kind"]
        if kind == "relative_size":
            output.append({
                "constraint_id": constraint["constraint_id"], "kind": "relative_size",
                "subject_id": ids.get(str(constraint.get("subject_id")), str(constraint.get("subject_id"))),
                "reference_id": ids.get(str(constraint.get("reference_id")), str(constraint.get("reference_id"))),
                "dimension": str(constraint.get("dimension", "width")),
                "min_ratio": constraint.get("min_ratio"), "max_ratio": constraint.get("max_ratio"),
            })
        elif kind == "direction":
            output.append({
                "constraint_id": constraint["constraint_id"], "kind": "direction",
                "source_id": ids.get(str(constraint.get("source_id")), str(constraint.get("source_id"))),
                "target_id": ids.get(str(constraint.get("target_id")), str(constraint.get("target_id"))),
                "expected_angle_degrees": constraint.get("expected_angle_degrees"),
                "tolerance_degrees": constraint.get("tolerance_degrees", 15),
            })
        elif kind == "containment":
            item = {
                "constraint_id": constraint["constraint_id"], "kind": "contains_center",
                "subject_id": ids.get(str(constraint.get("subject_id")), str(constraint.get("subject_id"))),
                "container_id": ids.get(str(constraint.get("container_id")), str(constraint.get("container_id"))),
                "visibility_mode": constraint.get("visibility_mode"),
            }
            if constraint.get("visibility_mode") == "layered_container":
                item["front_layer_id"] = ids.get(str(constraint.get("front_layer_id")), str(constraint.get("front_layer_id")))
            output.append(item)
    output.append({
        "constraint_id": "planned_visible_count",
        "kind": "count",
        "subject_ids": [asset.identity for asset in assets],
        "expected_count": len(assets),
    })
    return output


def _verify_scene(scene: dict, *, plan_constraints: list[dict]) -> list[dict]:
    """Independent geometry verification of the scene contract, without the solver."""
    canvas = scene["canvas"]
    layers = {str(item["asset_id"]): item for item in scene["layers"]}
    alpha_coverages: dict[str, float] | None = None

    def layer_reference(value: object) -> str:
        reference = str(value or "")
        if reference in layers:
            return reference
        matches = [identity for identity in layers if identity.split("@", 1)[0] == reference]
        if len(matches) == 1:
            return matches[0]
        raise KeyError(reference)

    verdicts: list[dict] = []
    for constraint in plan_constraints:
        constraint_id = constraint["constraint_id"]
        kind = constraint["kind"]
        passed = True
        evidence: dict[str, Any] = {}
        try:
            if kind == "relative_size":
                subject = layers[layer_reference(constraint["subject_id"])]
                reference = layers[layer_reference(constraint["reference_id"])]
                dimension = str(constraint.get("dimension", "width"))
                ratio = subject[dimension] / reference[dimension]
                passed = float(constraint["min_ratio"]) <= ratio <= float(constraint["max_ratio"])
                evidence = {"ratio": ratio}
            elif kind == "direction":
                source = layers[layer_reference(constraint["source_id"])]
                target = layers[layer_reference(constraint["target_id"])]
                dx = target["x"] + target["width"] / 2 - source["x"] - source["width"] / 2
                dy = target["y"] + target["height"] / 2 - source["y"] - source["height"] / 2
                angle = math.degrees(math.atan2(dy, dx)) % 360
                expected = float(constraint["expected_angle_degrees"]) % 360
                distance = min(abs(angle - expected), 360 - abs(angle - expected))
                passed = distance <= float(constraint.get("tolerance_degrees", 15))
                evidence = {"angle_degrees": angle, "distance_degrees": distance}
            elif kind == "containment":
                subject = layers[layer_reference(constraint["subject_id"])]
                container = layers[layer_reference(constraint["container_id"])]
                within_box = (
                    subject["x"] >= container["x"] and subject["y"] >= container["y"]
                    and subject["x"] + subject["width"] <= container["x"] + container["width"]
                    and subject["y"] + subject["height"] <= container["y"] + container["height"]
                )
                mode = str(constraint.get("visibility_mode", "")).strip()
                evidence = {"visibility_mode": mode or "legacy_opaque_surface", "within_box": within_box}
                if not mode:
                    passed = within_box
                elif mode not in _CONTAINMENT_VISIBILITY_MODES:
                    raise PlannerError("containment visibility_mode is invalid")
                elif not all(str(layer.get("image_path", "")).strip() for layer in layers.values()):
                    passed = within_box
                else:
                    subject_id = str(subject["asset_id"])
                    center_x = subject["x"] + subject["width"] / 2
                    center_y = subject["y"] + subject["height"] / 2
                    container_alpha = _planned_alpha_at(container, center_x=center_x, center_y=center_y)
                    if alpha_coverages is None:
                        alpha_coverages = _planned_visibility_coverages(scene)
                    subject_visible = alpha_coverages.get(subject_id, 0.0) >= float(subject.get("minimum_visible_coverage", 0.01))
                    passed = within_box and subject_visible
                    evidence.update({
                        "subject_center": [center_x, center_y], "container_alpha": container_alpha,
                        "subject_visible_coverage": alpha_coverages.get(subject_id, 0.0),
                    })
                    if mode == "transparent_window":
                        passed = passed and container_alpha < 255
                    elif mode == "layered_container":
                        front = layers[layer_reference(constraint.get("front_layer_id"))]
                        aligned = all(front[key] == container[key] for key in ("x", "y", "width", "height"))
                        ordered = container["z_index"] < subject["z_index"] < front["z_index"]
                        passed = passed and aligned and ordered
                        evidence.update({"front_layer_aligned": aligned, "layer_ordered": ordered})
            elif kind == "safe_area":
                margin = _integer(constraint.get("margin_px", 0), f"{constraint_id}.margin_px", minimum=0)
                references = constraint.get("asset_ids") or list(layers)
                passed = all(
                    layers[item]["x"] >= margin and layers[item]["y"] >= margin
                    and layers[item]["x"] + layers[item]["width"] <= canvas["width"] - margin
                    and layers[item]["y"] + layers[item]["height"] <= canvas["height"] - margin
                    for item in references
                )
            elif kind == "presence":
                references = constraint.get("asset_ids") or _constraint_references(constraint)
                passed = all(reference in layers for reference in references)
                evidence = {"actual_count": len([reference for reference in references if reference in layers]), "expected_count": len(set(references))}
            elif kind == "count":
                references = constraint.get("asset_ids") or _constraint_references(constraint)
                expected = _integer(constraint.get("expected_count"), f"{constraint_id}.expected_count", minimum=0)
                actual = len({reference for reference in references if reference in layers})
                passed = actual == expected and actual == len(set(references))
                evidence = {"actual_count": actual, "expected_count": expected}
            elif kind == "minimum_visibility":
                references = constraint.get("asset_ids") or _constraint_references(constraint)
                threshold = _number(constraint.get("minimum_visible_coverage", constraint.get("minimum_coverage", 0.01)), f"{constraint_id}.minimum_visible_coverage", minimum=0.0)
                if threshold > 1:
                    raise PlannerError("minimum visible coverage must be at most 1")
                if alpha_coverages is None and all(str(layer.get("image_path", "")).strip() for layer in layers.values()):
                    alpha_coverages = _planned_visibility_coverages(scene)
                coverages = {}
                for reference in references:
                    layer = layers[reference]
                    if alpha_coverages is not None:
                        coverages[reference] = alpha_coverages[reference]
                    else:
                        total = max(1, layer["width"] * layer["height"])
                        visible_width = max(0, min(canvas["width"], layer["x"] + layer["width"]) - max(0, layer["x"]))
                        visible_height = max(0, min(canvas["height"], layer["y"] + layer["height"]) - max(0, layer["y"]))
                        coverages[reference] = visible_width * visible_height / total
                passed = bool(coverages) and all(value >= threshold and value > 0 for value in coverages.values())
                evidence = {"minimum_visible_coverage": threshold, "alpha_coverage": coverages}
            elif kind == "forbidden_occlusion":
                references = constraint.get("asset_ids") or _constraint_references(constraint)
                if len(references) != 2:
                    raise PlannerError("forbidden occlusion requires exactly two assets")
                first, second = layers[references[0]], layers[references[1]]
                overlap_width = max(0, min(first["x"] + first["width"], second["x"] + second["width"]) - max(first["x"], second["x"]))
                overlap_height = max(0, min(first["y"] + first["height"], second["y"] + second["height"]) - max(first["y"], second["y"]))
                overlap_pixels = overlap_width * overlap_height
                passed = overlap_pixels == 0
                evidence = {"overlap_pixels": overlap_pixels}
            elif kind == "z_order":
                references = constraint.get("asset_ids") or _constraint_references(constraint)
                if len(references) != 2:
                    raise PlannerError("z order requires exactly two assets")
                lower, upper = references
                passed = layers[lower]["z_index"] < layers[upper]["z_index"]
                evidence = {"behind_z_index": layers[lower]["z_index"], "front_z_index": layers[upper]["z_index"]}
            else:
                continue
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            passed = False
            evidence = {"reason": str(exc)}
        verdicts.append({"constraint_id": constraint_id, "kind": kind, "verdict": "pass" if passed else "fail", "evidence": evidence})
    return verdicts


def _scene_plan(
    shot: dict,
    scene: dict,
    storyboard: dict,
    assets: dict[str, AssetRecord],
    *, allow_fixtures: bool,
    timeout_seconds: float,
    seed: int,
) -> dict:
    shot_id = _required_text(shot.get("shot_id"), "shot_id")
    if not _SAFE_SHOT_ID.fullmatch(shot_id):
        raise PlannerError("shot_id must be a single safe filename component")
    hard_constraints, candidates, unsupported = _normalise_constraints(shot, assets)
    preferences, preferences_need_review = _normalise_preferences(shot)
    references = _shot_asset_references(shot)
    for constraint in hard_constraints:
        references.extend(constraint["asset_references"])
    references = list(dict.fromkeys(references))
    requests: list[dict] = []
    resolved: list[AssetRecord] = []
    for reference in references:
        record = _asset_by_reference(reference, assets)
        if record is None:
            requests.append(_asset_request(reference, "asset is not registered", shot_id=shot_id))
            continue
        lifecycle_reason = _ensure_formal(record, allow_fixtures=allow_fixtures)
        if lifecycle_reason:
            requests.append(_asset_request(reference, lifecycle_reason, shot_id=shot_id))
            continue
        if record not in resolved:
            resolved.append(record)
    background_reference = _background_reference(shot, scene)
    background = _asset_by_reference(background_reference, assets)
    if background is None:
        requests.append(_asset_request(background_reference or "background", "background asset is not registered", shot_id=shot_id))
    elif _ensure_formal(background, allow_fixtures=allow_fixtures):
        requests.append(_asset_request(background_reference, "formal background revision is required", shot_id=shot_id))
    if unsupported:
        return {"shot_id": shot_id, "status": BLOCKED_UNSUPPORTED, "asset_requests": requests, "unsupported": unsupported, "candidate_constraints": candidates}
    if requests:
        return {"shot_id": shot_id, "status": NEEDS_ASSETS, "asset_requests": requests, "candidate_constraints": candidates}
    if candidates:
        return {"shot_id": shot_id, "status": NEEDS_CONSTRAINT_CONFIRMATION, "asset_requests": [], "candidate_constraints": candidates}
    if not resolved:
        return {"shot_id": shot_id, "status": NEEDS_ASSETS, "asset_requests": [_asset_request("visible_asset", "shot has no registered visible assets", shot_id=shot_id)]}
    resolved_assets = {record.identity: record for record in resolved}
    for preference in preferences:
        affected = preference["affected_asset_ids"]
        normalised_affected: list[str] = []
        for reference in affected:
            record = _asset_by_reference(reference, assets)
            if record is None or record.identity not in resolved_assets:
                raise PlannerError(
                    f"layout preference {preference['preference']} references an unknown or non-visible asset: {reference}"
                )
            normalised_affected.append(record.identity)
        preference["affected_asset_ids"] = list(dict.fromkeys(normalised_affected))
    width, height = _shot_canvas(storyboard, shot)
    started = time.monotonic()
    placements, conflicts, solver_state = _solve_layout(
        resolved, hard_constraints, canvas_width=width, canvas_height=height,
        timeout_seconds=timeout_seconds, seed=seed, preferences=preferences,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    if solver_state:
        return {"shot_id": shot_id, "status": solver_state, "solver": {"elapsed_ms": elapsed_ms}, "conflicts": conflicts}
    if conflicts:
        return {"shot_id": shot_id, "status": BLOCKED_CONFLICT, "solver": {"elapsed_ms": elapsed_ms}, "conflicts": conflicts}
    # Every planned layer receives a minimum alpha-visibility contract.
    minimum_coverage = {asset.identity: 0.01 for asset in resolved}
    human_review_required = preferences_need_review
    for constraint in hard_constraints:
        if constraint["kind"] == "minimum_visibility":
            value = _number(constraint.get("minimum_visible_coverage", constraint.get("minimum_coverage", 0.01)), f"{constraint['constraint_id']}.minimum_visible_coverage", minimum=0.0)
            if value > 1:
                raise PlannerError("minimum visible coverage must be at most 1")
            for identity in constraint.get("asset_ids", []):
                minimum_coverage[identity] = max(minimum_coverage.get(identity, 0.01), value)
    layers = []
    for record in resolved:
        placement = placements[record.identity]
        layers.append({
            "asset_id": record.identity, "image_path": record.image_path,
            **placement, "minimum_visible_coverage": minimum_coverage[record.identity],
            "human_review_required": human_review_required,
        })
    hybrid_constraints = _hybrid_constraints(hard_constraints, resolved)
    scene_json = {
        "schema_version": HYBRID_SCHEMA_VERSION,
        "canvas": {"width": width, "height": height},
        "background_path": background.image_path,
        "layers": layers,
        "constraints": hybrid_constraints,
    }
    verification = _verify_scene(scene_json, plan_constraints=hard_constraints)
    failed = [item["constraint_id"] for item in verification if item["verdict"] != "pass"]
    visibility_coverages = _planned_visibility_coverages(scene_json)
    layer_visibility_failures = [
        {
            "asset_id": str(layer["asset_id"]),
            "visible_coverage": visibility_coverages[str(layer["asset_id"])],
            "minimum_visible_coverage": float(layer["minimum_visible_coverage"]),
        }
        for layer in layers
        if visibility_coverages[str(layer["asset_id"])] < float(layer["minimum_visible_coverage"])
    ]
    structural_visibility_failures = []
    for constraint in hard_constraints:
        if constraint["kind"] != "containment":
            continue
        subject = _asset_by_reference(constraint.get("subject_id"), resolved_assets)
        if subject is None:
            continue
        coverage = visibility_coverages[subject.identity]
        required_coverage = max(0.01, minimum_coverage[subject.identity])
        if coverage < required_coverage:
            structural_visibility_failures.append({
                "constraint_id": constraint["constraint_id"],
                "subject_id": subject.identity,
                "visibility_mode": constraint.get("visibility_mode"),
                "visible_coverage": coverage,
                "minimum_visible_coverage": required_coverage,
            })
    structural_constraint_failures = [
        item for item in verification
        if item["kind"] == "containment" and item["verdict"] != "pass"
    ]
    status = NEEDS_HUMAN_REVIEW if human_review_required else READY
    if structural_visibility_failures or structural_constraint_failures:
        status = SCENE_CONTRACT_REVISION_REQUIRED
    elif failed or layer_visibility_failures:
        status = BLOCKED_CONFLICT
    return {
        "shot_id": shot_id,
        "status": status,
        "canvas": scene_json["canvas"],
        "assets": [record.to_dict() for record in resolved],
        "background": background.to_dict(),
        "hard_constraints": hard_constraints,
        "layout_preferences": preferences,
        "continuity": dict(shot.get("continuity", {})) if isinstance(shot.get("continuity"), dict) else {},
        "scene": scene_json,
        "independent_verification": verification,
        "visibility_preflight": {
            "coverages": visibility_coverages,
            "failures": layer_visibility_failures,
            "structural_failures": structural_visibility_failures,
            "structural_constraint_failures": structural_constraint_failures,
        },
        "failed_constraint_ids": failed,
        "solver": {
            "solver": "ortools-cp-sat", "seed": seed, "timeout_seconds": timeout_seconds,
            "workers": 1, "elapsed_ms": elapsed_ms, "policy_version": PLANNER_POLICY_VERSION,
            "ortools_version": _solver_version(),
        },
    }


def _iter_storyboard_shots(storyboard: dict) -> Iterable[tuple[dict, dict]]:
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise PlannerError("storyboard requires a non-empty scenes list")
    seen: set[str] = set()
    for scene_index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise PlannerError(f"scenes[{scene_index - 1}] must be an object")
        shots = scene.get("shots")
        if not isinstance(shots, list) or not shots:
            raise PlannerError(f"scenes[{scene_index - 1}] requires shots")
        for shot_index, shot in enumerate(shots, 1):
            if not isinstance(shot, dict):
                raise PlannerError(f"scene {scene_index} shot {shot_index} must be an object")
            shot_id = _required_text(shot.get("shot_id"), f"scene {scene_index} shot {shot_index}.shot_id")
            if shot_id in seen:
                raise PlannerError(f"duplicate shot_id: {shot_id}")
            seen.add(shot_id)
            yield scene, shot


def _plan_status(shots: list[dict]) -> str:
    statuses = {shot["status"] for shot in shots}
    for status in (SCENE_CONTRACT_REVISION_REQUIRED, BLOCKED_INTEGRITY, BLOCKED_CONTINUITY, BLOCKED_CONFLICT, BLOCKED_UNSUPPORTED, BLOCKED_SOLVER_TIMEOUT):
        if status in statuses:
            return status
    for status in (NEEDS_ASSETS, NEEDS_CONSTRAINT_CONFIRMATION, NEEDS_HUMAN_REVIEW):
        if status in statuses:
            return status
    return READY


def _continuity_verdicts(shots: list[dict]) -> list[dict]:
    """Check only explicitly declared persistent assets and states."""
    seen: dict[str, tuple[str, int, int]] = {}
    states: dict[str, str] = {}
    verdicts: list[dict] = []
    for shot in shots:
        continuity = shot.get("continuity", {}) if isinstance(shot.get("continuity"), dict) else {}
        persistent = {str(value) for value in continuity.get("persistent_asset_ids", []) if str(value).strip()} if isinstance(continuity.get("persistent_asset_ids", []), list) else set()
        override = bool(continuity.get("structured_event") or continuity.get("override"))
        planned_layers = {
            str(layer.get("asset_id", "")): layer
            for layer in shot.get("scene", {}).get("layers", [])
            if isinstance(layer, dict)
        }
        for asset in shot.get("assets", []):
            asset_id = str(asset.get("asset_id", ""))
            revision = str(asset.get("revision", ""))
            layer = planned_layers.get(str(asset.get("identity", "")), {})
            planned_size = (int(layer.get("width", 0)), int(layer.get("height", 0)))
            prior = seen.get(asset_id)
            if prior is not None and prior[0] != revision:
                verdicts.append({
                    "verdict": "fail", "asset_id": asset_id, "previous_revision": prior[0],
                    "revision": revision, "shot_id": shot["shot_id"], "reason": "asset revision drift has no structured override",
                })
            if asset_id in persistent and prior is not None and prior[1:] != planned_size and not override:
                verdicts.append({
                    "verdict": "fail", "asset_id": asset_id, "shot_id": shot["shot_id"],
                    "reason": "persistent asset scale drift has no structured event or override",
                    "previous_size": list(prior[1:]), "size": list(planned_size),
                })
            seen[asset_id] = (revision, *planned_size)
        persistent_state = continuity.get("persistent_state", {})
        if isinstance(persistent_state, dict):
            for key, value in persistent_state.items():
                fingerprint = content_fingerprint(value, length=32)
                state_key = str(key)
                if state_key in states and states[state_key] != fingerprint and not override:
                    verdicts.append({
                        "verdict": "fail", "state": state_key, "shot_id": shot["shot_id"],
                        "reason": "persistent state drift has no structured event or override",
                    })
                states[state_key] = fingerprint
    return verdicts


def plan_hybrid_storyboard(
    storyboard: dict,
    asset_registry: dict,
    *,
    storyboard_dir: str,
    registry_dir: str,
    allow_fixtures: bool = False,
    timeout_seconds: float = DEFAULT_SOLVER_TIMEOUT_SECONDS,
    seed: int = DEFAULT_SOLVER_SEED,
) -> dict:
    """Compile structured storyboard facts into versioned, auditable scene plans."""
    if not isinstance(storyboard, dict):
        raise PlannerError("storyboard must be an object")
    if timeout_seconds <= 0 or timeout_seconds > DEFAULT_SOLVER_TIMEOUT_SECONDS:
        raise PlannerError(f"timeout_seconds must be in (0, {DEFAULT_SOLVER_TIMEOUT_SECONDS}]")
    assets, roots = _read_asset_registry(asset_registry, base_dir=registry_dir)
    shot_plans = [
        _scene_plan(shot, scene, storyboard, assets, allow_fixtures=allow_fixtures, timeout_seconds=timeout_seconds, seed=seed)
        for scene, shot in _iter_storyboard_shots(storyboard)
    ]
    continuity = _continuity_verdicts(shot_plans)
    if any(item["verdict"] == "fail" for item in continuity):
        for shot in shot_plans:
            if shot["status"] == READY:
                shot["status"] = BLOCKED_CONTINUITY
    plan = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "policy_version": PLANNER_POLICY_VERSION,
        "status": _plan_status(shot_plans),
        "input": {
            "storyboard_fingerprint": content_fingerprint(storyboard, length=64),
            "asset_registry_fingerprint": content_fingerprint(asset_registry, length=64),
            "storyboard_dir": os.path.realpath(storyboard_dir),
            "asset_roots": roots,
        },
        "solver": {
            "name": "ortools-cp-sat", "seed": seed, "workers": 1,
            "timeout_seconds": timeout_seconds, "policy_version": PLANNER_POLICY_VERSION,
            "ortools_version": sorted({str(shot.get("solver", {}).get("ortools_version", "")) for shot in shot_plans if isinstance(shot.get("solver"), dict)}),
        },
        "global_assets": [record.to_dict() for record in sorted(assets.values(), key=lambda item: item.identity)],
        "shots": shot_plans,
        "continuity_verdicts": continuity,
        "render_replan_limit": 3,
    }
    plan["fingerprint"] = content_fingerprint({key: value for key, value in plan.items() if key != "fingerprint"}, length=64)
    return plan


def _plan_paths(output_dir: str) -> tuple[str, str, str]:
    root = os.path.abspath(output_dir)
    return root, os.path.join(root, "hybrid_plan.json"), os.path.join(root, "hybrid_plan_manifest.json")


def _stable_plan_fingerprint(plan: dict) -> str:
    """Fingerprint planning facts while excluding paths and self-references added at write time."""
    value = copy.deepcopy(plan)
    value.pop("fingerprint", None)
    for shot in value.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot.pop("scene_path", None)
        shot.pop("scene_sha256", None)
        scene = shot.get("scene")
        if isinstance(scene, dict):
            scene.pop("_planner", None)
    return content_fingerprint(value, length=64)


def _expected_plan_scenes(plan: dict) -> dict[str, str]:
    """Derive every immutable planned scene from the plan, never its manifest."""
    expected: dict[str, str] = {}
    for shot in plan.get("shots", []):
        if not isinstance(shot, dict) or shot.get("status") not in {READY, NEEDS_HUMAN_REVIEW}:
            continue
        relative = str(shot.get("scene_path", "")).replace("\\", "/")
        digest = str(shot.get("scene_sha256", ""))
        if not relative or os.path.isabs(relative) or relative.startswith("../") or "/../" in relative:
            raise PlannerError("hybrid plan has an unsafe immutable scene path")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or relative in expected:
            raise PlannerError("hybrid plan has an invalid immutable scene record")
        expected[relative] = digest
    return expected


def _scene_contract_without_planner(value: object, *, label: str) -> dict:
    if not isinstance(value, dict):
        raise PlannerError(f"{label} must contain a scene object")
    contract = copy.deepcopy(value)
    planner = contract.pop("_planner", None)
    if planner is not None and not isinstance(planner, dict):
        raise PlannerError(f"{label} planner contract is invalid")
    return contract


def write_hybrid_plan(plan: dict, output_dir: str) -> dict:
    """Persist a plan without overwriting an existing immutable plan run."""
    root, plan_path, manifest_path = _plan_paths(output_dir)
    if os.path.exists(manifest_path) or (os.path.isdir(root) and os.listdir(root)):
        raise PlannerError("hybrid plan output must be a new empty directory")
    os.makedirs(root, exist_ok=True)
    scene_hashes: dict[str, str] = {}
    for shot in plan.get("shots", []):
        if shot.get("status") in {READY, NEEDS_HUMAN_REVIEW}:
            shot["scene_path"] = os.path.join("scenes", f"{shot['shot_id']}.scene.json").replace("\\", "/")
    plan["fingerprint"] = _stable_plan_fingerprint(plan)
    for shot in plan.get("shots", []):
        if shot.get("status") not in {READY, NEEDS_HUMAN_REVIEW}:
            continue
        scene_path = os.path.join(root, shot["scene_path"])
        shot["scene"]["_planner"] = {
            "schema_version": PLANNER_SCHEMA_VERSION,
            "plan_dir": root,
            "plan_fingerprint": plan["fingerprint"],
            "shot_id": shot["shot_id"],
        }
        atomic_write_json(scene_path, shot["scene"])
        relative = os.path.relpath(scene_path, root).replace("\\", "/")
        scene_hashes[relative] = _file_sha256(scene_path)
        shot["scene_sha256"] = scene_hashes[relative]
    atomic_write_json(plan_path, plan)
    asset_hashes = {
        str(item["identity"]): {
            "image_path": str(item["image_path"]),
            "sha256": str(item["image_sha256"]),
        }
        for item in plan.get("global_assets", []) if isinstance(item, dict)
    }
    asset_promotions = {
        str(item["identity"]): content_fingerprint(item.get("promotion_evidence", {}), length=64)
        for item in plan.get("global_assets", [])
        if isinstance(item, dict) and str(item.get("source_kind", "")) == "provider"
    }
    manifest = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_status": plan["status"],
        "plan_fingerprint": plan["fingerprint"],
        "plan_sha256": _file_sha256(plan_path),
        "scene_sha256": scene_hashes,
        "asset_sha256": asset_hashes,
        "asset_promotion_evidence": asset_promotions,
        "approval_required": plan["status"] == NEEDS_HUMAN_REVIEW,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def plan_hybrid_storyboard_file(
    storyboard_path: str,
    asset_registry_path: str,
    output_dir: str,
    *,
    allow_fixtures: bool = False,
    timeout_seconds: float = DEFAULT_SOLVER_TIMEOUT_SECONDS,
) -> dict:
    storyboard_file = os.path.abspath(storyboard_path)
    registry_file = os.path.abspath(asset_registry_path)
    plan = plan_hybrid_storyboard(
        _json_object(storyboard_file, "storyboard"),
        _json_object(registry_file, "asset registry"),
        storyboard_dir=os.path.dirname(storyboard_file), registry_dir=os.path.dirname(registry_file),
        allow_fixtures=allow_fixtures, timeout_seconds=timeout_seconds,
    )
    manifest = write_hybrid_plan(plan, output_dir)
    return {"plan": plan, "manifest": manifest}


def verify_hybrid_plan(output_dir: str) -> dict:
    """Verify that an immutable planner output and every rendered input still match."""
    root, plan_path, manifest_path = _plan_paths(output_dir)
    plan = read_json(plan_path)
    manifest = read_json(manifest_path)
    if not isinstance(plan, dict) or not isinstance(manifest, dict):
        raise PlannerError("hybrid plan and manifest are required")
    if manifest.get("plan_sha256") != _file_sha256(plan_path):
        raise PlannerError("hybrid plan integrity check failed")
    if manifest.get("plan_fingerprint") != plan.get("fingerprint"):
        raise PlannerError("hybrid plan fingerprint check failed")
    if plan.get("fingerprint") != _stable_plan_fingerprint(plan):
        raise PlannerError("hybrid plan stable fingerprint check failed")
    expected_scenes = _expected_plan_scenes(plan)
    manifest_scenes = manifest.get("scene_sha256", {})
    if not isinstance(manifest_scenes, dict) or manifest_scenes != expected_scenes:
        raise PlannerError("hybrid planned scene integrity set does not match the immutable plan")
    shots_by_scene = {
        str(shot.get("scene_path", "")).replace("\\", "/"): shot
        for shot in plan.get("shots", [])
        if isinstance(shot, dict) and shot.get("status") in {READY, NEEDS_HUMAN_REVIEW}
    }
    for relative, expected in expected_scenes.items():
        path = os.path.realpath(os.path.join(root, relative))
        if not _inside_any_root(path, [root]) or not os.path.isfile(path) or _file_sha256(path) != expected:
            raise PlannerError("hybrid planned scene integrity check failed")
        disk_scene = _json_object(path, "hybrid planned scene")
        plan_scene = shots_by_scene[relative].get("scene")
        if content_fingerprint(_scene_contract_without_planner(disk_scene, label="hybrid planned scene"), length=64) != content_fingerprint(_scene_contract_without_planner(plan_scene, label="hybrid plan scene"), length=64):
            raise PlannerError("hybrid planned scene content does not match the immutable plan")
        planner_contract = disk_scene.get("_planner")
        if not isinstance(planner_contract, dict) or (
            planner_contract.get("schema_version") != PLANNER_SCHEMA_VERSION
            or os.path.realpath(str(planner_contract.get("plan_dir", ""))) != os.path.realpath(root)
            or planner_contract.get("plan_fingerprint") != plan.get("fingerprint")
            or planner_contract.get("shot_id") != shots_by_scene[relative].get("shot_id")
        ):
            raise PlannerError("hybrid planned scene planner contract does not match the immutable plan")
    declared_roots = [os.path.realpath(str(value)) for value in plan.get("input", {}).get("asset_roots", [])]
    expected_assets = {
        str(item.get("identity", "")): {
            "image_path": str(item.get("image_path", "")), "sha256": str(item.get("image_sha256", "")),
        }
        for item in plan.get("global_assets", []) if isinstance(item, dict)
    }
    expected_promotions = {
        str(item.get("identity", "")): content_fingerprint(item.get("promotion_evidence", {}), length=64)
        for item in plan.get("global_assets", [])
        if isinstance(item, dict) and str(item.get("source_kind", "")) == "provider"
    }
    manifest_assets = manifest.get("asset_sha256", {})
    if not isinstance(manifest_assets, dict) or set(manifest_assets) != set(expected_assets):
        raise PlannerError("hybrid asset integrity set does not match the immutable plan")
    if manifest.get("asset_promotion_evidence") != expected_promotions:
        raise PlannerError("hybrid asset promotion evidence does not match the immutable plan")
    for identity, expected in expected_assets.items():
        item = manifest_assets.get(identity)
        if not isinstance(item, dict) or item.get("image_path") != expected["image_path"] or item.get("sha256") != expected["sha256"]:
            raise PlannerError(f"hybrid asset integrity metadata does not match immutable plan: {identity}")
        path = os.path.realpath(expected["image_path"])
        if not _inside_any_root(path, declared_roots) or not os.path.isfile(path):
            raise PlannerError(f"hybrid planned asset is outside its declared root: {identity}")
        if _file_sha256(path) != expected["sha256"]:
            raise PlannerError(f"hybrid planned asset integrity check failed: {identity}")
    for item in plan.get("global_assets", []):
        if not isinstance(item, dict) or str(item.get("source_kind", "")) != "provider":
            continue
        try:
            verify_asset_promotion_evidence(
                item.get("promotion_evidence"),
                asset_id=str(item.get("asset_id", "")), revision=str(item.get("revision", "")),
                asset_type=str(item.get("asset_type", "")), image_path=str(item.get("image_path", "")),
                image_sha256=str(item.get("image_sha256", "")), width=int(item.get("width", 0)), height=int(item.get("height", 0)),
                derivation_roots=declared_roots,
            )
        except (AssetIntakeError, TypeError, ValueError) as exc:
            raise PlannerError(f"hybrid provider asset promotion verification failed: {exc}") from exc
    return {"status": "verified", "plan_status": plan.get("status"), "plan_fingerprint": plan.get("fingerprint")}


def _approval_payload_without_signature(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "integrity_signature"}


def verify_hybrid_plan_human_verification(output_dir: str) -> dict:
    """Verify that a high-risk plan has a current launcher-key HMAC approval."""
    root, _, manifest_path = _plan_paths(output_dir)
    verify_hybrid_plan(root)
    manifest = _json_object(manifest_path, "hybrid plan manifest")
    approval_path = os.path.join(root, "hybrid_plan_approval.json")
    approval = _json_object(approval_path, "hybrid plan approval")
    if approval.get("status") != "human_verified":
        raise PlannerError("human plan verification status is invalid")
    if approval.get("reviewed_plan_sha256") != manifest.get("plan_sha256") or approval.get("reviewed_plan_fingerprint") != manifest.get("plan_fingerprint"):
        raise PlannerError("human plan verification does not match the current immutable plan")
    key = os.environ.get(HYBRID_SIGNING_KEY_ENV, "")
    if not key:
        raise PlannerError("MANJU_HYBRID_SIGNING_KEY is required to validate human plan verification")
    unsigned = json.dumps(_approval_payload_without_signature(approval), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(key.encode("utf-8"), unsigned, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(approval.get("integrity_signature", "")), expected):
        raise PlannerError("human plan verification integrity check failed")
    return {"status": "human_verified", "reviewed_plan_fingerprint": manifest["plan_fingerprint"]}


def verify_planned_scene_input(scene_path: str, payload: dict) -> dict | None:
    """Gate renderer entry when a scene was emitted from a planner run."""
    contract = payload.get("_planner")
    if not isinstance(contract, dict):
        return None
    plan_dir = os.path.realpath(str(contract.get("plan_dir", "")))
    if not plan_dir or not os.path.isdir(plan_dir):
        raise PlannerError("planned scene is missing its immutable plan directory")
    verification = verify_hybrid_plan(plan_dir)
    if contract.get("plan_fingerprint") != verification["plan_fingerprint"]:
        raise PlannerError("planned scene does not match its immutable plan fingerprint")
    root, plan_path, manifest_path = _plan_paths(plan_dir)
    plan = _json_object(plan_path, "hybrid plan")
    manifest = _json_object(manifest_path, "hybrid plan manifest")
    relative = os.path.relpath(os.path.realpath(scene_path), root).replace("\\", "/")
    if relative not in _expected_plan_scenes(plan):
        raise PlannerError("planned scene is not an approved member of its plan")
    if plan.get("status") == NEEDS_HUMAN_REVIEW:
        verify_hybrid_plan_human_verification(plan_dir)
    elif plan.get("status") != READY:
        raise PlannerError(f"planned scene cannot render while plan status is {plan.get('status')}")
    return verification


def replan_hybrid_plan_from_render(
    plan_dir: str,
    render_output_dir: str,
    output_dir: str,
) -> dict:
    """Create one immutable, evidence-linked soft-layout retry after pixel failure.

    The old plan is never changed.  Only a layer reported as unverifiable is
    protected from accidental overlap on the next solve; assets and existing
    hard facts are copied byte-for-byte from the frozen parent plan.
    """
    root, plan_path, _ = _plan_paths(plan_dir)
    verify_hybrid_plan(root)
    parent = _json_object(plan_path, "hybrid plan")
    render_root = os.path.abspath(render_output_dir)
    render_manifest = _json_object(os.path.join(render_root, "hybrid_render.json"), "hybrid render manifest")
    if render_manifest.get("status") != "blocked_unverifiable":
        raise PlannerError("render evidence must be a blocked_unverifiable renderer result")
    evidence = render_manifest.get("planner_evidence")
    if not isinstance(evidence, dict):
        raise PlannerError("render evidence was not produced from an immutable planned scene")
    if evidence.get("plan_fingerprint") != parent.get("fingerprint"):
        raise PlannerError("render evidence does not belong to the parent plan")
    evidence_shot_id = _required_text(evidence.get("shot_id"), "render evidence shot_id")
    parent_shot = next((item for item in parent.get("shots", []) if item.get("shot_id") == evidence_shot_id), None)
    if not isinstance(parent_shot, dict) or evidence.get("scene_sha256") != parent_shot.get("scene_sha256"):
        raise PlannerError("render evidence does not match an immutable parent scene")
    ambiguous_containment = [
        item.get("constraint_id", "containment")
        for item in parent_shot.get("hard_constraints", [])
        if isinstance(item, dict)
        and item.get("kind") == "containment"
        and item.get("visibility_mode") not in _CONTAINMENT_VISIBILITY_MODES
    ]
    if ambiguous_containment:
        raise PlannerError(
            "scene_contract_revision_required: containment visibility semantics must be confirmed before any replan"
        )
    expected_hashes = {str(item.get("identity")): str(item.get("image_sha256")) for item in parent.get("global_assets", []) if isinstance(item, dict)}
    if evidence.get("asset_sha256") != expected_hashes:
        raise PlannerError("render evidence asset hashes do not match the parent plan")
    run_id = str(render_manifest.get("run_id", ""))
    if not _SAFE_SHOT_ID.fullmatch(run_id):
        raise PlannerError("render evidence run_id is invalid")
    artifact_manifest_path = os.path.join(render_root, "stages", "hybrid_renderer", "runs", run_id, "artifacts", "hybrid_manifest.json")
    artifact_manifest = _json_object(artifact_manifest_path, "renderer artifact manifest")
    if artifact_manifest != render_manifest:
        raise PlannerError("render evidence does not match the renderer artifact manifest")
    state = VisualEventStore(render_root, run_id, pipeline_name=HYBRID_PIPELINE_NAME).recover_state()
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        raise PlannerError("render evidence has no immutable renderer event state")
    integrity = state.get("hybrid_integrity")
    if not isinstance(integrity, dict):
        raise PlannerError("render evidence has no renderer integrity record")
    expected_manifest_relative = os.path.relpath(artifact_manifest_path, render_root)
    if (
        state.get("hybrid_manifest") != expected_manifest_relative
        or integrity.get("manifest_sha256") != _file_sha256(artifact_manifest_path)
        or integrity.get("render_status") != "blocked_unverifiable"
        or state.get("stop_reason") != "blocked_unverifiable"
    ):
        raise PlannerError("render evidence does not match immutable renderer event integrity")
    signature = str(state.get("hybrid_integrity_signature", ""))
    if signature:
        key = os.environ.get(HYBRID_SIGNING_KEY_ENV, "")
        if not key:
            raise PlannerError("signed render evidence requires MANJU_HYBRID_SIGNING_KEY")
        signed = json.dumps(integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected_signature = hmac.new(key.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise PlannerError("render evidence event integrity signature check failed")
    state_scene = state.get("hybrid_scene_contract")
    source_scene = copy.deepcopy(parent_shot.get("scene", {}))
    source_scene.pop("_planner", None)
    expected_scene = scene_from_dict(source_scene, base_dir=root)
    expected_contract = {
        "schema_version": HYBRID_SCHEMA_VERSION,
        "canvas": {"width": expected_scene.canvas_width, "height": expected_scene.canvas_height},
        "background_path": expected_scene.background_path,
        "layers": [layer.to_dict() for layer in expected_scene.layers],
        "constraints": list(expected_scene.constraints),
    }
    expected_scene_fingerprint = content_fingerprint(expected_contract, length=64)
    if (
        not isinstance(state_scene, dict)
        or content_fingerprint(state_scene, length=64) != expected_scene_fingerprint
        or artifact_manifest.get("invocation_contract", {}).get("scene_fingerprint") != expected_scene_fingerprint
    ):
        raise PlannerError("render evidence scene contract does not match the immutable parent scene")
    artifact = artifact_manifest.get("artifact", {})
    rendered_path = os.path.realpath(os.path.join(render_root, str(artifact.get("rendered_path", ""))))
    if not _inside_any_root(rendered_path, [render_root]) or not os.path.isfile(rendered_path) or _file_sha256(rendered_path) != artifact.get("rendered_sha256"):
        raise PlannerError("render evidence artifact integrity check failed")
    targets = [str(value) for value in render_manifest.get("unverifiable_layer_asset_ids", []) if str(value).strip()]
    if not targets:
        raise PlannerError("render evidence has no unverifiable planned layer to replan")
    declared_layers = {
        str(layer.get("asset_id", ""))
        for layer in parent_shot.get("scene", {}).get("layers", [])
        if isinstance(layer, dict)
    }
    if any(target not in declared_layers for target in targets):
        raise PlannerError("render evidence names a layer outside its immutable parent scene")
    history = parent.get("render_replan_history", [])
    if not isinstance(history, list):
        raise PlannerError("render replan history is invalid")
    if len(history) >= int(parent.get("render_replan_limit", 3)):
        raise PlannerError("render replan limit exhausted; preserve evidence and request human correction")
    child = copy.deepcopy(parent)
    child.pop("fingerprint", None)
    target_shot = None
    for shot in child.get("shots", []):
        if shot.get("shot_id") == evidence_shot_id:
            target_shot = shot
            break
    if not isinstance(target_shot, dict) or target_shot.get("status") not in {READY, NEEDS_HUMAN_REVIEW}:
        raise PlannerError("render evidence shot is not available for soft-layout replan")
    for shot in child.get("shots", []):
        if shot.get("status") not in {READY, NEEDS_HUMAN_REVIEW}:
            continue
        if shot.get("shot_id") != evidence_shot_id:
            continue
        layer_ids = {str(item.get("asset_id", "")) for item in shot.get("scene", {}).get("layers", []) if isinstance(item, dict)}
        relevant = [target for target in targets if target in layer_ids]
        if set(relevant) != set(targets):
            raise PlannerError("render evidence targets a layer outside its declared shot")
        assets = [AssetRecord(
            asset_id=str(item["asset_id"]), revision=str(item["revision"]), asset_type=str(item["asset_type"]),
            lifecycle=str(item["lifecycle"]), image_path=str(item["image_path"]), image_sha256=str(item["image_sha256"]),
            width=int(item["width"]), height=int(item["height"]),
        ) for item in shot.get("assets", [])]
        solver = shot.get("solver", {})
        retry_seed = int(solver.get("seed", DEFAULT_SOLVER_SEED)) + len(history) + 1
        target_layers = [item for item in shot.get("scene", {}).get("layers", []) if isinstance(item, dict) and item.get("asset_id") in targets]
        blocker_ids = {
            str(other.get("asset_id"))
            for target in target_layers
            for other in shot.get("scene", {}).get("layers", [])
            if isinstance(other, dict)
            and int(other.get("z_index", 0)) > int(target.get("z_index", 0))
            and max(0, min(int(target["x"]) + int(target["width"]), int(other["x"]) + int(other["width"])) - max(int(target["x"]), int(other["x"])))
            * max(0, min(int(target["y"]) + int(target["height"]), int(other["y"]) + int(other["height"])) - max(int(target["y"]), int(other["y"]))) > 0
        }
        repair_ids = [identity for identity in [*targets, *sorted(blocker_ids)] if identity in layer_ids]
        retry_preference = {
            "preference": "spread_subjects", "priority": 100, "reason": "render visibility repair",
            "source": "render_visibility_evidence", "affected_asset_ids": repair_ids,
            "requires_human_review": False,
        }
        preferences = [*shot.get("layout_preferences", []), retry_preference]
        started = time.monotonic()
        placements, conflicts, solver_state = _solve_layout(
            assets, shot.get("hard_constraints", []), canvas_width=int(shot["canvas"]["width"]), canvas_height=int(shot["canvas"]["height"]),
            timeout_seconds=float(solver.get("timeout_seconds", DEFAULT_SOLVER_TIMEOUT_SECONDS)),
            seed=retry_seed, preferences=preferences,
        )
        shot["layout_preferences"] = preferences
        shot["solver"] = {**solver, "seed": retry_seed, "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "workers": 1, "ortools_version": _solver_version(), "replan_attempt": len(history) + 1}
        if solver_state or conflicts:
            shot["status"] = solver_state or BLOCKED_CONFLICT
            shot["conflicts"] = conflicts
            continue
        for layer in shot["scene"]["layers"]:
            layer.update(placements[str(layer["asset_id"])])
        verification = _verify_scene(shot["scene"], plan_constraints=shot.get("hard_constraints", []))
        shot["independent_verification"] = verification
        shot["failed_constraint_ids"] = [item["constraint_id"] for item in verification if item["verdict"] != "pass"]
        if shot["failed_constraint_ids"]:
            shot["status"] = BLOCKED_CONFLICT
    child["render_replan_history"] = [*history, {
        "attempt": len(history) + 1,
        "parent_plan_fingerprint": parent.get("fingerprint"),
        "render_evidence_fingerprint": content_fingerprint(render_manifest, length=64),
        "render_evidence": evidence,
        "unverifiable_layer_asset_ids": targets,
        "soft_variables_only": True,
    }]
    child["status"] = _plan_status(child.get("shots", []))
    manifest = write_hybrid_plan(child, output_dir)
    return {"plan": child, "manifest": manifest}


def record_hybrid_plan_human_verification(output_dir: str, *, reviewer: str, note: str) -> dict:
    """Record immutable confirmation for a high-risk layout plan."""
    clean_reviewer = _required_text(reviewer, "reviewer")
    clean_note = _required_text(note, "note")
    verification = verify_hybrid_plan(output_dir)
    root, _, manifest_path = _plan_paths(output_dir)
    manifest = _json_object(manifest_path, "hybrid plan manifest")
    if manifest.get("plan_status") != NEEDS_HUMAN_REVIEW:
        raise PlannerError("only a needs_human_review plan may receive human verification")
    key = os.environ.get(HYBRID_SIGNING_KEY_ENV, "")
    if not key:
        raise PlannerError("MANJU_HYBRID_SIGNING_KEY is required for plan verification")
    approval_path = os.path.join(root, "hybrid_plan_approval.json")
    payload = {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "status": "human_verified",
        "reviewer": clean_reviewer,
        "note": clean_note,
        "reviewed_plan_sha256": manifest["plan_sha256"],
        "reviewed_plan_fingerprint": manifest["plan_fingerprint"],
    }
    signature_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["integrity_signature"] = hmac.new(key.encode("utf-8"), signature_payload, hashlib.sha256).hexdigest()
    existing = read_json(approval_path)
    if existing:
        if existing == payload:
            return existing
        raise PlannerError("human plan verification is immutable once recorded")
    atomic_write_json(approval_path, payload)
    return {**payload, "verification": verification}


def migrate_storyboard_visual_constraints(storyboard: dict) -> tuple[dict, dict]:
    """Create a non-destructive v2 candidate that retains only unambiguous facts."""
    candidate = copy.deepcopy(storyboard)
    report = {"schema_version": PLANNER_SCHEMA_VERSION, "migrations": [], "candidates": []}
    for _, shot in _iter_storyboard_shots(candidate):
        visual = _shot_visual(shot)
        existing = shot.get("visual_constraints")
        if isinstance(existing, list):
            continue
        constraints: list[dict] = []
        visible = _shot_asset_references(shot)
        if visible:
            constraints.append({
                "constraint_id": "presence_" + content_fingerprint(shot["shot_id"], visible, length=16),
                "kind": "presence", "subject_ids": visible, "hard": True,
                "provenance": "storyboard.visible_asset_ids",
            })
        shot["visual_constraints"] = constraints
        report["migrations"].append({"shot_id": shot["shot_id"], "added_constraint_count": len(constraints)})
        if visual.get("description"):
            report["candidates"].append({
                "shot_id": shot["shot_id"], "status": "needs_confirmation",
                "reason": "free text was intentionally not converted into hard geometry",
            })
    return candidate, report
