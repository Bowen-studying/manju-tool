"""Offline rc2 acceptance fixtures: generic stories, not prompt keywords."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest
from PIL import Image

from manju.pipeline.visual.hybrid import render_hybrid_scene_file
from manju.pipeline.visual.planner import PLANNER_SCHEMA_VERSION, plan_hybrid_storyboard, write_hybrid_plan


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None,
    reason="requires manju-tool[planner]",
)


def _image(path: Path, size: tuple[int, int], color: tuple[int, int, int, int]) -> None:
    Image.new("RGBA", size, color).save(path)


def _assets(tmp_path: Path, prefix: str) -> dict:
    root = tmp_path / f"{prefix}-assets"
    root.mkdir()
    _image(root / "background.png", (160, 90), (20, 30, 40, 255))
    _image(root / "primary.png", (20, 20), (220, 40, 40, 255))
    _image(root / "secondary.png", (20, 20), (40, 200, 40, 255))
    _image(root / "tertiary.png", (16, 16), (40, 60, 220, 255))
    return {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "asset_roots": [str(root)],
        "assets": [
            {"asset_id": f"{prefix}-background", "revision": "1", "asset_type": "background", "lifecycle": "formal", "image_path": str(root / "background.png")},
            {"asset_id": f"{prefix}-primary", "revision": "1", "asset_type": "character_identity", "lifecycle": "formal", "image_path": str(root / "primary.png")},
            {"asset_id": f"{prefix}-secondary", "revision": "1", "asset_type": "key_prop", "lifecycle": "formal", "image_path": str(root / "secondary.png")},
            {"asset_id": f"{prefix}-tertiary", "revision": "1", "asset_type": "portable_prop", "lifecycle": "formal", "image_path": str(root / "tertiary.png")},
        ],
    }


def _story(prefix: str, variant: int) -> dict:
    primary = f"{prefix}-primary"
    secondary = f"{prefix}-secondary"
    tertiary = f"{prefix}-tertiary"
    shots = []
    for number in range(1, 31):
        constraints = [
            {"constraint_id": f"presence-{number}", "kind": "presence", "subject_ids": [primary, secondary], "hard": True, "provenance": "fixture"},
            {"constraint_id": f"scale-{number}", "kind": "relative_size", "subject_id": primary, "reference_id": secondary, "dimension": "width", "min_ratio": 0.9, "max_ratio": 1.1, "hard": True, "provenance": "fixture"},
            {"constraint_id": f"direction-{number}", "kind": "direction", "source_id": primary, "target_id": secondary, "expected_angle_degrees": 0, "tolerance_degrees": 45, "hard": True, "provenance": "fixture"},
        ]
        props = [secondary]
        if variant == 1:
            props.append(tertiary)
            constraints.extend([
                {"constraint_id": f"count-{number}", "kind": "count", "subject_ids": [primary, secondary, tertiary], "expected_count": 3, "hard": True, "provenance": "fixture"},
                {"constraint_id": f"separate-{number}", "kind": "forbidden_occlusion", "subject_ids": [primary, tertiary], "hard": True, "provenance": "fixture"},
            ])
        elif variant == 2:
            props.append(tertiary)
            constraints.extend([
                {"constraint_id": f"vertical-{number}", "kind": "direction", "source_id": tertiary, "target_id": secondary, "expected_angle_degrees": 90, "tolerance_degrees": 45, "hard": True, "provenance": "fixture"},
                {"constraint_id": f"layer-{number}", "kind": "z_order", "behind_id": primary, "front_id": tertiary, "hard": True, "provenance": "fixture"},
            ])
        shots.append({
            "shot_id": f"1.{number}",
            "visual": {"visible_character_ids": [primary]},
            "visible_prop_ids": props,
            "visual_constraints": constraints,
        })
    return {
        "schema_version": "2.0", "canvas": {"width": 160, "height": 90},
        "scenes": [{"scene_id": "1", "heading": "fixture", "background_asset_id": f"{prefix}-background", "shots": shots}],
    }


def test_three_generic_stories_each_meet_the_rc2_offline_constraint_volume(tmp_path: Path) -> None:
    started = time.monotonic()
    all_plans = []
    for index in range(3):
        prefix = f"story-{index + 1}"
        plan = plan_hybrid_storyboard(
            _story(prefix, index), _assets(tmp_path, prefix),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )
        assert plan["status"] == "ready"
        assert len(plan["shots"]) == 30
        assert sum(len(shot["hard_constraints"]) for shot in plan["shots"]) >= 50
        assert all(all(item["verdict"] == "pass" for item in shot["independent_verification"]) for shot in plan["shots"])
        all_plans.append(plan)
    assert time.monotonic() - started < 60
    assert len({plan["fingerprint"] for plan in all_plans}) == 3


def test_planned_scene_is_accepted_by_the_existing_pixel_renderer(tmp_path: Path) -> None:
    plan = plan_hybrid_storyboard(
        _story("integration", 0), _assets(tmp_path, "integration"),
        storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
    )
    plan["shots"] = [plan["shots"][0]]
    output = tmp_path / "plan-output"
    write_hybrid_plan(plan, str(output))
    scene_path = output / plan["shots"][0]["scene_path"]
    manifest = render_hybrid_scene_file(str(scene_path), str(tmp_path / "render-output"))
    assert manifest["status"] == "auto_verified"
