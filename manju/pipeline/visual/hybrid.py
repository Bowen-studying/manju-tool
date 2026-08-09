"""Deterministic local compositor for the v4.1 hybrid visual path.

The renderer deliberately owns only measurable screen-space facts.  It never
calls an image or vision provider: providers may prepare a background or an
object layer, while this module records and verifies the resulting placement.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

from PIL import Image, ImageChops, ImageFilter

from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json

from .identity import create_run_identity
from .store import VisualEventStore, read_current_run_id


HYBRID_SCHEMA_VERSION = 1
HYBRID_RENDERER_VERSION = "4.1.0-hybrid-rc4-hf3"
HYBRID_PIPELINE_NAME = "hybrid_renderer"
HYBRID_SIGNING_KEY_ENV = "MANJU_HYBRID_SIGNING_KEY"
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class HybridLayer:
    """One immutable source asset and its deterministic canvas transform."""

    asset_id: str
    image_path: str
    x: int
    y: int
    width: int
    height: int
    z_index: int = 0
    rotation_degrees: float = 0.0
    mask_path: str = ""
    opacity: float = 1.0
    editable_halo_px: int = 0
    minimum_visible_coverage: float = 0.01
    human_review_required: bool = False

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "image_path": self.image_path,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "z_index": self.z_index,
            "rotation_degrees": self.rotation_degrees,
            "mask_path": self.mask_path,
            "opacity": self.opacity,
            "editable_halo_px": self.editable_halo_px,
            "minimum_visible_coverage": self.minimum_visible_coverage,
            "human_review_required": self.human_review_required,
        }


@dataclass(frozen=True)
class HybridScene:
    canvas_width: int
    canvas_height: int
    background_path: str
    layers: tuple[HybridLayer, ...]
    constraints: tuple[dict, ...] = ()


def _require_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    result = value
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return result


def _require_float(value: object, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return result


def _resolved_path(value: object, base_dir: str, field: str) -> str:
    path = str(value or "").strip()
    if not path:
        raise ValueError(f"{field} is required")
    candidate = path if os.path.isabs(path) else os.path.join(base_dir, path)
    resolved = os.path.abspath(candidate)
    if not os.path.isfile(resolved):
        raise ValueError(f"{field} does not exist: {path}")
    return resolved


def _safe_run_id(value: str) -> str:
    run_id = str(value or "").strip()
    if (
        not run_id
        or run_id in {".", ".."}
        or os.path.isabs(run_id)
        or os.path.basename(run_id) != run_id
        or not _SAFE_RUN_ID.fullmatch(run_id)
    ):
        raise ValueError("run_id must be a single directory name")
    return run_id


def _integrity_signature(integrity: dict) -> str:
    """Sign human-review evidence with a launcher-injected key, never an artifact key."""
    key = os.environ.get(HYBRID_SIGNING_KEY_ENV, "")
    if not key:
        return ""
    payload = json.dumps(integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def scene_from_dict(value: dict, *, base_dir: str) -> HybridScene:
    """Parse a portable JSON scene contract without accepting implicit assets."""
    if not isinstance(value, dict):
        raise ValueError("hybrid scene must be an object")
    if int(value.get("schema_version", 0) or 0) != HYBRID_SCHEMA_VERSION:
        raise ValueError(f"hybrid scene requires schema_version {HYBRID_SCHEMA_VERSION}")
    canvas = value.get("canvas")
    if not isinstance(canvas, dict):
        raise ValueError("hybrid scene requires canvas")
    width = _require_int(canvas.get("width"), "canvas.width", minimum=1)
    height = _require_int(canvas.get("height"), "canvas.height", minimum=1)
    background_path = _resolved_path(value.get("background_path"), base_dir, "background_path")
    source_layers = value.get("layers")
    if not isinstance(source_layers, list) or not source_layers:
        raise ValueError("hybrid scene requires at least one layer")
    layers: list[HybridLayer] = []
    asset_ids: set[str] = set()
    for index, item in enumerate(source_layers):
        if not isinstance(item, dict):
            raise ValueError(f"layers[{index}] must be an object")
        asset_id = str(item.get("asset_id", "")).strip()
        if not asset_id:
            raise ValueError(f"layers[{index}].asset_id is required")
        if asset_id in asset_ids:
            raise ValueError(f"duplicate layer asset_id: {asset_id}")
        asset_ids.add(asset_id)
        opacity = _require_float(item.get("opacity", 1.0), f"layers[{index}].opacity", minimum=0.0)
        if opacity > 1.0:
            raise ValueError(f"layers[{index}].opacity must be at most 1")
        minimum_visible_coverage = _require_float(
            item.get("minimum_visible_coverage", 0.01),
            f"layers[{index}].minimum_visible_coverage", minimum=0.0,
        )
        if minimum_visible_coverage > 1.0:
            raise ValueError(f"layers[{index}].minimum_visible_coverage must be at most 1")
        mask_value = str(item.get("mask_path", "") or "").strip()
        human_review_required = item.get("human_review_required", False)
        if not isinstance(human_review_required, bool):
            raise ValueError(f"layers[{index}].human_review_required must be a boolean")
        layers.append(HybridLayer(
            asset_id=asset_id,
            image_path=_resolved_path(item.get("image_path"), base_dir, f"layers[{index}].image_path"),
            x=_require_int(item.get("x"), f"layers[{index}].x"),
            y=_require_int(item.get("y"), f"layers[{index}].y"),
            width=_require_int(item.get("width"), f"layers[{index}].width", minimum=1),
            height=_require_int(item.get("height"), f"layers[{index}].height", minimum=1),
            z_index=_require_int(item.get("z_index", 0), f"layers[{index}].z_index"),
            rotation_degrees=_require_float(item.get("rotation_degrees", 0.0), f"layers[{index}].rotation_degrees"),
            mask_path=_resolved_path(mask_value, base_dir, f"layers[{index}].mask_path") if mask_value else "",
            opacity=opacity,
            editable_halo_px=_require_int(item.get("editable_halo_px", 0), f"layers[{index}].editable_halo_px", minimum=0),
            minimum_visible_coverage=minimum_visible_coverage,
            human_review_required=human_review_required,
        ))
    constraints = value.get("constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(item, dict) for item in constraints):
        raise ValueError("constraints must be a list of objects")
    return HybridScene(width, height, background_path, tuple(layers), tuple(constraints))


def load_hybrid_scene(path: str) -> HybridScene:
    scene_path = os.path.abspath(path)
    payload = read_json(scene_path)
    if not isinstance(payload, dict):
        raise ValueError("hybrid scene JSON must contain an object")
    return scene_from_dict(payload, base_dir=os.path.dirname(scene_path))


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _combine_alpha(image: Image.Image, mask_path: str, opacity: float) -> Image.Image:
    result = image.convert("RGBA")
    alpha = result.getchannel("A")
    if mask_path:
        with Image.open(mask_path) as mask_file:
            mask = mask_file.convert("L")
        if mask.size != result.size:
            raise ValueError("layer mask must have the same dimensions as its source image")
        alpha = ImageChops.multiply(alpha, mask)
    if opacity < 1.0:
        alpha = alpha.point(lambda item: round(item * opacity))
    result.putalpha(alpha)
    return result


def _place_rgba(canvas: Image.Image, raster: Image.Image, x: int, y: int) -> None:
    """Alpha composite a raster while safely clipping off-canvas content."""
    left = max(0, x)
    top = max(0, y)
    right = min(canvas.width, x + raster.width)
    bottom = min(canvas.height, y + raster.height)
    if left >= right or top >= bottom:
        return
    source = raster.crop((left - x, top - y, right - x, bottom - y))
    canvas.alpha_composite(source, dest=(left, top))


def _placed_layer(layer: HybridLayer) -> tuple[Image.Image, tuple[int, int], tuple[int, int, int, int]]:
    with Image.open(layer.image_path) as source_file:
        source = source_file.copy()
    raster = _combine_alpha(source, layer.mask_path, layer.opacity)
    raster = raster.resize((layer.width, layer.height), Image.Resampling.LANCZOS)
    if layer.rotation_degrees:
        raster = raster.rotate(-layer.rotation_degrees, expand=True, resample=Image.Resampling.BICUBIC)
    x = layer.x
    y = layer.y
    return raster, (x, y), (x, y, x + raster.width, y + raster.height)


def _editable_mask(scene: HybridScene, placements: dict[str, tuple[Image.Image, tuple[int, int]]]) -> Image.Image:
    editable = Image.new("L", (scene.canvas_width, scene.canvas_height), 0)
    for layer in scene.layers:
        raster, (x, y) = placements[layer.asset_id]
        # A non-zero alpha is an editable pixel.  Keeping it binary means a
        # feathered edge is never simultaneously treated as editable and
        # protected by the subsequent complement operation.
        alpha = raster.getchannel("A").point(lambda value: 255 if value else 0)
        if layer.editable_halo_px:
            size = layer.editable_halo_px * 2 + 1
            alpha = alpha.filter(ImageFilter.MaxFilter(size=size))
        left = max(0, x)
        top = max(0, y)
        right = min(editable.width, x + alpha.width)
        bottom = min(editable.height, y + alpha.height)
        if left < right and top < bottom:
            crop = alpha.crop((left - x, top - y, right - x, bottom - y))
            existing = editable.crop((left, top, right, bottom))
            editable.paste(ImageChops.lighter(existing, crop), (left, top))
    return editable


def _protected_pixel_delta(background: Image.Image, rendered: Image.Image, editable: Image.Image) -> int:
    difference = ImageChops.difference(background.convert("RGBA"), rendered.convert("RGBA"))
    channels = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), ImageChops.lighter(channels[2], channels[3]))
    protected = ImageChops.invert(editable)
    observed = ImageChops.multiply(maximum, protected)
    return sum(1 for value in observed.tobytes() if value)


def _layer_geometry(
    layer: HybridLayer,
    raster: Image.Image,
    position: tuple[int, int],
) -> dict:
    """Describe both declared scale and the actual post-rotation placement."""
    x, y = position
    return {
        "logical_width": layer.width,
        "logical_height": layer.height,
        "rendered_x": x,
        "rendered_y": y,
        "rendered_width": raster.width,
        "rendered_height": raster.height,
        "center_x": x + raster.width / 2,
        "center_y": y + raster.height / 2,
        "rotation_degrees": layer.rotation_degrees,
    }


def _visible_coverage(
    raster: Image.Image,
    position: tuple[int, int],
    canvas_size: tuple[int, int],
) -> float:
    """Return visible alpha coverage relative to the full transformed layer."""
    alpha = raster.getchannel("A")
    x, y = position
    canvas_width, canvas_height = canvas_size
    left = max(0, x)
    top = max(0, y)
    right = min(canvas_width, x + alpha.width)
    bottom = min(canvas_height, y + alpha.height)
    total = alpha.width * alpha.height
    if left >= right or top >= bottom or not total:
        return 0.0
    visible = alpha.crop((left - x, top - y, right - x, bottom - y))
    return sum(1 for value in visible.tobytes() if value) / total


def _alpha_on_canvas(raster: Image.Image, position: tuple[int, int], canvas_size: tuple[int, int]) -> Image.Image:
    canvas = Image.new("L", canvas_size, 0)
    alpha = raster.getchannel("A")
    x, y = position
    left, top = max(0, x), max(0, y)
    right, bottom = min(canvas.width, x + alpha.width), min(canvas.height, y + alpha.height)
    if left < right and top < bottom:
        canvas.paste(alpha.crop((left - x, top - y, right - x, bottom - y)), (left, top))
    return canvas


def _final_visible_coverages(
    scene: HybridScene,
    placements: dict[str, tuple[Image.Image, tuple[int, int]]],
) -> dict[str, float]:
    """Measure each layer after higher z-index alpha has obscured it."""
    covered = Image.new("L", (scene.canvas_width, scene.canvas_height), 0)
    coverages: dict[str, float] = {}
    ordered = sorted(scene.layers, key=lambda item: (item.z_index, item.asset_id))
    for layer in reversed(ordered):
        raster, position = placements[layer.asset_id]
        full_alpha = _alpha_on_canvas(raster, position, covered.size)
        residual = ImageChops.multiply(full_alpha, ImageChops.invert(covered))
        total = raster.width * raster.height
        coverages[layer.asset_id] = (
            sum(1 for value in residual.tobytes() if value) / total if total else 0.0
        )
        covered = ImageChops.lighter(covered, full_alpha)
    return coverages


def _angular_distance(value: float, target: float) -> float:
    return abs((value - target + 180.0) % 360.0 - 180.0)


def verify_hard_constraints(
    scene: HybridScene,
    *,
    placements: dict[str, tuple[Image.Image, tuple[int, int]]] | None = None,
    visible_asset_ids: set[str] | None = None,
) -> list[dict]:
    """Verify only explicit, measurable screen-space contract types."""
    geometry: dict[str, dict] = {}
    for layer in scene.layers:
        raster, position = (placements or {}).get(layer.asset_id, (None, None))
        if raster is None or position is None:
            raster, position, _ = _placed_layer(layer)
        geometry[layer.asset_id] = _layer_geometry(layer, raster, position)
    verdicts: list[dict] = []
    for index, constraint in enumerate(scene.constraints):
        constraint_id = str(constraint.get("constraint_id", "")).strip() or f"constraint_{index + 1}"
        kind = str(constraint.get("kind", "")).strip()
        try:
            if kind == "relative_size":
                subject = geometry[str(constraint["subject_id"])]
                reference = geometry[str(constraint["reference_id"])]
                dimension = str(constraint.get("dimension", "width"))
                dimension_key = {
                    "width": "logical_width",
                    "height": "logical_height",
                    "visible_width": "rendered_width",
                    "visible_height": "rendered_height",
                }.get(dimension)
                if not dimension_key:
                    raise ValueError("dimension must be width, height, visible_width or visible_height")
                denominator = float(reference[dimension_key])
                if denominator <= 0:
                    raise ValueError("reference size must be positive")
                ratio = float(subject[dimension_key]) / denominator
                minimum = _require_float(constraint["min_ratio"], "min_ratio", minimum=0.0)
                maximum = _require_float(constraint["max_ratio"], "max_ratio", minimum=minimum)
                passed = minimum <= ratio <= maximum
                evidence = {
                    "ratio": ratio, "min_ratio": minimum, "max_ratio": maximum,
                    "dimension": dimension, "subject_value": subject[dimension_key],
                    "reference_value": reference[dimension_key],
                }
            elif kind == "direction":
                source = geometry[str(constraint["source_id"])]
                target = geometry[str(constraint["target_id"])]
                dx = float(target["center_x"]) - float(source["center_x"])
                # Screen-space geometry uses a downward-positive Y axis, the
                # same convention used by the planner and emitted scene JSON.
                # Therefore 90 degrees means that target is below source.
                dy = float(target["center_y"]) - float(source["center_y"])
                if dx == 0 and dy == 0:
                    raise ValueError("direction requires distinct source and target centers")
                angle = math.degrees(math.atan2(dy, dx)) % 360.0
                expected = _require_float(constraint["expected_angle_degrees"], "expected_angle_degrees") % 360.0
                tolerance = _require_float(constraint.get("tolerance_degrees", 15.0), "tolerance_degrees", minimum=0.0)
                distance = _angular_distance(angle, expected)
                passed = distance <= tolerance
                evidence = {"angle_degrees": angle, "expected_angle_degrees": expected, "tolerance_degrees": tolerance, "distance_degrees": distance}
            elif kind == "count":
                subject_ids = constraint.get("subject_ids")
                if not isinstance(subject_ids, list) or not subject_ids:
                    raise ValueError("subject_ids must be a non-empty list")
                missing = [
                    str(item) for item in subject_ids
                    if str(item) not in geometry or (visible_asset_ids is not None and str(item) not in visible_asset_ids)
                ]
                expected = _require_int(constraint["expected_count"], "expected_count", minimum=0)
                actual = len(set(map(str, subject_ids)))
                passed = not missing and actual == expected
                evidence = {"actual_count": actual, "expected_count": expected, "missing_asset_ids": missing}
            elif kind == "contains_center":
                subject = geometry[str(constraint["subject_id"])]
                container = geometry[str(constraint["container_id"])]
                mode = str(constraint.get("visibility_mode", "legacy_opaque_surface")).strip()
                if mode not in {"legacy_opaque_surface", "subject_in_front", "transparent_window", "layered_container"}:
                    raise ValueError("contains_center visibility_mode is invalid")
                center_x, center_y = float(subject["center_x"]), float(subject["center_y"])
                within_box = (
                    float(container["rendered_x"]) <= center_x <= float(container["rendered_x"]) + float(container["rendered_width"])
                    and float(container["rendered_y"]) <= center_y <= float(container["rendered_y"]) + float(container["rendered_height"])
                )
                container_raster, container_position = (placements or {}).get(
                    str(constraint["container_id"]), (None, None),
                )
                if container_raster is None or container_position is None:
                    container_raster, container_position, _ = _placed_layer(
                        next(layer for layer in scene.layers if layer.asset_id == str(constraint["container_id"]))
                    )
                sample_x = int(math.floor(center_x - container_position[0]))
                sample_y = int(math.floor(center_y - container_position[1]))
                alpha = 0
                if 0 <= sample_x < container_raster.width and 0 <= sample_y < container_raster.height:
                    alpha = int(container_raster.getchannel("A").getpixel((sample_x, sample_y)))
                subject_visible = visible_asset_ids is None or str(constraint["subject_id"]) in visible_asset_ids
                passed = within_box and subject_visible
                if mode == "legacy_opaque_surface":
                    passed = passed and alpha > 0
                elif mode == "transparent_window":
                    passed = passed and alpha < 255
                elif mode == "layered_container":
                    front_id = str(constraint.get("front_layer_id", ""))
                    front = geometry.get(front_id)
                    front_layer = next((layer for layer in scene.layers if layer.asset_id == front_id), None)
                    container_layer = next((layer for layer in scene.layers if layer.asset_id == str(constraint["container_id"])), None)
                    if front is None or front_layer is None or container_layer is None:
                        raise ValueError("layered_container requires a front_layer_id")
                    aligned = all(front[key] == container[key] for key in ("rendered_x", "rendered_y", "rendered_width", "rendered_height"))
                    ordered = container_layer.z_index < next(layer.z_index for layer in scene.layers if layer.asset_id == str(constraint["subject_id"])) < front_layer.z_index
                    passed = passed and aligned and ordered
                evidence = {
                    "subject_center": [center_x, center_y], "container_geometry": container,
                    "container_sample": [sample_x, sample_y], "container_alpha": alpha,
                    "visibility_mode": mode, "subject_visible": subject_visible,
                }
                if mode == "layered_container":
                    evidence.update({"front_layer_aligned": aligned, "layer_ordered": ordered})
            else:
                raise ValueError("unsupported or missing hard constraint kind")
            verdicts.append({
                "constraint_id": constraint_id,
                "kind": kind,
                "verdict": "pass" if passed else "fail",
                "evidence": evidence,
            })
        except (KeyError, TypeError, ValueError) as exc:
            verdicts.append({
                "constraint_id": constraint_id,
                "kind": kind,
                "verdict": "unverifiable",
                "evidence": {"reason": str(exc)},
            })
    return verdicts


def render_hybrid_scene(
    scene: HybridScene,
    output_dir: str,
    *,
    run_id: str | None = None,
    planner_evidence: dict | None = None,
) -> dict:
    """Render a local scene and persist auditable artifacts in an isolated run."""
    target_dir = os.path.abspath(output_dir)
    run_id = _safe_run_id(run_id or uuid.uuid4().hex)
    scene_contract = {
        "schema_version": HYBRID_SCHEMA_VERSION,
        "canvas": {"width": scene.canvas_width, "height": scene.canvas_height},
        "background_path": scene.background_path,
        "layers": [layer.to_dict() for layer in scene.layers],
        "constraints": list(scene.constraints),
    }
    invocation_contract = {
        "renderer_version": HYBRID_RENDERER_VERSION,
        "scene_fingerprint": content_fingerprint(scene_contract, length=64),
        "render_mode": "hybrid",
    }
    identity = create_run_identity(invocation_contract, run_id=run_id, run_kind="hybrid_render")
    store = VisualEventStore(target_dir, run_id, pipeline_name=HYBRID_PIPELINE_NAME)
    # Check immutable identity before writing any artifact for a supplied run ID.
    store.write_identity(identity)
    with Image.open(scene.background_path) as background_file:
        background = background_file.convert("RGBA")
    if background.size != (scene.canvas_width, scene.canvas_height):
        raise ValueError("background dimensions must exactly match the declared canvas")
    rendered = background.copy()
    placements: dict[str, tuple[Image.Image, tuple[int, int]]] = {}
    layer_evidence: list[dict] = []
    invisible_layers: list[str] = []
    for layer in sorted(scene.layers, key=lambda item: (item.z_index, item.asset_id)):
        raster, position, rendered_box = _placed_layer(layer)
        placements[layer.asset_id] = (raster, position)
        _place_rgba(rendered, raster, *position)
        layer_evidence.append({
            "asset_id": layer.asset_id,
            "z_index": layer.z_index,
            "geometry": _layer_geometry(layer, raster, position),
            "rendered_box": list(rendered_box),
            "source_sha256": _file_hash(layer.image_path),
            "mask_sha256": _file_hash(layer.mask_path) if layer.mask_path else "",
            "visible_coverage": 0.0,
            "minimum_visible_coverage": layer.minimum_visible_coverage,
            "human_review_required": layer.human_review_required,
        })
    final_coverages = _final_visible_coverages(scene, placements)
    for evidence in layer_evidence:
        coverage = final_coverages[evidence["asset_id"]]
        evidence["visible_coverage"] = coverage
        layer = next(item for item in scene.layers if item.asset_id == evidence["asset_id"])
        if coverage == 0.0 or coverage < layer.minimum_visible_coverage:
            invisible_layers.append(layer.asset_id)
    editable = _editable_mask(scene, placements)
    protected_delta_count = _protected_pixel_delta(background, rendered, editable)
    verdicts = verify_hard_constraints(
        scene, placements=placements,
        visible_asset_ids={layer.asset_id for layer in scene.layers if layer.asset_id not in invisible_layers},
    )
    failed = [item["constraint_id"] for item in verdicts if item["verdict"] == "fail"]
    unverifiable = [item["constraint_id"] for item in verdicts if item["verdict"] == "unverifiable"]
    requires_human = any(layer.human_review_required for layer in scene.layers)
    if protected_delta_count:
        status = "blocked_protection_violation"
    elif invisible_layers:
        status = "blocked_unverifiable"
    elif failed:
        status = "blocked_hard_constraint"
    elif unverifiable:
        status = "blocked_unverifiable"
    elif requires_human:
        status = "needs_human_review"
    else:
        status = "auto_verified"

    if status == "needs_human_review" and not os.environ.get(HYBRID_SIGNING_KEY_ENV, ""):
        status = "blocked_integrity_key_required"
    artifact_dir = os.path.join(target_dir, "stages", HYBRID_PIPELINE_NAME, "runs", run_id, "artifacts")
    os.makedirs(artifact_dir, exist_ok=True)
    image_path = os.path.join(artifact_dir, "rendered.png")
    rendered.save(image_path, "PNG")
    manifest = {
        "schema_version": HYBRID_SCHEMA_VERSION,
        "renderer_version": HYBRID_RENDERER_VERSION,
        "render_mode": "hybrid",
        "run_id": run_id,
        "status": status,
        "canvas": {"width": scene.canvas_width, "height": scene.canvas_height},
        "background_sha256": _file_hash(scene.background_path),
        "layers": layer_evidence,
        "hard_constraint_verdicts": verdicts,
        "unverifiable_layer_asset_ids": invisible_layers,
        "protection": {
            "protected_pixel_delta_count": protected_delta_count,
            "passed": protected_delta_count == 0,
            "editable_pixels": sum(1 for value in editable.tobytes() if value),
        },
        "human_review_required": requires_human,
        "artifact": {
            "rendered_path": os.path.relpath(image_path, target_dir),
            "rendered_sha256": _file_hash(image_path),
        },
    }
    if planner_evidence is not None:
        manifest["planner_evidence"] = planner_evidence
    manifest["invocation_contract"] = invocation_contract
    manifest_path = os.path.join(artifact_dir, "hybrid_manifest.json")
    atomic_write_json(manifest_path, manifest)
    integrity = {
        "manifest_sha256": _file_hash(manifest_path),
        "rendered_sha256": manifest["artifact"]["rendered_sha256"],
        "rendered_path": manifest["artifact"]["rendered_path"],
        "render_status": status,
        "hard_constraint_verdicts_fingerprint": content_fingerprint(verdicts, length=64),
    }
    integrity_signature = _integrity_signature(integrity)
    store.commit_state({
        "run_id": run_id,
        "status": "completed" if status == "auto_verified" else "needs_review",
        "stage": "completed" if status == "auto_verified" else "blocked_upstream",
        "stop_reason": "" if status == "auto_verified" else status,
        "render_mode": "hybrid",
        "hybrid_manifest": os.path.relpath(manifest_path, target_dir),
        "hybrid_integrity": integrity,
        "hybrid_integrity_signature": integrity_signature,
        "hybrid_scene_contract": scene_contract,
        "run_identity": identity.to_dict(),
    }, reason="hybrid_rendered")
    atomic_write_json(os.path.join(target_dir, "hybrid_render.json"), manifest)
    return manifest


def render_hybrid_scene_file(scene_path: str, output_dir: str) -> dict:
    resolved_scene_path = os.path.abspath(scene_path)
    payload = read_json(resolved_scene_path)
    if not isinstance(payload, dict):
        raise ValueError("hybrid scene JSON must contain an object")
    # Planner-produced scenes carry a contract back to their immutable plan.
    # Standalone rc1 scenes remain supported, while planned scenes cannot skip
    # asset integrity checks or a required human composition approval.
    planner_evidence = None
    if isinstance(payload.get("_planner"), dict):
        from .planner import verify_planned_scene_input

        verification = verify_planned_scene_input(resolved_scene_path, payload)
        plan_dir = os.path.realpath(str(payload["_planner"]["plan_dir"]))
        plan = read_json(os.path.join(plan_dir, "hybrid_plan.json"))
        planner_evidence = {
            "plan_fingerprint": verification["plan_fingerprint"],
            "shot_id": payload["_planner"]["shot_id"],
            "scene_sha256": _file_hash(resolved_scene_path),
            "asset_sha256": {
                str(item.get("identity")): str(item.get("image_sha256"))
                for item in plan.get("global_assets", []) if isinstance(item, dict)
            },
        }
    return render_hybrid_scene(
        scene_from_dict(payload, base_dir=os.path.dirname(resolved_scene_path)), output_dir,
        planner_evidence=planner_evidence,
    )


def record_hybrid_human_verification(
    output_dir: str,
    *,
    reviewer: str,
    note: str,
    run_id: str | None = None,
) -> dict:
    """Record an idempotent human approval without weakening blocked outcomes."""
    target_dir = os.path.abspath(output_dir)
    resolved_run_id = run_id or read_current_run_id(
        target_dir, pipeline_name=HYBRID_PIPELINE_NAME
    )
    if not resolved_run_id:
        raise ValueError("hybrid run_id is required when no current hybrid run exists")
    resolved_run_id = _safe_run_id(resolved_run_id)
    clean_reviewer = reviewer.strip()
    clean_note = note.strip()
    if not clean_reviewer:
        raise ValueError("reviewer is required")
    if not clean_note:
        raise ValueError("review note is required")
    run_root = os.path.join(target_dir, "stages", HYBRID_PIPELINE_NAME, "runs", resolved_run_id)
    manifest_path = os.path.join(run_root, "artifacts", "hybrid_manifest.json")
    store = VisualEventStore(target_dir, resolved_run_id, pipeline_name=HYBRID_PIPELINE_NAME)
    state = store.recover_state() or {}
    verification_path = os.path.join(run_root, "hybrid_human_verification.json")
    existing = read_json(verification_path)
    if isinstance(existing, dict):
        if state.get("verification") != existing or state.get("status") != "completed":
            raise ValueError("human verification integrity check failed")
        if existing.get("reviewer") == clean_reviewer and existing.get("note") == clean_note:
            return existing
        raise ValueError("human verification is immutable once recorded")
    integrity = state.get("hybrid_integrity", {})
    if (
        state.get("status") != "needs_review"
        or state.get("stop_reason") != "needs_human_review"
        or not isinstance(integrity, dict)
    ):
        raise ValueError("only the event-recorded needs_human_review state may receive human verification")
    signature = str(state.get("hybrid_integrity_signature", ""))
    expected_signature = _integrity_signature(integrity)
    if not signature or not expected_signature or not hmac.compare_digest(signature, expected_signature):
        raise ValueError("hybrid integrity signature check failed")
    if str(state.get("hybrid_manifest", "")) != os.path.relpath(manifest_path, target_dir):
        raise ValueError("hybrid manifest path does not match the event-recorded run")
    if _file_hash(manifest_path) != str(integrity.get("manifest_sha256", "")):
        raise ValueError("hybrid manifest integrity check failed")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("hybrid manifest is missing")
    rendered_path = os.path.abspath(os.path.join(target_dir, str(manifest.get("artifact", {}).get("rendered_path", ""))))
    if (
        not os.path.isfile(rendered_path)
        or str(manifest.get("artifact", {}).get("rendered_sha256", "")) != str(integrity.get("rendered_sha256", ""))
        or _file_hash(rendered_path) != str(integrity.get("rendered_sha256", ""))
        or content_fingerprint(manifest.get("hard_constraint_verdicts", []), length=64)
        != str(integrity.get("hard_constraint_verdicts_fingerprint", ""))
    ):
        raise ValueError("hybrid artifact integrity check failed")
    scene_contract = state.get("hybrid_scene_contract")
    if not isinstance(scene_contract, dict):
        raise ValueError("hybrid scene contract is missing from the event record")
    scene = scene_from_dict(scene_contract, base_dir=target_dir)
    recomputed_verdicts = verify_hard_constraints(scene)
    if content_fingerprint(recomputed_verdicts, length=64) != str(integrity.get("hard_constraint_verdicts_fingerprint", "")):
        raise ValueError("hybrid scene verification no longer matches signed evidence")
    if manifest.get("status") != "needs_human_review":
        raise ValueError("only a needs_human_review hybrid run may receive human verification")
    if not manifest.get("protection", {}).get("passed"):
        raise ValueError("cannot verify a protection violation")
    verdicts = manifest.get("hard_constraint_verdicts", [])
    if not isinstance(verdicts, list) or any(item.get("verdict") != "pass" for item in verdicts if isinstance(item, dict)):
        raise ValueError("cannot verify a run with failed or unverifiable hard constraints")
    verification = {
        "schema_version": HYBRID_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "status": "human_verified",
        "reviewer": clean_reviewer,
        "note": clean_note,
        "reviewed_manifest_sha256": _file_hash(manifest_path),
        "reviewed_rendered_sha256": _file_hash(rendered_path),
    }
    atomic_write_json(verification_path, verification)
    store.commit_state({
        **state,
        "run_id": resolved_run_id,
        "status": "completed",
        "stage": "completed",
        "stop_reason": "",
        "render_mode": "hybrid",
        "verification": verification,
    }, reason="hybrid_human_verified")
    return verification
