from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from manju.cli import cli
from manju.pipeline.storyboard_schema import normalize_storyboard
from manju.pipeline.visual.planner import (
    AssetRecord,
    BLOCKED_UNSUPPORTED,
    NEEDS_ASSETS,
    NEEDS_CONSTRAINT_CONFIRMATION,
    SCENE_CONTRACT_REVISION_REQUIRED,
    PLANNER_SCHEMA_VERSION,
    PlannerDependencyError,
    migrate_storyboard_visual_constraints,
    plan_hybrid_storyboard,
    plan_hybrid_storyboard_file,
    record_hybrid_plan_human_verification,
    replan_hybrid_plan_from_render,
    verify_hybrid_plan,
)
from manju.pipeline.visual.planner import _solve_layout, _verify_scene
from manju.pipeline.visual.hybrid import render_hybrid_scene_file


def _image(path: Path, size: tuple[int, int], color: tuple[int, int, int, int]) -> None:
    Image.new("RGBA", size, color).save(path)


def _window_image(path: Path, *, size: tuple[int, int] = (40, 40)) -> None:
    image = Image.new("RGBA", size, (40, 40, 220, 255))
    inset = max(1, min(size) // 5)
    image.paste((0, 0, 0, 0), (inset, inset, size[0] - inset, size[1] - inset))
    image.save(path)


def _registry(tmp_path: Path, *, include_reference: bool = True) -> dict:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    _image(assets / "background.png", (100, 80), (15, 20, 30, 255))
    _image(assets / "subject.png", (20, 20), (230, 20, 20, 255))
    if include_reference:
        _image(assets / "reference.png", (20, 20), (20, 220, 20, 255))
    records = [
        {"asset_id": "background", "revision": "r1", "asset_type": "background", "lifecycle": "formal", "image_path": "assets/background.png"},
        {"asset_id": "subject", "revision": "r1", "asset_type": "character_identity", "lifecycle": "formal", "image_path": "assets/subject.png"},
    ]
    if include_reference:
        records.append({"asset_id": "reference", "revision": "r1", "asset_type": "key_prop", "lifecycle": "formal", "image_path": "assets/reference.png"})
    return {"schema_version": PLANNER_SCHEMA_VERSION, "asset_roots": ["assets"], "assets": records}


def _storyboard() -> dict:
    return {
        "schema_version": "2.0",
        "canvas": {"width": 100, "height": 80},
        "scenes": [{
            "scene_id": "1", "heading": "scene", "background_asset_id": "background",
            "shots": [{
                "shot_id": "1.1",
                "visual": {"visible_character_ids": ["subject"], "description": "structured test scene"},
                "visible_prop_ids": ["reference"],
                "visual_constraints": [
                    {"constraint_id": "presence", "kind": "presence", "subject_ids": ["subject", "reference"], "hard": True, "provenance": "test"},
                    {"constraint_id": "scale", "kind": "relative_size", "subject_id": "subject", "reference_id": "reference", "dimension": "width", "min_ratio": 0.9, "max_ratio": 1.1, "hard": True, "provenance": "test"},
                    {"constraint_id": "direction", "kind": "direction", "source_id": "subject", "target_id": "reference", "expected_angle_degrees": 0, "tolerance_degrees": 45, "hard": True, "provenance": "test"},
                    {"constraint_id": "safe", "kind": "safe_area", "subject_ids": ["subject", "reference"], "margin_px": 2, "hard": True, "provenance": "test"},
                ],
            }],
        }],
    }


def test_planner_requires_the_optional_solver_only_after_input_validation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    storyboard = _storyboard()
    try:
        import ortools  # noqa: F401
    except ImportError:
        with pytest.raises(PlannerDependencyError, match=r"manju-tool\[planner\]"):
            plan_hybrid_storyboard(storyboard, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    else:
        plan = plan_hybrid_storyboard(storyboard, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
        assert plan["status"] == "ready"


def test_planner_reports_missing_assets_without_guessing_or_loading_solver(tmp_path: Path) -> None:
    registry = _registry(tmp_path, include_reference=False)
    plan = plan_hybrid_storyboard(_storyboard(), registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == NEEDS_ASSETS
    assert plan["shots"][0]["asset_requests"][0]["asset_reference"] == "reference"


def test_planner_blocks_unknown_hard_constraint_without_special_casing(tmp_path: Path) -> None:
    storyboard = _storyboard()
    storyboard["scenes"][0]["shots"][0]["visual_constraints"].append({
        "constraint_id": "unknown", "kind": "hand_contact", "hard": True, "provenance": "test",
    })
    plan = plan_hybrid_storyboard(storyboard, _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == BLOCKED_UNSUPPORTED
    assert plan["shots"][0]["unsupported"][0]["constraint_id"] == "unknown"


def test_registry_rejects_asset_path_outside_the_declared_root(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    outside = tmp_path / "outside.png"
    _image(outside, (2, 2), (1, 2, 3, 255))
    registry["assets"][1]["image_path"] = "outside.png"
    with pytest.raises(ValueError, match="escapes declared asset_roots"):
        plan_hybrid_storyboard(_storyboard(), registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))


def test_migration_is_non_destructive_and_does_not_turn_text_into_hard_geometry() -> None:
    source = _storyboard()
    source.pop("schema_version")
    source["scenes"][0]["shots"][0].pop("visual_constraints")
    original = copy.deepcopy(source)
    candidate, report = migrate_storyboard_visual_constraints(source)
    assert source == original
    migrated = candidate["scenes"][0]["shots"][0]["visual_constraints"]
    assert migrated[0]["kind"] == "presence"
    assert report["candidates"][0]["status"] == "needs_confirmation"


def test_normalize_storyboard_preserves_structured_planner_fields() -> None:
    source = _storyboard()
    source["creative_bible"] = {"style_anchor": "test", "characters": []}
    source["scenes"][0]["shots"][0]["prompts"] = {"image_cn": "x", "image_en": "x"}
    normalized = normalize_storyboard(source)
    shot = normalized["scenes"][0]["shots"][0]
    assert shot["visual_constraints"][0]["constraint_id"] == "presence"


def test_independent_verifier_rejects_count_occlusion_and_reversed_layer_order() -> None:
    scene = {
        "canvas": {"width": 20, "height": 20},
        "layers": [
            {"asset_id": "a@1", "x": 0, "y": 0, "width": 10, "height": 10, "z_index": 2},
            {"asset_id": "b@1", "x": 5, "y": 0, "width": 10, "height": 10, "z_index": 1},
        ],
    }
    verdicts = _verify_scene(scene, plan_constraints=[
        {"constraint_id": "count", "kind": "count", "asset_ids": ["a@1", "b@1"], "expected_count": 3},
        {"constraint_id": "occlusion", "kind": "forbidden_occlusion", "asset_ids": ["a@1", "b@1"]},
        {"constraint_id": "z", "kind": "z_order", "asset_ids": ["a@1", "b@1"]},
    ])
    assert {item["constraint_id"] for item in verdicts if item["verdict"] == "fail"} == {"count", "occlusion", "z"}


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_planner_writes_immutable_scenes_and_cli_verifies_them(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    storyboard = _storyboard()
    storyboard_path = tmp_path / "storyboard.json"
    registry_path = tmp_path / "assets.json"
    storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    output = tmp_path / "plan"
    result = plan_hybrid_storyboard_file(str(storyboard_path), str(registry_path), str(output))
    assert result["plan"]["status"] == "ready"
    scene_path = output / result["plan"]["shots"][0]["scene_path"]
    assert scene_path.is_file()
    assert verify_hybrid_plan(str(output))["status"] == "verified"
    scene_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        verify_hybrid_plan(str(output))
    runner = __import__("click.testing", fromlist=["CliRunner"]).CliRunner()
    response = runner.invoke(cli, ["hybrid-plan", str(storyboard_path), "--assets", str(registry_path), "-o", str(tmp_path / "cli-plan")])
    assert response.exit_code == 0, response.output


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_asset_integrity_cannot_be_bypassed_by_deleting_a_manifest_entry(tmp_path: Path) -> None:
    plan = plan_hybrid_storyboard(_storyboard(), _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    output = tmp_path / "plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    manifest_path = output / "hybrid_plan_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["asset_sha256"].pop("subject@r1")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _image(tmp_path / "assets" / "subject.png", (20, 20), (1, 2, 3, 255))
    with pytest.raises(ValueError, match="asset integrity set"):
        verify_hybrid_plan(str(output))


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_scene_integrity_cannot_be_bypassed_by_deleting_a_manifest_entry(tmp_path: Path) -> None:
    plan = plan_hybrid_storyboard(_storyboard(), _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    output = tmp_path / "plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    manifest_path = output / "hybrid_plan_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scene_sha256"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (output / plan["shots"][0]["scene_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="scene integrity set"):
        verify_hybrid_plan(str(output))


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_scene_content_cannot_be_rebound_by_rewriting_mutable_hash_fields(tmp_path: Path) -> None:
    plan = plan_hybrid_storyboard(_storyboard(), _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    output = tmp_path / "plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    plan_path = output / "hybrid_plan.json"
    manifest_path = output / "hybrid_plan_manifest.json"
    disk_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    scene_relative = disk_plan["shots"][0]["scene_path"]
    scene_path = output / scene_relative
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    scene["layers"][0]["x"] += 1
    scene_path.write_text(json.dumps(scene), encoding="utf-8")
    disk_plan["shots"][0]["scene_sha256"] = hashlib.sha256(scene_path.read_bytes()).hexdigest()
    plan_path.write_text(json.dumps(disk_plan), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manifest["scene_sha256"][scene_relative] = disk_plan["shots"][0]["scene_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="scene content"):
        verify_hybrid_plan(str(output))


def test_planner_rejects_unknown_or_malformed_soft_preferences(tmp_path: Path) -> None:
    for preference in ("invented_preference", "center_subjects"):
        storyboard = _storyboard()
        storyboard["scenes"][0]["shots"][0]["layout_preferences"] = [{
            "preference": preference, "priority": 50, "reason": "validation fixture",
            "source": "model_layout_preference", "affected_asset_ids": "subject",
        }]
        with pytest.raises(ValueError, match="affected_asset_ids" if preference == "center_subjects" else "not supported"):
            plan_hybrid_storyboard(storyboard, _registry(tmp_path / preference), storyboard_dir=str(tmp_path / preference), registry_dir=str(tmp_path / preference))


def test_planner_rejects_soft_preferences_that_target_unknown_assets(tmp_path: Path) -> None:
    storyboard = _storyboard()
    storyboard["scenes"][0]["shots"][0]["layout_preferences"] = [{
        "preference": "center_subjects", "priority": 50, "reason": "typo fixture",
        "source": "model_layout_preference", "affected_asset_ids": ["not-present"],
    }]
    with pytest.raises(ValueError, match="unknown or non-visible asset"):
        plan_hybrid_storyboard(storyboard, _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_direction_solver_accepts_odd_even_center_offsets_inside_the_declared_cone() -> None:
    assets = [
        AssetRecord("a", "1", "fixture", "fixture", "", "", 20, 10),
        AssetRecord("b", "1", "fixture", "fixture", "", "", 10, 10),
    ]
    constraints = [
        {"constraint_id": "size", "kind": "relative_size", "subject_id": "a", "reference_id": "b", "dimension": "width", "min_ratio": 0.5, "max_ratio": 0.5, "asset_ids": ["a@1", "b@1"]},
        {"constraint_id": "contains", "kind": "containment", "subject_id": "a", "container_id": "b", "asset_ids": ["a@1", "b@1"]},
        {"constraint_id": "right", "kind": "direction", "source_id": "a", "target_id": "b", "expected_angle_degrees": 0, "tolerance_degrees": 45, "asset_ids": ["a@1", "b@1"]},
    ]
    placements, conflicts, state = _solve_layout(assets, constraints, canvas_width=100, canvas_height=80, timeout_seconds=5, seed=41027)
    assert state is None and not conflicts
    scene = {"canvas": {"width": 100, "height": 80}, "layers": [{"asset_id": identity, **placement} for identity, placement in placements.items()]}
    assert all(item["verdict"] == "pass" for item in _verify_scene(scene, plan_constraints=constraints))


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_large_canvas_wide_direction_cone_is_fast_and_deterministic() -> None:
    assets = [
        AssetRecord("a", "1", "fixture", "fixture", "", "", 20, 20),
        AssetRecord("b", "1", "fixture", "fixture", "", "", 20, 20),
    ]
    constraints = [{
        "constraint_id": "right", "kind": "direction", "source_id": "a", "target_id": "b",
        "expected_angle_degrees": 0, "tolerance_degrees": 45, "asset_ids": ["a@1", "b@1"],
    }]
    results = [_solve_layout(assets, constraints, canvas_width=800, canvas_height=450, timeout_seconds=5, seed=41027) for _ in range(3)]
    assert all(state is None and not conflicts and placements for placements, conflicts, state in results)
    assert results[0][0] == results[1][0] == results[2][0]


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_coprime_source_dimensions_fit_deterministically_with_hard_render_minimums() -> None:
    assets = [
        AssetRecord("wide", "1", "fixture", "fixture", "", "", 1009, 67),
        AssetRecord("tall", "1", "fixture", "fixture", "", "", 401, 101),
    ]
    results = [_solve_layout(assets, [], canvas_width=320, canvas_height=180, timeout_seconds=5, seed=41027) for _ in range(3)]
    assert all(state is None and not conflicts for _, conflicts, state in results)
    assert results[0][0] == results[1][0] == results[2][0]
    for placement in results[0][0].values():
        assert placement["width"] >= 16 and placement["height"] >= 16
        assert placement["width"] * placement["height"] >= 256
        assert placement["x"] + placement["width"] <= 320
        assert placement["y"] + placement["height"] <= 180
    _, conflicts, state = _solve_layout(
        [AssetRecord("tiny", "1", "fixture", "fixture", "", "", 20, 20)], [],
        canvas_width=100, canvas_height=10, timeout_seconds=5, seed=41027,
    )
    assert state is None and conflicts[0]["constraint_id"] == "minimum_render_size"


def test_containment_requires_explicit_visibility_semantics_and_fails_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _image(tmp_path / "assets" / "container.png", (40, 40), (40, 40, 220, 255))
    registry["assets"].append({"asset_id": "container", "revision": "r1", "asset_type": "key_prop", "lifecycle": "formal", "image_path": "assets/container.png"})
    storyboard = _storyboard()
    storyboard["scenes"][0]["shots"][0]["visible_prop_ids"].append("container")
    storyboard["scenes"][0]["shots"][0]["visual_constraints"].append({
        "constraint_id": "contains", "kind": "containment", "subject_id": "subject", "container_id": "container",
        "hard": True, "provenance": "fixture",
    })
    plan = plan_hybrid_storyboard(storyboard, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == NEEDS_CONSTRAINT_CONFIRMATION
    assert "visibility_mode" in plan["shots"][0]["candidate_constraints"][0]["confirmation_reason"]


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_opaque_containment_is_classified_before_render_but_subject_front_is_visible(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _image(tmp_path / "assets" / "container.png", (40, 40), (40, 40, 220, 255))
    registry["assets"].append({"asset_id": "container", "revision": "r1", "asset_type": "key_prop", "lifecycle": "formal", "image_path": "assets/container.png"})
    base = _storyboard()
    base["scenes"][0]["shots"][0]["visible_prop_ids"].append("container")
    constraint = {
        "constraint_id": "contains", "kind": "containment", "subject_id": "subject", "container_id": "container",
        "hard": True, "provenance": "fixture", "visibility_mode": "transparent_window",
    }
    base["scenes"][0]["shots"][0]["visual_constraints"].append(constraint)
    blocked = plan_hybrid_storyboard(base, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert blocked["status"] == SCENE_CONTRACT_REVISION_REQUIRED
    assert blocked["shots"][0]["visibility_preflight"]["structural_failures"]
    visible = copy.deepcopy(base)
    visible["scenes"][0]["shots"][0]["visual_constraints"][-1]["visibility_mode"] = "subject_in_front"
    plan = plan_hybrid_storyboard(visible, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == "ready"
    assert plan["shots"][0]["visibility_preflight"]["coverages"]["subject@r1"] > 0
    scene_constraint = next(item for item in plan["shots"][0]["scene"]["constraints"] if item["kind"] == "contains_center")
    assert scene_constraint == {
        "constraint_id": "contains", "kind": "contains_center", "subject_id": "subject@r1",
        "container_id": "container@r1", "visibility_mode": "subject_in_front",
    }
    output = tmp_path / "subject-front-plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    assert render_hybrid_scene_file(str(output / plan["shots"][0]["scene_path"]), str(tmp_path / "subject-front-render"))["status"] == "auto_verified"


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_transparent_window_keeps_its_semantics_through_plan_and_renderer(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _window_image(tmp_path / "assets" / "container.png")
    registry["assets"].append({"asset_id": "container", "revision": "r1", "asset_type": "key_prop", "lifecycle": "formal", "image_path": "assets/container.png"})
    storyboard = _storyboard()
    storyboard["scenes"][0]["shots"][0]["visible_prop_ids"].append("container")
    storyboard["scenes"][0]["shots"][0]["visual_constraints"].append({
        "constraint_id": "window", "kind": "containment", "subject_id": "subject", "container_id": "container",
        "hard": True, "provenance": "fixture", "visibility_mode": "transparent_window",
    })
    plan = plan_hybrid_storyboard(storyboard, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == "ready"
    scene_constraint = next(item for item in plan["shots"][0]["scene"]["constraints"] if item["kind"] == "contains_center")
    assert scene_constraint["visibility_mode"] == "transparent_window"
    output = tmp_path / "plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    manifest = render_hybrid_scene_file(str(output / plan["shots"][0]["scene_path"]), str(tmp_path / "render"))
    assert manifest["status"] == "auto_verified"
    assert all(item["verdict"] == "pass" for item in manifest["hard_constraint_verdicts"])


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_layered_container_requires_aligned_front_geometry() -> None:
    assets = [
        AssetRecord("back", "1", "fixture", "fixture", "", "", 64, 64),
        AssetRecord("subject", "1", "fixture", "fixture", "", "", 32, 32),
        AssetRecord("front", "1", "fixture", "fixture", "", "", 64, 64),
    ]
    constraints = [{
        "constraint_id": "layered", "kind": "containment", "subject_id": "subject", "container_id": "back",
        "front_layer_id": "front", "visibility_mode": "layered_container", "asset_ids": ["subject@1", "back@1", "front@1"],
    }]
    placements, conflicts, state = _solve_layout(assets, constraints, canvas_width=200, canvas_height=120, timeout_seconds=5, seed=41027)
    assert state is None and not conflicts
    assert placements["front@1"] | {"z_index": 0} == placements["back@1"] | {"z_index": 0}
    assert placements["back@1"]["z_index"] < placements["subject@1"]["z_index"] < placements["front@1"]["z_index"]


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_planned_vertical_direction_renders_with_the_same_screen_axis(tmp_path: Path) -> None:
    storyboard = _storyboard()
    storyboard["scenes"][0]["shots"][0]["visual_constraints"][2].update({
        "expected_angle_degrees": 90, "tolerance_degrees": 15,
    })
    plan = plan_hybrid_storyboard(storyboard, _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == "ready"
    output = tmp_path / "plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    manifest = render_hybrid_scene_file(str(output / plan["shots"][0]["scene_path"]), str(tmp_path / "render"))
    assert manifest["status"] == "auto_verified"


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_planned_asset_and_required_human_approval_gate_the_renderer(tmp_path: Path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    storyboard = _storyboard()
    plan = plan_hybrid_storyboard(storyboard, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    output = tmp_path / "plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(output))
    scene_path = output / plan["shots"][0]["scene_path"]
    render_hybrid_scene_file(str(scene_path), str(tmp_path / "rendered"))
    _image(tmp_path / "assets" / "subject.png", (20, 20), (1, 2, 3, 255))
    with pytest.raises(ValueError, match="asset integrity"):
        verify_hybrid_plan(str(output))

    reviewed = _storyboard()
    reviewed["scenes"][0]["shots"][0]["layout_preferences"] = [{
        "preference": "center_subjects", "priority": 50, "reason": "review gate fixture",
        "source": "model_layout_preference", "requires_human_review": True,
    }]
    reviewed_plan = plan_hybrid_storyboard(reviewed, _registry(tmp_path / "reviewed"), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path / "reviewed"))
    reviewed_output = tmp_path / "reviewed-plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(reviewed_plan, str(reviewed_output))
    reviewed_scene = reviewed_output / reviewed_plan["shots"][0]["scene_path"]
    monkeypatch.setenv("MANJU_HYBRID_SIGNING_KEY", "test-key")
    with pytest.raises(ValueError, match="approval"):
        render_hybrid_scene_file(str(reviewed_scene), str(tmp_path / "blocked-render"))
    record_hybrid_plan_human_verification(str(reviewed_output), reviewer="qa", note="reviewed composition")
    assert render_hybrid_scene_file(str(reviewed_scene), str(tmp_path / "approved-render"))["status"] == "needs_human_review"


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_render_visibility_evidence_creates_at_most_an_immutable_soft_layout_retry(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan = plan_hybrid_storyboard(_storyboard(), registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == "ready"
    next(layer for layer in plan["shots"][0]["scene"]["layers"] if layer["asset_id"] == "subject@r1")["x"] = 200
    parent_dir = tmp_path / "parent-plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(parent_dir))
    evidence_dir = tmp_path / "render-evidence"
    render_hybrid_scene_file(str(parent_dir / plan["shots"][0]["scene_path"]), str(evidence_dir))
    retried = replan_hybrid_plan_from_render(str(parent_dir), str(evidence_dir), str(tmp_path / "retry-plan"))
    retry_shot = retried["plan"]["shots"][0]
    assert retried["plan"]["render_replan_history"][0]["soft_variables_only"] is True
    assert retry_shot["solver"]["seed"] == plan["shots"][0]["solver"]["seed"] + 1
    assert retry_shot["layout_preferences"][-1]["preference"] == "spread_subjects"
    assert retry_shot["hard_constraints"] == plan["shots"][0]["hard_constraints"]
    assert verify_hybrid_plan(str(parent_dir))["status"] == "verified"


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_replan_only_changes_the_evidence_shot_when_assets_are_shared_across_shots(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    storyboard = _storyboard()
    second_shot = copy.deepcopy(storyboard["scenes"][0]["shots"][0])
    second_shot["shot_id"] = "1.2"
    storyboard["scenes"][0]["shots"].append(second_shot)
    plan = plan_hybrid_storyboard(storyboard, registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    next(
        layer for shot in plan["shots"] if shot["shot_id"] == "1.2"
        for layer in shot["scene"]["layers"] if layer["asset_id"] == "subject@r1"
    )["x"] = 200
    parent_dir = tmp_path / "parent-plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(parent_dir))
    evidence_dir = tmp_path / "render-evidence"
    second_scene = parent_dir / next(shot["scene_path"] for shot in plan["shots"] if shot["shot_id"] == "1.2")
    assert render_hybrid_scene_file(str(second_scene), str(evidence_dir))["status"] == "blocked_unverifiable"
    retried = replan_hybrid_plan_from_render(str(parent_dir), str(evidence_dir), str(tmp_path / "retry-plan"))["plan"]
    original_first = next(shot for shot in plan["shots"] if shot["shot_id"] == "1.1")
    retried_first = next(shot for shot in retried["shots"] if shot["shot_id"] == "1.1")
    retried_second = next(shot for shot in retried["shots"] if shot["shot_id"] == "1.2")
    assert retried_first["scene"]["layers"] == original_first["scene"]["layers"]
    assert retried_first["hard_constraints"] == original_first["hard_constraints"]
    assert retried_second["layout_preferences"][-1]["source"] == "render_visibility_evidence"
    assert verify_hybrid_plan(str(parent_dir))["status"] == "verified"


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_replan_rejects_fake_or_mismatched_renderer_evidence(tmp_path: Path) -> None:
    plan = plan_hybrid_storyboard(_storyboard(), _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    parent_dir = tmp_path / "parent-plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(parent_dir))
    fake_dir = tmp_path / "fake-render"
    fake_dir.mkdir()
    (fake_dir / "hybrid_render.json").write_text(json.dumps({"status": "blocked_unverifiable", "unverifiable_layer_asset_ids": ["subject@r1"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not produced"):
        replan_hybrid_plan_from_render(str(parent_dir), str(fake_dir), str(tmp_path / "retry"))


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_replan_rejects_a_tampered_auto_verified_renderer_manifest(tmp_path: Path) -> None:
    plan = plan_hybrid_storyboard(_storyboard(), _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    parent_dir = tmp_path / "parent-plan"
    __import__("manju.pipeline.visual.planner", fromlist=["write_hybrid_plan"]).write_hybrid_plan(plan, str(parent_dir))
    render_dir = tmp_path / "render"
    render_hybrid_scene_file(str(parent_dir / plan["shots"][0]["scene_path"]), str(render_dir))
    root_manifest_path = render_dir / "hybrid_render.json"
    manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "auto_verified"
    manifest["status"] = "blocked_unverifiable"
    manifest["unverifiable_layer_asset_ids"] = ["subject@r1"]
    root_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifact_manifest_path = render_dir / "stages" / "hybrid_renderer" / "runs" / manifest["run_id"] / "artifacts" / "hybrid_manifest.json"
    artifact_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="event integrity"):
        replan_hybrid_plan_from_render(str(parent_dir), str(render_dir), str(tmp_path / "retry"))


@pytest.mark.skipif(__import__("importlib").util.find_spec("ortools") is None, reason="requires manju-tool[planner]")
def test_alpha_invisible_asset_is_blocked_before_any_render_replan(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _image(tmp_path / "assets" / "subject.png", (20, 20), (230, 20, 20, 0))
    plan = plan_hybrid_storyboard(_storyboard(), registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path))
    assert plan["status"] == "blocked_conflict"
    assert plan["shots"][0]["visibility_preflight"]["failures"]
