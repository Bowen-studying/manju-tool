from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from manju.cli import cli
from manju.pipeline.visual import asset_intake
from manju.pipeline.visual.asset_intake import (
    ASSET_PROMOTION_SIGNING_KEY_ENV,
    inspect_asset_candidate,
    promote_asset_candidate,
    verify_asset_promotion,
)
from manju.pipeline.visual.planner import PLANNER_SCHEMA_VERSION, PlannerError, plan_hybrid_storyboard, verify_hybrid_plan, write_hybrid_plan


def _image(path: Path, color: tuple[int, int, int, int] = (220, 30, 40, 128)) -> None:
    Image.new("RGBA", (20, 10), color).save(path)


def _storyboard() -> dict:
    return {
        "schema_version": "2.0",
        "canvas": {"width": 100, "height": 60},
        "scenes": [{
            "scene_id": "scene", "heading": "generic", "background_asset_id": "background",
            "shots": [{
                "shot_id": "shot", "visual": {"visible_character_ids": ["subject"]},
                "visual_constraints": [{
                    "constraint_id": "presence", "kind": "presence", "subject_ids": ["subject"],
                    "hard": True, "provenance": "fixture",
                }],
            }],
        }],
    }


def _registry(root: Path, *, promotion_path: str = "") -> dict:
    records = [
        {
            "asset_id": "background", "revision": "1", "asset_type": "background",
            "lifecycle": "formal", "source_kind": "local", "image_path": str(root / "background.png"),
        },
        {
            "asset_id": "subject", "revision": "1", "asset_type": "character_identity",
            "lifecycle": "formal", "source_kind": "provider", "image_path": str(root / "subject.png"),
        },
    ]
    if promotion_path:
        records[1]["promotion_path"] = promotion_path
    return {"schema_version": PLANNER_SCHEMA_VERSION, "asset_roots": [str(root)], "assets": records}


def test_provider_asset_cannot_become_formal_from_lifecycle_text_alone(tmp_path: Path) -> None:
    _image(tmp_path / "background.png", (20, 30, 40, 255))
    _image(tmp_path / "subject.png")
    with pytest.raises(PlannerError, match="promotion_path is required"):
        plan_hybrid_storyboard(
            _storyboard(), _registry(tmp_path), storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def test_inspection_promotion_and_planner_bind_the_exact_provider_asset(tmp_path: Path, monkeypatch) -> None:
    _image(tmp_path / "background.png", (20, 30, 40, 255))
    subject = tmp_path / "subject.png"
    _image(subject)
    candidate_path = tmp_path / "candidate.json"
    candidate = inspect_asset_candidate(
        str(subject), str(candidate_path), asset_id="subject", revision="1",
        asset_type="character_identity", source_kind="provider",
    )
    assert candidate["asset"]["has_alpha"] is True
    assert candidate["asset"]["nontransparent_pixel_count"] == 200
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "test-promotion-key-0123456789abcdef")
    promotion_path = tmp_path / "promotion.json"
    promotion = promote_asset_candidate(
        str(candidate_path), str(promotion_path), reviewer="qa", note="identity and alpha edge reviewed",
    )
    assert promotion["status"] == "human_promoted"
    assert promotion["signing_key_id"].startswith("sha256:")

    plan = plan_hybrid_storyboard(
        _storyboard(), _registry(tmp_path, promotion_path=str(promotion_path)),
        storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
    )
    assert plan["status"] == "ready"
    subject_record = next(item for item in plan["global_assets"] if item["asset_id"] == "subject")
    assert subject_record["source_kind"] == "provider"
    assert subject_record["promotion_sha256"]
    assert subject_record["promotion_evidence"]["reviewer"] == "qa"

    output = tmp_path / "plan"
    write_hybrid_plan(plan, str(output))
    assert verify_hybrid_plan(str(output))["status"] == "verified"

    _image(subject, (1, 2, 3, 255))
    with pytest.raises(PlannerError, match="does not match the registry asset"):
        plan_hybrid_storyboard(
            _storyboard(), _registry(tmp_path, promotion_path=str(promotion_path)),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def test_asset_intake_cli_is_offline_and_refuses_overwriting_evidence(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "asset.png"
    _image(image)
    candidate = tmp_path / "candidate.json"
    runner = CliRunner()
    inspected = runner.invoke(cli, [
        "hybrid-asset-inspect", str(image), "--asset-id", "asset", "--revision", "2",
        "--asset-type", "key_prop", "-o", str(candidate),
    ])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(candidate.read_text(encoding="utf-8"))["status"] == "technically_inspected"
    assert runner.invoke(cli, [
        "hybrid-asset-inspect", str(image), "--asset-id", "asset", "--revision", "2",
        "--asset-type", "key_prop", "-o", str(candidate),
    ]).exit_code != 0

    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "test-promotion-key-0123456789abcdef")
    promotion = tmp_path / "promotion.json"
    promoted = runner.invoke(cli, [
        "hybrid-asset-promote", str(candidate), "--reviewer", "qa", "--note", "checked", "-o", str(promotion),
    ])
    assert promoted.exit_code == 0, promoted.output
    assert json.loads(promotion.read_text(encoding="utf-8"))["integrity_signature"]


@pytest.mark.parametrize("mutation", [
    lambda report: report.update({"fingerprint": "invented"}),
    lambda report: report["asset"].update({"width": 999}),
    lambda report: report.update({"schema_version": 99}),
])
def test_promotion_rejects_forged_or_tampered_technical_inspection(
    tmp_path: Path, monkeypatch, mutation,
) -> None:
    image = tmp_path / "asset.png"
    _image(image)
    candidate_path = tmp_path / "candidate.json"
    candidate = inspect_asset_candidate(
        str(image), str(candidate_path), asset_id="asset", revision="1",
        asset_type="key_prop", source_kind="provider",
    )
    mutation(candidate)
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "test-promotion-key-0123456789abcdef")
    with pytest.raises(ValueError):
        promote_asset_candidate(
            str(candidate_path), str(tmp_path / "promotion.json"), reviewer="qa", note="checked",
        )


def test_promotion_requires_a_strong_key_and_binds_a_non_secret_key_identifier(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "asset.png"
    _image(image)
    candidate_path = tmp_path / "candidate.json"
    inspect_asset_candidate(str(image), str(candidate_path), asset_id="asset", revision="1", asset_type="key_prop")
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "short-demo-key")
    with pytest.raises(ValueError, match="at least 32"):
        promote_asset_candidate(str(candidate_path), str(tmp_path / "weak.json"), reviewer="qa", note="checked")
    strong_key = "test-promotion-key-0123456789abcdef"
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, strong_key)
    promotion_path = tmp_path / "promotion.json"
    promotion = promote_asset_candidate(str(candidate_path), str(promotion_path), reviewer="qa", note="checked")
    assert strong_key not in json.dumps(promotion)
    assert promotion["signing_key_id"].startswith("sha256:")
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "different-test-promotion-key-0123456789")
    with pytest.raises(ValueError, match="identifier"):
        verify_asset_promotion(
            str(promotion_path), asset_id="asset", revision="1", asset_type="key_prop",
            image_path=str(image), image_sha256=promotion["asset"]["image_sha256"], width=20, height=10,
        )


def test_derivative_asset_lineage_is_hashed_and_tampering_is_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "source.png"
    _image(parent, (20, 40, 60, 255))
    image = tmp_path / "derived.png"
    _image(image)
    output_hash = __import__("hashlib").sha256(image.read_bytes()).hexdigest()
    lineage = {
        "parent_image_path": str(parent),
        "parent_image_sha256": __import__("hashlib").sha256(parent.read_bytes()).hexdigest(),
        "operation": "background_matting",
        "operation_version": "1",
        "parameter_fingerprint": "b" * 64,
        "mask_sha256": "c" * 64,
        "output_image_sha256": output_hash,
    }
    candidate_path = tmp_path / "candidate.json"
    candidate = inspect_asset_candidate(
        str(image), str(candidate_path), asset_id="derived", revision="2", asset_type="key_prop",
        technical_requirements={"requires_alpha": True, "minimum_alpha_coverage": 0.1}, derivation=lineage,
    )
    assert candidate["derivation"]["output_image_sha256"] == output_hash
    assert candidate["derivation"]["parent_image_sha256"] == lineage["parent_image_sha256"]
    candidate["derivation"]["output_image_sha256"] = "d" * 64
    candidate["fingerprint"] = __import__("manju.utils.runtime", fromlist=["content_fingerprint"]).content_fingerprint(
        {key: value for key, value in candidate.items() if key != "fingerprint"}, length=64,
    )
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    with pytest.raises(ValueError, match="output_image_sha256"):
        promote_asset_candidate(str(candidate_path), str(tmp_path / "promotion.json"), reviewer="qa", note="checked")


def test_derivative_promotion_can_relocate_parent_only_inside_declared_asset_roots(tmp_path: Path, monkeypatch) -> None:
    original = tmp_path / "original"
    original.mkdir()
    parent = original / "source.png"
    _image(parent, (20, 40, 60, 255))
    root = tmp_path / "registry-assets"
    root.mkdir()
    _image(root / "background.png", (20, 30, 40, 255))
    derived = root / "subject.png"
    _image(derived)
    output_hash = __import__("hashlib").sha256(derived.read_bytes()).hexdigest()
    lineage = {
        "parent_image_path": str(parent),
        "parent_image_sha256": __import__("hashlib").sha256(parent.read_bytes()).hexdigest(),
        "operation": "alpha_matte",
        "operation_version": "1.2",
        "parameter_fingerprint": "b" * 64,
        "output_image_sha256": output_hash,
    }
    candidate_path = tmp_path / "candidate.json"
    inspect_asset_candidate(
        str(derived), str(candidate_path), asset_id="subject", revision="2",
        asset_type="character_identity", source_kind="provider", derivation=lineage,
    )
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "test-promotion-key-0123456789abcdef")
    promotion_path = tmp_path / "promotion.json"
    promote_asset_candidate(str(candidate_path), str(promotion_path), reviewer="qa", note="lineage checked")

    relocated = root / "lineage" / "source-copy.png"
    relocated.parent.mkdir()
    relocated.write_bytes(parent.read_bytes())
    parent.unlink()

    registry = _registry(root, promotion_path=str(promotion_path))
    registry["assets"][1]["revision"] = "2"
    plan = plan_hybrid_storyboard(
        _storyboard(), registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
    )
    assert plan["status"] == "ready"
    output = tmp_path / "plan"
    write_hybrid_plan(plan, str(output))
    assert verify_hybrid_plan(str(output))["status"] == "verified"


def test_derivative_promotion_refuses_parent_outside_declared_asset_roots(tmp_path: Path, monkeypatch) -> None:
    original = tmp_path / "original"
    original.mkdir()
    parent = original / "source.png"
    _image(parent, (20, 40, 60, 255))
    root = tmp_path / "registry-assets"
    root.mkdir()
    _image(root / "background.png", (20, 30, 40, 255))
    derived = root / "subject.png"
    _image(derived)
    lineage = {
        "parent_image_path": str(parent),
        "parent_image_sha256": __import__("hashlib").sha256(parent.read_bytes()).hexdigest(),
        "operation": "alpha_matte",
        "operation_version": "1.2",
        "parameter_fingerprint": "b" * 64,
        "output_image_sha256": __import__("hashlib").sha256(derived.read_bytes()).hexdigest(),
    }
    candidate_path = tmp_path / "candidate.json"
    inspect_asset_candidate(
        str(derived), str(candidate_path), asset_id="subject", revision="2",
        asset_type="character_identity", source_kind="provider", derivation=lineage,
    )
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "test-promotion-key-0123456789abcdef")
    promotion_path = tmp_path / "promotion.json"
    promote_asset_candidate(str(candidate_path), str(promotion_path), reviewer="qa", note="lineage checked")
    parent.unlink()

    registry = _registry(root, promotion_path=str(promotion_path))
    registry["assets"][1]["revision"] = "2"
    with pytest.raises(PlannerError, match="no matching parent exists in declared asset_roots"):
        plan_hybrid_storyboard(
            _storyboard(), registry, storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def _promoted_derivative(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    original = tmp_path / "original"
    original.mkdir()
    parent = original / "source.png"
    _image(parent, (20, 40, 60, 255))
    root = tmp_path / "registry-assets"
    root.mkdir()
    _image(root / "background.png", (20, 30, 40, 255))
    derived = root / "subject.png"
    _image(derived)
    lineage = {
        "parent_image_path": str(parent),
        "parent_image_sha256": __import__("hashlib").sha256(parent.read_bytes()).hexdigest(),
        "operation": "alpha_matte",
        "operation_version": "1.2",
        "parameter_fingerprint": "b" * 64,
        "output_image_sha256": __import__("hashlib").sha256(derived.read_bytes()).hexdigest(),
    }
    candidate_path = tmp_path / "candidate.json"
    inspect_asset_candidate(
        str(derived), str(candidate_path), asset_id="subject", revision="2",
        asset_type="character_identity", source_kind="provider", derivation=lineage,
    )
    monkeypatch.setenv(ASSET_PROMOTION_SIGNING_KEY_ENV, "test-promotion-key-0123456789abcdef")
    promotion_path = tmp_path / "promotion.json"
    promote_asset_candidate(str(candidate_path), str(promotion_path), reviewer="qa", note="lineage checked")
    return root, parent, promotion_path


def _registry_for_derivative(root: Path, promotion_path: Path) -> dict:
    registry = _registry(root, promotion_path=str(promotion_path))
    registry["assets"][1]["revision"] = "2"
    return registry


def test_derivative_promotion_refuses_existing_parent_with_wrong_sha_and_no_root_match(tmp_path: Path, monkeypatch) -> None:
    root, parent, promotion_path = _promoted_derivative(tmp_path, monkeypatch)
    (root / "matching-parent-copy.png").write_bytes(parent.read_bytes())
    _image(parent, (1, 2, 3, 255))
    with pytest.raises(PlannerError, match="derivation parent_image_sha256 does not match parent_image_path"):
        plan_hybrid_storyboard(
            _storyboard(), _registry_for_derivative(root, promotion_path),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def test_derivative_parent_search_bounds_empty_directories(tmp_path: Path, monkeypatch) -> None:
    root, parent, promotion_path = _promoted_derivative(tmp_path, monkeypatch)
    parent.unlink()
    (root / "empty-a").mkdir()
    (root / "empty-b").mkdir()
    monkeypatch.setattr(asset_intake, "_MAX_DERIVATION_ROOT_ENTRIES", 1)
    with pytest.raises(PlannerError, match="declared-root entry limit"):
        plan_hybrid_storyboard(
            _storyboard(), _registry_for_derivative(root, promotion_path),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def test_derivative_parent_search_does_not_follow_root_symlinks(tmp_path: Path, monkeypatch) -> None:
    root, parent, promotion_path = _promoted_derivative(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source-copy.png").write_bytes(parent.read_bytes())
    parent.unlink()
    link = root / "linked-lineage"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable in this test environment: {exc}")
    with pytest.raises(PlannerError, match="no matching parent exists in declared asset_roots"):
        plan_hybrid_storyboard(
            _storyboard(), _registry_for_derivative(root, promotion_path),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def test_derivative_promotion_refuses_a_recorded_parent_path_replaced_by_symlink(tmp_path: Path, monkeypatch) -> None:
    root, parent, promotion_path = _promoted_derivative(tmp_path, monkeypatch)
    outside = tmp_path / "outside-parent.png"
    outside.write_bytes(parent.read_bytes())
    parent.unlink()
    try:
        parent.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable in this test environment: {exc}")
    with pytest.raises(PlannerError, match="resolves through a symlink or junction"):
        plan_hybrid_storyboard(
            _storyboard(), _registry_for_derivative(root, promotion_path),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )


def test_derivative_promotion_refuses_a_recorded_parent_directory_replaced_by_junction(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("native junctions require Windows")
    root, parent, promotion_path = _promoted_derivative(tmp_path, monkeypatch)
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    (outside / "source.png").write_bytes(parent.read_bytes())
    parent.unlink()
    parent.parent.rmdir()
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(parent.parent), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("native junction creation is unavailable in this test environment")
    with pytest.raises(PlannerError, match="resolves through a symlink or junction"):
        plan_hybrid_storyboard(
            _storyboard(), _registry_for_derivative(root, promotion_path),
            storyboard_dir=str(tmp_path), registry_dir=str(tmp_path),
        )
