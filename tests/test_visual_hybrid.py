from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from manju.cli import cli
from manju.pipeline.visual.hybrid import (
    HYBRID_PIPELINE_NAME,
    HYBRID_SCHEMA_VERSION,
    load_hybrid_scene,
    render_hybrid_scene,
    record_hybrid_human_verification,
    scene_from_dict,
    verify_hard_constraints,
)
from manju.pipeline.visual.store import VisualEventStore


def _image(path: Path, color: tuple[int, int, int, int], size: tuple[int, int]) -> None:
    Image.new("RGBA", size, color).save(path)


def _scene(tmp_path: Path, *, human_review_required: bool = False) -> dict:
    background = tmp_path / "background.png"
    prop = tmp_path / "prop.png"
    target = tmp_path / "target.png"
    _image(background, (10, 20, 30, 255), (100, 80))
    _image(prop, (255, 0, 0, 255), (20, 20))
    _image(target, (0, 255, 0, 255), (20, 20))
    return {
        "schema_version": HYBRID_SCHEMA_VERSION,
        "canvas": {"width": 100, "height": 80},
        "background_path": str(background),
        "layers": [
            {
                "asset_id": "prop", "image_path": str(prop),
                "x": 10, "y": 30, "width": 10, "height": 10,
                "z_index": 1, "human_review_required": human_review_required,
            },
            {
                "asset_id": "target", "image_path": str(target),
                "x": 50, "y": 30, "width": 20, "height": 20, "z_index": 2,
            },
        ],
        "constraints": [
            {
                "constraint_id": "scale", "kind": "relative_size",
                "subject_id": "prop", "reference_id": "target",
                "dimension": "width", "min_ratio": 0.4, "max_ratio": 0.6,
            },
            {
                "constraint_id": "direction", "kind": "direction",
                "source_id": "prop", "target_id": "target",
                "expected_angle_degrees": 0, "tolerance_degrees": 10,
            },
            {
                "constraint_id": "count", "kind": "count",
                "subject_ids": ["prop", "target"], "expected_count": 2,
            },
        ],
    }


def test_hybrid_renderer_preserves_background_outside_editable_layers(tmp_path: Path) -> None:
    scene = scene_from_dict(_scene(tmp_path), base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="hybrid-run")

    assert manifest["status"] == "auto_verified"
    assert manifest["unverifiable_layer_asset_ids"] == []
    assert manifest["protection"]["protected_pixel_delta_count"] == 0
    assert all(item["verdict"] == "pass" for item in manifest["hard_constraint_verdicts"])
    rendered = tmp_path / "output" / manifest["artifact"]["rendered_path"]
    assert rendered.is_file()
    assert manifest["artifact"]["rendered_sha256"]
    store = VisualEventStore(str(tmp_path / "output"), "hybrid-run", pipeline_name=HYBRID_PIPELINE_NAME)
    assert store.recover_state()["render_mode"] == "hybrid"


def test_hybrid_renderer_blocks_failed_or_unverifiable_hard_constraints(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["constraints"][0]["min_ratio"] = 0.6
    payload["constraints"][0]["max_ratio"] = 0.8
    payload["constraints"].append({"constraint_id": "unknown", "kind": "not_supported"})
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="blocked-run")

    assert manifest["status"] == "blocked_hard_constraint"
    verdicts = {item["constraint_id"]: item["verdict"] for item in manifest["hard_constraint_verdicts"]}
    assert verdicts == {"scale": "fail", "direction": "pass", "count": "pass", "unknown": "unverifiable"}


def test_hybrid_renderer_requires_human_review_without_relaxing_hard_checks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MANJU_HYBRID_SIGNING_KEY", "test-signing-key")
    scene = scene_from_dict(_scene(tmp_path, human_review_required=True), base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="review-run")

    assert manifest["status"] == "needs_human_review"
    assert manifest["human_review_required"] is True
    assert all(item["verdict"] == "pass" for item in manifest["hard_constraint_verdicts"])
    verification = record_hybrid_human_verification(
        str(tmp_path / "output"), reviewer="qa", note="complex hand overlap reviewed",
    )
    assert verification["status"] == "human_verified"
    store = VisualEventStore(str(tmp_path / "output"), "review-run", pipeline_name=HYBRID_PIPELINE_NAME)
    assert store.recover_state()["status"] == "completed"
    with pytest.raises(ValueError, match="immutable"):
        record_hybrid_human_verification(
            str(tmp_path / "output"), reviewer="qa", note="different note",
        )


def test_hybrid_renderer_applies_mask_without_changing_protected_pixels(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    mask = tmp_path / "prop-mask.png"
    Image.new("L", (20, 20), 0).save(mask)
    payload["layers"][0]["mask_path"] = str(mask)
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="masked-run")

    assert manifest["status"] == "blocked_unverifiable"
    assert manifest["unverifiable_layer_asset_ids"] == ["prop"]
    assert manifest["protection"]["editable_pixels"] == 400
    with Image.open(tmp_path / "output" / manifest["artifact"]["rendered_path"]) as rendered:
        assert rendered.convert("RGBA").getpixel((12, 32)) == (10, 20, 30, 255)


def test_hybrid_renderer_blocks_a_layer_that_is_outside_the_canvas(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["layers"][0]["x"] = 200
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="off-canvas-run")

    assert manifest["status"] == "blocked_unverifiable"
    assert manifest["unverifiable_layer_asset_ids"] == ["prop"]
    assert manifest["layers"][0]["visible_coverage"] == 0.0


def test_hybrid_protection_accepts_semitransparent_and_rotated_editable_pixels(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    Image.new("RGBA", (20, 20), (255, 0, 0, 128)).save(tmp_path / "prop.png")
    payload["layers"][0]["rotation_degrees"] = 25
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="alpha-run")

    assert manifest["status"] == "auto_verified"
    assert manifest["protection"]["protected_pixel_delta_count"] == 0


def test_hybrid_constraints_use_post_rotation_geometry_for_direction_and_containment(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["layers"] = [
        {
            "asset_id": "prop", "image_path": payload["layers"][0]["image_path"],
            "x": 0, "y": 0, "width": 10, "height": 2, "rotation_degrees": 90,
        },
        {
            "asset_id": "container", "image_path": payload["layers"][1]["image_path"],
            "x": 0, "y": 0, "width": 2, "height": 10,
        },
        {
            "asset_id": "target", "image_path": payload["layers"][1]["image_path"],
            "x": 10, "y": 0, "width": 2, "height": 10,
        },
    ]
    payload["constraints"] = [
        {
            "constraint_id": "direction", "kind": "direction",
            "source_id": "prop", "target_id": "target",
            "expected_angle_degrees": 0, "tolerance_degrees": 1,
        },
        {
            "constraint_id": "containment", "kind": "contains_center",
            "subject_id": "prop", "container_id": "container",
        },
    ]
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    verdicts = {item["constraint_id"]: item for item in verify_hard_constraints(scene)}

    assert verdicts["direction"]["verdict"] == "pass"
    assert verdicts["containment"]["verdict"] == "pass"
    assert verdicts["direction"]["evidence"]["angle_degrees"] == pytest.approx(0.0)


def test_transparent_window_requires_a_visible_subject_at_a_nonopaque_center(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    window = Image.new("RGBA", (20, 20), (0, 255, 0, 255))
    window.paste((0, 0, 0, 0), (5, 5, 15, 15))
    window.save(tmp_path / "target.png")
    payload["layers"] = [
        {"asset_id": "prop", "image_path": payload["layers"][0]["image_path"], "x": 35, "y": 35, "width": 10, "height": 10, "z_index": 1},
        {"asset_id": "container", "image_path": str(tmp_path / "target.png"), "x": 30, "y": 30, "width": 20, "height": 20, "z_index": 2},
    ]
    payload["constraints"] = [{
        "constraint_id": "window", "kind": "contains_center", "subject_id": "prop", "container_id": "container",
        "visibility_mode": "transparent_window",
    }]
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    passed = verify_hard_constraints(scene, visible_asset_ids={"prop", "container"})[0]
    assert passed["verdict"] == "pass"
    assert passed["evidence"]["container_alpha"] == 0

    failed = verify_hard_constraints(scene, visible_asset_ids={"container"})[0]
    assert failed["verdict"] == "fail"


@pytest.mark.parametrize(("angle", "target_position"), [
    (0, (60, 30)), (45, (60, 50)), (90, (40, 50)), (135, (20, 50)),
    (180, (20, 30)), (225, (20, 10)), (270, (40, 10)), (315, (60, 10)),
])
def test_hybrid_direction_uses_the_same_downward_screen_axis_as_the_planner(
    tmp_path: Path, angle: int, target_position: tuple[int, int],
) -> None:
    payload = _scene(tmp_path)
    payload["layers"][0].update({"x": 40, "y": 30, "width": 10, "height": 10})
    payload["layers"][1].update({"x": target_position[0], "y": target_position[1], "width": 10, "height": 10})
    payload["constraints"] = [{
        "constraint_id": "direction", "kind": "direction",
        "source_id": "prop", "target_id": "target",
        "expected_angle_degrees": angle, "tolerance_degrees": 1,
    }]
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    verdict = verify_hard_constraints(scene)[0]
    assert verdict["verdict"] == "pass"
    assert verdict["evidence"]["angle_degrees"] == pytest.approx(float(angle))


def test_hybrid_human_verification_rejects_tampered_manifest_or_rendered_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MANJU_HYBRID_SIGNING_KEY", "test-signing-key")
    scene = scene_from_dict(_scene(tmp_path, human_review_required=True), base_dir=str(tmp_path))
    output_dir = tmp_path / "output"
    manifest = render_hybrid_scene(scene, str(output_dir), run_id="tamper-run")
    manifest_path = output_dir / "stages" / HYBRID_PIPELINE_NAME / "runs" / "tamper-run" / "artifacts" / "hybrid_manifest.json"
    rendered_path = output_dir / manifest["artifact"]["rendered_path"]

    altered = json.loads(manifest_path.read_text(encoding="utf-8"))
    altered["hard_constraint_verdicts"][0]["verdict"] = "fail"
    manifest_path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest integrity"):
        record_hybrid_human_verification(str(output_dir), reviewer="qa", note="reviewed")

    render_hybrid_scene(scene, str(output_dir), run_id="artifact-run")
    artifact_path = output_dir / "stages" / HYBRID_PIPELINE_NAME / "runs" / "artifact-run" / "artifacts" / "rendered.png"
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="artifact integrity"):
        record_hybrid_human_verification(
            str(output_dir), reviewer="qa", note="reviewed", run_id="artifact-run",
        )


def test_hybrid_scene_rejects_missing_or_duplicate_assets(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["layers"][1]["asset_id"] = "prop"
    with pytest.raises(ValueError, match="duplicate"):
        scene_from_dict(payload, base_dir=str(tmp_path))

    payload = _scene(tmp_path)
    payload["background_path"] = "missing.png"
    with pytest.raises(ValueError, match="does not exist"):
        scene_from_dict(payload, base_dir=str(tmp_path))


def test_hybrid_scene_file_is_portable_relative_to_its_json(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    for key in ("background_path",):
        payload[key] = Path(payload[key]).name
    for layer in payload["layers"]:
        layer["image_path"] = Path(layer["image_path"]).name
    scene_path = tmp_path / "scene.json"
    scene_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_hybrid_scene(str(scene_path))
    assert loaded.background_path == str(tmp_path / "background.png")
    assert [layer.asset_id for layer in loaded.layers] == ["prop", "target"]


def test_constraint_checks_use_declared_scene_geometry_not_model_confidence(tmp_path: Path) -> None:
    scene = scene_from_dict(_scene(tmp_path), base_dir=str(tmp_path))
    verdicts = verify_hard_constraints(scene)
    assert {item["kind"] for item in verdicts} == {"relative_size", "direction", "count"}
    assert all("confidence" not in item for item in verdicts)


def test_hybrid_cli_renders_and_records_human_verification_without_any_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MANJU_HYBRID_SIGNING_KEY", "test-signing-key")
    payload = _scene(tmp_path, human_review_required=True)
    for key in ("background_path",):
        payload[key] = Path(payload[key]).name
    for layer in payload["layers"]:
        layer["image_path"] = Path(layer["image_path"]).name
    scene_path = tmp_path / "scene.json"
    scene_path.write_text(json.dumps(payload), encoding="utf-8")
    output_dir = tmp_path / "output"
    runner = CliRunner()

    rendered = runner.invoke(cli, ["hybrid-render", str(scene_path), "-o", str(output_dir)])
    assert rendered.exit_code == 2
    manifest = json.loads((output_dir / "hybrid_render.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "needs_human_review"
    approved = runner.invoke(cli, [
        "hybrid-approve", str(output_dir), "--reviewer", "qa", "--note", "verified hand overlap",
    ])
    assert approved.exit_code == 0, approved.output
    assert json.loads(approved.output)["status"] == "human_verified"


def test_hybrid_human_review_fails_closed_without_an_external_signing_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MANJU_HYBRID_SIGNING_KEY", raising=False)
    scene = scene_from_dict(_scene(tmp_path, human_review_required=True), base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="unsigned-review")

    assert manifest["status"] == "blocked_integrity_key_required"
    with pytest.raises(ValueError, match="event-recorded needs_human_review"):
        record_hybrid_human_verification(str(tmp_path / "output"), reviewer="qa", note="reviewed")


def test_hybrid_rejects_path_traversal_run_ids_without_creating_legacy_visual_paths(tmp_path: Path) -> None:
    scene = scene_from_dict(_scene(tmp_path), base_dir=str(tmp_path))
    with pytest.raises(ValueError, match="single directory"):
        render_hybrid_scene(scene, str(tmp_path / "output"), run_id="..\\..\\visual_agent\\runs\\poison")
    with pytest.raises(ValueError, match="single directory"):
        render_hybrid_scene(scene, str(tmp_path / "output"), run_id="C:")
    assert not (tmp_path / "output" / "stages" / "visual_agent").exists()


def test_hybrid_blocks_transparent_container_even_when_subject_center_is_inside_its_bounds(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    transparent_container = tmp_path / "container.png"
    container = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    container.putpixel((0, 0), (0, 255, 0, 255))
    container.save(transparent_container)
    payload["layers"] = [
        {
            "asset_id": "subject", "image_path": payload["layers"][0]["image_path"],
            "x": 8, "y": 8, "width": 4, "height": 4,
        },
        {
            "asset_id": "container", "image_path": str(transparent_container),
            "x": 0, "y": 0, "width": 20, "height": 20,
            "minimum_visible_coverage": 0.001,
        },
    ]
    payload["constraints"] = [{
        "constraint_id": "contains", "kind": "contains_center",
        "subject_id": "subject", "container_id": "container",
    }]
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="transparent-container")

    assert manifest["status"] == "blocked_hard_constraint"
    assert manifest["hard_constraint_verdicts"][0]["verdict"] == "fail"
    assert manifest["hard_constraint_verdicts"][0]["evidence"]["container_alpha"] == 0


def test_zero_visible_coverage_is_blocked_even_when_layer_threshold_is_zero(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["layers"][0].update({"x": 200, "minimum_visible_coverage": 0})
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="zero-threshold")

    assert manifest["status"] == "blocked_unverifiable"
    assert manifest["unverifiable_layer_asset_ids"] == ["prop"]


def test_hybrid_reused_run_id_rejects_before_overwriting_existing_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    scene = scene_from_dict(_scene(tmp_path), base_dir=str(tmp_path))
    first = render_hybrid_scene(scene, str(output_dir), run_id="stable-run")
    rendered_path = output_dir / first["artifact"]["rendered_path"]
    before = rendered_path.read_bytes()
    payload = _scene(tmp_path)
    payload["layers"][0]["x"] = 25
    changed_scene = scene_from_dict(payload, base_dir=str(tmp_path))

    with pytest.raises(ValueError, match="immutable"):
        render_hybrid_scene(changed_scene, str(output_dir), run_id="stable-run")
    assert rendered_path.read_bytes() == before


def test_hybrid_blocks_a_key_layer_fully_hidden_by_a_higher_z_layer(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["layers"][1].update({"x": 10, "y": 30, "width": 10, "height": 10})
    scene = scene_from_dict(payload, base_dir=str(tmp_path))
    manifest = render_hybrid_scene(scene, str(tmp_path / "output"), run_id="occluded-run")

    assert manifest["status"] == "blocked_unverifiable"
    evidence = {item["asset_id"]: item for item in manifest["layers"]}
    assert evidence["prop"]["visible_coverage"] == 0.0
    assert manifest["unverifiable_layer_asset_ids"] == ["prop"]


def test_hybrid_scene_rejects_lossy_numeric_and_non_boolean_inputs(tmp_path: Path) -> None:
    payload = _scene(tmp_path)
    payload["canvas"]["width"] = 100.9
    with pytest.raises(ValueError, match="canvas.width must be an integer"):
        scene_from_dict(payload, base_dir=str(tmp_path))

    payload = _scene(tmp_path)
    payload["layers"][0]["human_review_required"] = "false"
    with pytest.raises(ValueError, match="must be a boolean"):
        scene_from_dict(payload, base_dir=str(tmp_path))
