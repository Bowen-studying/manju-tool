from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import zlib

import pytest

from manju.pipeline.visual.events import event_json, new_event
import manju.pipeline.visual.evaluation as evaluation_module
from manju.production.approvals import ApprovalRequest, Grant
from manju.production.events import EventStore
from manju.production.operations import OperationRecord
from manju.production.security import MappingHmacKeyProvider

from manju.pipeline.visual.evaluation import (
    CONTRACT_SCHEMA_VERSION,
    DIMENSIONS,
    M8EvaluationError,
    PREFERENCE_THRESHOLD,
    SAMPLE_MANIFEST_SCHEMA_VERSION,
    aggregate_reviews,
    attest_pair_evidence,
    build_contract,
    build_sample_manifest,
    freeze_m8,
    generate_blind_materials,
    validate_sample_manifest,
    _sanitize_jpeg,
    _sanitize_webp,
)


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PRODUCTION_KEY_ID = "m8-test-key"
PRODUCTION_KEY = b"m8-offline-test-key"


@pytest.fixture(autouse=True)
def _trusted_production_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANJU_PRODUCTION_HMAC_KEY", PRODUCTION_KEY.decode("ascii"))
    monkeypatch.delenv("MANJU_M8_PRODUCTION_HEADS_JSON", raising=False)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pair_evidence(
    root: Path,
    *,
    group_id: str,
    story_id: str,
    source_text: str,
    agent: Path,
    legacy: Path,
    frozen_source_sha256: str = "0" * 64,
    attested: bool = True,
) -> Path:
    evidence_root = root / "evidence" / group_id
    evidence_root.mkdir(parents=True, exist_ok=True)
    source = evidence_root / "source.txt"
    source.write_text(source_text, encoding="utf-8", newline="\n")
    agent_bound = evidence_root / "agent.png"
    legacy_bound = evidence_root / "legacy.png"
    agent_bound.write_bytes(agent.read_bytes())
    legacy_bound.write_bytes(legacy.read_bytes())
    files = {
        "source_or_storyboard_binding": source,
        "character_identity_cards": evidence_root / "character-cards.json",
        "scene_master": evidence_root / "scene-master.json",
        "shot_inputs": evidence_root / "shot-inputs.json",
        "agent_candidate_image": agent_bound,
        "legacy_candidate_image": legacy_bound,
        "revision_trace": evidence_root / "revision-trace.json",
        "visual_agent_run": evidence_root / "visual-agent-run.json",
        "visual_event_log": evidence_root / "events.jsonl",
        "production_event_log": evidence_root / "production-events.jsonl",
        "visual_review_evidence": evidence_root / "visual-review.json",
        "cost_record_or_zero_cost_fixture_record": evidence_root / "cost.json",
    }
    for name, path in files.items():
        if name != "production_event_log" and not path.exists():
            _write_json(path, {"artifact": name, "group_id": group_id})
    run_id = f"run-{group_id}"
    quality_gate = {
        "automated_review_completed": True,
        "passed_without_override": True,
        "blocking_status": "clear",
        "overridden_blocking_issue_count": 0,
        "overridden_issue_ids": [],
    }
    state = {
        "run_id": run_id,
        "status": "completed",
        "stop_reason": "completed",
        "quality_gate": quality_gate,
    }
    event = new_event(1, "legacy_state_committed", {
        "run_id": run_id,
        "reason": "m8-test-fixture",
        "state_fingerprint": "fixture",
        "state": state,
    })
    files["visual_event_log"].write_text(event_json(event) + "\n", encoding="utf-8", newline="\n")
    _write_json(files["visual_agent_run"], {
        "run_id": run_id,
        "status": "completed",
        "stop_reason": "completed",
        "quality_gate": quality_gate,
        "event_store": {"event_sequence": 1, "event_checksum": event.checksum},
    })
    _write_json(files["visual_review_evidence"], {
        "run_id": run_id,
        "status": "completed",
        **quality_gate,
    })
    _write_json(files["cost_record_or_zero_cost_fixture_record"], {
        "run_id": run_id,
        "approved_paid_calls": 1,
        "used_paid_calls": 1,
        "actionable_uncertain_paid_jobs": [],
    })
    production_run_id = f"production-{group_id}"
    operation_id = f"operation-{group_id}"
    project_id = f"project-{story_id}"
    request = ApprovalRequest(
        request_id=f"approval-{group_id}",
        project_id=project_id,
        run_id=production_run_id,
        stage="visual",
        stage_run_id=f"visual-{group_id}",
        kind="paid_visual_batch",
        state_fingerprint=f"sha256:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()}",
        artifact_versions=(
            {"artifact_id": "storyboard", "version_id": f"sha256:{frozen_source_sha256}"},
            {
                "artifact_id": f"m8-source:{group_id}",
                "version_id": f"sha256:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()}",
            },
        ),
        operation_intents=({
            "operation_id": operation_id,
            "input_fingerprint": f"sha256:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()}",
            "kind": "image",
        },),
        maximum_paid_calls=1,
        maximum_amount="0",
        currency="USD",
        provider_profile="m8-real-provider-fixture",
        expires_at="2099-01-01T00:00:00Z",
    )
    production_store = EventStore(
        str(files["production_event_log"]),
        key_provider=MappingHmacKeyProvider({PRODUCTION_KEY_ID: PRODUCTION_KEY}),
    )
    production_store.append("project_initialized", project_id=project_id)
    production_store.append("run_created", project_id=project_id, run_id=production_run_id)
    production_store.append("run_started", project_id=project_id, run_id=production_run_id)
    production_store.append(
        "approval_requested", project_id=project_id, run_id=production_run_id,
        payload={"approval_request": request.to_dict(), "key_id": PRODUCTION_KEY_ID},
    )
    production_store.append(
        "approval_approved", project_id=project_id, run_id=production_run_id,
        payload={"request_id": request.request_id, "reviewer": "m8-test", "key_id": PRODUCTION_KEY_ID},
    )
    grant = Grant.issue(
        request,
        grant_id=f"grant-{group_id}",
        issued_by="m8-test",
        issued_at="2026-08-26T00:00:00Z",
        key_id=PRODUCTION_KEY_ID,
        key=PRODUCTION_KEY,
    )
    production_store.append(
        "grant_issued", project_id=project_id, run_id=production_run_id,
        payload={"grant": grant.to_dict(), "key_id": PRODUCTION_KEY_ID},
    )
    reserved = OperationRecord(
        operation_id, grant.grant_id, "image", request.operation_intents[0]["input_fingerprint"],
        request.provider_profile,
    )
    submitted = reserved.submit(f"job-{group_id}")
    settled = submitted.settle(
        outcome="succeeded",
        result_fingerprint=f"sha256:{hashlib.sha256(agent_bound.read_bytes()).hexdigest()}",
    )
    for event_type, operation in (
        ("call_reserved", reserved),
        ("call_submitted", submitted),
        ("call_settled", settled),
    ):
        production_store.append(
            event_type, project_id=project_id, run_id=production_run_id,
            payload={"operation": operation.to_dict(), "key_id": PRODUCTION_KEY_ID},
        )
    completed = production_store.append(
        "stage_completed", project_id=project_id, run_id=production_run_id,
        payload={"stage": "visual", "stage_run_id": request.stage_run_id},
    )
    if attested:
        production_store.append(
            "m8_visual_evidence_attested", project_id=project_id, run_id=production_run_id,
            payload={
                "group_id": group_id,
                "story_id": story_id,
                "frozen_source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "agent_run_id": run_id,
                "agent_candidate_sha256": hashlib.sha256(agent_bound.read_bytes()).hexdigest(),
                "evidence_artifact_sha256": dict(sorted({
                    name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in files.items()
                    if name != "production_event_log"
                }.items())),
                "stage_completed_event_hash": completed["event_hash"],
                "key_id": PRODUCTION_KEY_ID,
            },
        )
    artifacts = {
        name: {
            "path": path.relative_to(evidence_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for name, path in files.items()
    }
    evidence = evidence_root / "evidence.json"
    _write_json(evidence, {
        "schema_version": "m8-visual-pair-evidence-v1",
        "group_id": group_id,
        "story_id": story_id,
        "shared_input_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "frozen_source_sha256": frozen_source_sha256,
        "artifacts": artifacts,
        "agent_execution": {
            "run_id": run_id,
            "status": "completed",
            "stop_reason": "completed",
            "automated_review_completed": True,
            "passed_without_override": True,
            "manual_quality_override": False,
            "blocking_status": "clear",
        },
        "cost": {
            "run_id": run_id,
            "approved_paid_calls": 1,
            "used_paid_calls": 1,
            "actionable_uncertain_paid_jobs": [],
            "settled_or_zero_cost": True,
        },
        "production": {
            "project_id": project_id,
            "run_id": production_run_id,
            "operation_id": operation_id,
            "stage_run_id": request.stage_run_id,
            "hmac_key_id": PRODUCTION_KEY_ID,
            "stage": "visual",
        },
    })
    return evidence


def _source_manifest(tmp_path: Path) -> Path:
    root = tmp_path / "source-files"
    samples: list[dict[str, object]] = []
    for index in range(20):
        filename = f"story-{index + 1:02d}.txt"
        content = f"Story {index + 1}: two characters cross a room with a marked prop.\n"
        source = root / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content.encode("utf-8"))
        samples.append({
            "id": f"sample-{index + 1:02d}",
            "kind": "script" if index < 10 else "novel",
            "filename": filename,
            "source_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "bytes": source.stat().st_size,
            "expected_stage": "visual",
        })
    manifest = root / "source-manifest.json"
    _write_json(manifest, {"schema_version": "fixture-v1", "samples": samples})
    return manifest


def _pair_input(tmp_path: Path, count: int = 4) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pairs: list[dict[str, object]] = []
    for index in range(count):
        agent = tmp_path / f"agent-{index + 1:02d}.png"
        legacy = tmp_path / f"legacy-{index + 1:02d}.png"
        # A valid tiny image keeps the test independent of Pillow while still
        # exercising the standard-library metadata sanitizer.
        agent.write_bytes(TINY_PNG)
        legacy.write_bytes(TINY_PNG)
        source_text = f"A source frame with a prop, case {index + 1}."
        evidence = _pair_evidence(
            tmp_path,
            group_id=f"fixture-group-{index + 1:02d}",
            story_id=f"fixture-story-{index + 1:02d}",
            source_text=source_text,
            agent=agent,
            legacy=legacy,
        )
        evidence_root = evidence.parent
        pairs.append({
            "group_id": f"fixture-group-{index + 1:02d}",
            "story_id": f"fixture-story-{index + 1:02d}",
            "source_text": source_text,
            "agent_image": str(evidence_root / "agent.png"),
            "legacy_image": str(evidence_root / "legacy.png"),
            "evidence_file": str(evidence),
        })
    pair_file = tmp_path / "pairs.json"
    _write_json(pair_file, {"schema_version": "m8-visual-pair-input-v1", "pairs": pairs})
    return pair_file


def _reviews_for_package(tmp_path: Path, public_dir: Path, mapping_path: Path) -> list[Path]:
    manifest_path = public_dir / "manifest.json"
    public = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    group_ids = [item["group_id"] for item in public["groups"]]
    paths: list[Path] = []
    for index, perspective in enumerate(("content", "visual_production", "target_user"), 1):
        rows = []
        for group_id in group_ids:
            agent_side = "A" if mapping["groups"][group_id]["A"] == "agent" else "B"
            rows.append({
                "group_id": group_id,
                "scores": {
                    agent_side: {dimension: 5 for dimension in DIMENSIONS},
                    ("B" if agent_side == "A" else "A"): {
                        dimension: 3 for dimension in DIMENSIONS
                    },
                },
                "preference": agent_side,
                "serious_error": {"A": False, "B": False},
                "serious_error_codes": {"A": [], "B": []},
                "notes": {"A": "", "B": ""},
            })
        review = tmp_path / f"review-{index}.json"
        _write_json(review, {
            "schema_version": "m8-visual-review-v2",
            "reviewer_id": f"reviewer-{index}",
            "perspective": perspective,
            "materials_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "mapping_commitment_sha256": public["integrity"]["mapping_commitment_sha256"],
            "rows": rows,
        })
        paths.append(review)
    return paths


def _pairs_for_frozen_sample(tmp_path: Path, sample_path: Path) -> Path:
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    repository_root = sample_path.parent.parent
    source_by_story = {
        story["story_id"]: repository_root / story["source_ref"]
        for story in sample["stories"]
    }
    pairs: list[dict[str, object]] = []
    production_heads: dict[str, str] = {}
    image_root = tmp_path / "full-images"
    for index, group in enumerate(sample["scene_groups"], 1):
        agent = image_root / f"{index:03d}-agent.png"
        legacy = image_root / f"{index:03d}-legacy.png"
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_bytes(TINY_PNG)
        legacy.write_bytes(TINY_PNG)
        source_path = source_by_story[group["story_id"]]
        source_text = source_path.read_text(encoding="utf-8")
        evidence = _pair_evidence(
            tmp_path,
            group_id=group["group_id"],
            story_id=group["story_id"],
            source_text=source_text,
            agent=agent,
            legacy=legacy,
            frozen_source_sha256=group["source_binding"]["source_sha256"],
        )
        evidence_root = evidence.parent
        evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))
        production = evidence_payload["production"]
        production_log_item = evidence_payload["artifacts"]["production_event_log"]
        production_events = [
            json.loads(line)
            for line in (evidence_root / production_log_item["path"]).read_text(encoding="utf-8").splitlines()
        ]
        production_heads[f"{production['project_id']}/{production['run_id']}"] = production_events[-1]["event_hash"]
        pairs.append({
            "group_id": group["group_id"],
            "story_id": group["story_id"],
            "source_file": str(source_path),
            "agent_image": str(evidence_root / "agent.png"),
            "legacy_image": str(evidence_root / "legacy.png"),
            "evidence_file": str(evidence),
        })
    pair_file = tmp_path / "full-pairs.json"
    _write_json(pair_file, {"schema_version": "m8-visual-pair-input-v1", "pairs": pairs})
    os.environ["MANJU_M8_PRODUCTION_HEADS_JSON"] = json.dumps(production_heads, sort_keys=True)
    return pair_file


def test_contract_freezes_explicit_scope_and_offline_zero_budget() -> None:
    contract = build_contract()

    assert contract["schema_version"] == CONTRACT_SCHEMA_VERSION
    assert contract["status"] == "frozen"
    assert contract["baseline"] == {
        "commit": "d7191cd",
        "branch": "feat/m3.4.1-audit-baseline",
        "comparison_baseline": "legacy",
    }
    assert contract["scope"]["stories_minimum"] == 20
    assert contract["scope"]["scene_groups_minimum"] == 60
    assert set(contract["scoring"]["dimensions"]) == set(DIMENSIONS)
    assert contract["budget"]["offline_provider_calls"] == 0
    assert contract["budget"]["offline_paid_amount_minor"] == 0
    assert contract["visual_gate"]["preference_threshold"] == PREFERENCE_THRESHOLD
    assert len(contract["severe_errors"]["codes"]) == 6


def test_sample_manifest_is_bound_to_source_and_covers_m8(tmp_path: Path) -> None:
    source = _source_manifest(tmp_path)
    manifest = build_sample_manifest(source)
    result = validate_sample_manifest(manifest)

    assert manifest["schema_version"] == SAMPLE_MANIFEST_SCHEMA_VERSION
    assert result["story_count"] == 20
    assert result["scene_group_count"] == 60
    assert manifest["execution"]["provider_calls"] == 0
    assert manifest["execution"]["paid_amount_minor"] == 0
    assert set(manifest["coverage_counts"]) >= {
        "multi_character", "duplicate_name_identity", "wardrobe_continuity",
        "key_prop_continuity", "day_night_transition", "action_continuity",
        "complex_composition",
    }


def test_freeze_is_new_only_and_source_bound(tmp_path: Path) -> None:
    source = _source_manifest(tmp_path)
    contract_path = tmp_path / "frozen" / "contract.json"
    sample_path = tmp_path / "frozen" / "samples.json"

    result = freeze_m8(
        source_manifest_path=source,
        contract_output=contract_path,
        sample_output=sample_path,
    )
    assert result["story_count"] == 20
    assert result["scene_group_count"] == 60
    assert json.loads(contract_path.read_text(encoding="utf-8"))["status"] == "frozen"

    with pytest.raises(M8EvaluationError, match="refuses to overwrite"):
        freeze_m8(
            source_manifest_path=source,
            contract_output=contract_path,
            sample_output=sample_path,
        )


def test_sample_manifest_rejects_fingerprint_and_coverage_tampering(tmp_path: Path) -> None:
    manifest = build_sample_manifest(_source_manifest(tmp_path))
    manifest["scene_groups"][0]["source_binding"]["source_sha256"] = "0" * 64
    with pytest.raises(M8EvaluationError, match="fingerprint mismatch"):
        validate_sample_manifest(manifest)


def test_blind_materials_are_balanced_and_private_mapping_is_separate(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"

    result = generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=11,
    )
    assert result["provider_calls"] == 0
    assert result["a_count"] == result["b_count"] == 2
    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in public_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".txt"}
    ).casefold()
    assert '"agent"' not in public_text
    assert '"legacy"' not in public_text
    manifest = json.loads((public_dir / "manifest.json").read_text(encoding="utf-8"))
    assert all(set(item["options"]) == {"A", "B"} for item in manifest["groups"])
    assert len(manifest["integrity"]["mapping_commitment_sha256"]) == 64
    assert mapping_path.is_file()


def test_blind_materials_remove_png_text_metadata(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=1)
    pair = json.loads(pair_file.read_text(encoding="utf-8"))["pairs"][0]
    agent_path = Path(pair["agent_image"])
    metadata = b"engine\x00private-provider-name"
    chunk = (
        len(metadata).to_bytes(4, "big")
        + b"tEXt"
        + metadata
        + (zlib.crc32(b"tEXt" + metadata) & 0xffffffff).to_bytes(4, "big")
    )
    agent_path.write_bytes(TINY_PNG[:-12] + chunk + TINY_PNG[-12:])
    evidence_path = Path(pair["evidence_file"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["artifacts"]["agent_candidate_image"]["sha256"] = hashlib.sha256(agent_path.read_bytes()).hexdigest()
    evidence["artifacts"]["agent_candidate_image"]["bytes"] = agent_path.stat().st_size
    _write_json(evidence_path, evidence)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"

    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=6,
    )
    public_media = next((public_dir / "groups").rglob("image*.png"))
    assert b"private-provider-name" not in public_media.read_bytes()


def test_packaging_rejects_override_and_unregistered_public_files(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    pair = json.loads(pair_file.read_text(encoding="utf-8"))["pairs"][0]
    evidence_path = Path(pair["evidence_file"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["agent_execution"]["manual_quality_override"] = True
    _write_json(evidence_path, evidence)
    with pytest.raises(M8EvaluationError, match="no-override"):
        generate_blind_materials(
            pair_input_path=pair_file,
            public_output_dir=tmp_path / "rejected-public",
            private_mapping_output=tmp_path / "private" / "rejected.json",
            seed=1,
        )

    pair_file = _pair_input(tmp_path / "clean", count=2)
    public_dir = tmp_path / "clean-public"
    mapping_path = tmp_path / "private" / "clean.json"
    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=1,
    )
    (public_dir / "A_IS_AGENT.txt").write_text("leak", encoding="utf-8")
    with pytest.raises(M8EvaluationError, match="unregistered files"):
        aggregate_reviews(
            materials_manifest_path=public_dir / "manifest.json",
            private_mapping_path=mapping_path,
            review_paths=[tmp_path / "x", tmp_path / "y", tmp_path / "z"],
            output_path=tmp_path / "unused.json",
        )

    pair_file = _pair_input(tmp_path / "contradictory", count=2)
    pair = json.loads(pair_file.read_text(encoding="utf-8"))["pairs"][0]
    evidence_path = Path(pair["evidence_file"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    review_item = evidence["artifacts"]["visual_review_evidence"]
    review_path = evidence_path.parent / review_item["path"]
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["automated_review_completed"] = False
    review["blocking_status"] = "blocked"
    _write_json(review_path, review)
    review_item["sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    review_item["bytes"] = review_path.stat().st_size
    _write_json(evidence_path, evidence)
    with pytest.raises(M8EvaluationError, match="visual_review contradicts"):
        generate_blind_materials(
            pair_input_path=pair_file,
            public_output_dir=tmp_path / "contradictory-public",
            private_mapping_output=tmp_path / "private" / "contradictory.json",
            seed=1,
        )


def test_jpeg_and_webp_private_metadata_is_not_preserved() -> None:
    secret = b"M8-PRIVATE-PROVIDER-SECRET"
    jpeg = (
        b"\xff\xd8\xff\xe0"
        + (len(secret) + 2).to_bytes(2, "big")
        + secret
        + b"\xff\xda\x00\x02\x00\xff\xd9"
    )
    assert secret not in _sanitize_jpeg(jpeg)
    vp8 = b"VP8 \x01\x00\x00\x00\x00\x00"
    private = b"PRIV" + len(secret).to_bytes(4, "little") + secret + (b"\x00" if len(secret) & 1 else b"")
    body = b"WEBP" + vp8 + private
    with pytest.raises(M8EvaluationError, match="unsupported metadata chunk"):
        _sanitize_webp(b"RIFF" + len(body).to_bytes(4, "little") + body)


def test_aggregate_decodes_three_reviews_but_never_claims_release(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=3,
    )
    reviews = _reviews_for_package(tmp_path, public_dir, mapping_path)
    output = tmp_path / "summary.json"

    result = aggregate_reviews(
        materials_manifest_path=public_dir / "manifest.json",
        private_mapping_path=mapping_path,
        review_paths=reviews,
        output_path=output,
    )
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "visual_gate_incomplete"
    assert summary["status"] == "visual_gate_incomplete"
    assert summary["release_eligible"] is False
    assert summary["gates"]["preference"]["win_rate"] == 1.0
    assert summary["gates"]["source_fidelity_non_inferior"]["passed"] is True
    assert summary["gates"]["source_fidelity_non_inferior"]["agent_mean"] == 5
    assert summary["gates"]["source_fidelity_non_inferior"]["legacy_mean"] == 3


def test_full_scope_binds_public_materials_to_contract(tmp_path: Path) -> None:
    source = _source_manifest(tmp_path)
    contract_path = tmp_path / "frozen" / "contract.json"
    sample_path = tmp_path / "frozen" / "samples.json"
    freeze_m8(
        source_manifest_path=source,
        contract_output=contract_path,
        sample_output=sample_path,
    )
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    package_result = generate_blind_materials(
        pair_input_path=_pairs_for_frozen_sample(tmp_path, sample_path),
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        sample_manifest_path=sample_path,
    )
    public = json.loads((public_dir / "manifest.json").read_text(encoding="utf-8"))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert public["group_count"] == 60
    assert public["integrity"]["contract_fingerprint"] == contract["fingerprint"]
    assert package_result["a_count"] == package_result["b_count"] == 30
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    groups = {item["group_id"]: item for item in sample["scene_groups"]}
    coverage_sides: dict[str, dict[str, int]] = {}
    for item in mapping["groups"].values():
        agent_side = "A" if item["A"] == "agent" else "B"
        for code in groups[item["source_group_id"]]["coverage_codes"]:
            coverage_sides.setdefault(code, {"A": 0, "B": 0})[agent_side] += 1
    assert all(abs(counts["A"] - counts["B"]) <= 1 for counts in coverage_sides.values())
    reviews = _reviews_for_package(tmp_path, public_dir, mapping_path)
    result = aggregate_reviews(
        materials_manifest_path=public_dir / "manifest.json",
        private_mapping_path=mapping_path,
        review_paths=reviews,
        output_path=tmp_path / "full-summary.json",
        contract_path=contract_path,
        sample_manifest_path=sample_path,
    )
    assert result["status"] == "visual_gate_passed"
    assert result["group_count"] == 60
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["baseline"]["commit"] = "attacker-changed"
    _write_json(contract_path, contract)
    with pytest.raises(M8EvaluationError, match="contract fingerprint mismatch"):
        aggregate_reviews(
            materials_manifest_path=public_dir / "manifest.json",
            private_mapping_path=mapping_path,
            review_paths=reviews,
            output_path=tmp_path / "tampered-contract-summary.json",
            contract_path=contract_path,
            sample_manifest_path=sample_path,
        )


def test_attest_pair_appends_final_hmac_witness_and_updates_log_binding(tmp_path: Path) -> None:
    agent = tmp_path / "agent.png"
    legacy = tmp_path / "legacy.png"
    agent.write_bytes(TINY_PNG)
    legacy.write_bytes(TINY_PNG)
    source_text = "A frozen M8 source."
    evidence_path = _pair_evidence(
        tmp_path,
        group_id="fixture-group-attest",
        story_id="fixture-story-attest",
        source_text=source_text,
        agent=agent,
        legacy=legacy,
        frozen_source_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        attested=False,
    )
    result = attest_pair_evidence(evidence_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    item = evidence["artifacts"]["production_event_log"]
    log_path = evidence_path.parent / item["path"]
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "m8_visual_evidence_attested"
    assert result["event_hash"] == events[-1]["event_hash"]
    assert item["sha256"] == hashlib.sha256(log_path.read_bytes()).hexdigest()
    repeated = attest_pair_evidence(evidence_path)
    assert repeated["event_hash"] == result["event_hash"]
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == len(events)


def test_formal_pair_requires_trusted_production_key_and_rejects_event_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_file = _pair_input(tmp_path, count=1)
    pair = evaluation_module._load_pair_input(pair_file)[0]
    source_bytes = str(pair["source_text"]).encode("utf-8")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    evidence_path = Path(pair["evidence_file"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["frozen_source_sha256"] = source_sha
    _write_json(evidence_path, evidence)
    expected = {
        "story_id": pair["story_id"],
        "source_binding": {"source_sha256": source_sha},
    }
    monkeypatch.delenv("MANJU_PRODUCTION_HMAC_KEY")
    with pytest.raises(M8EvaluationError, match="requires MANJU_PRODUCTION_HMAC_KEY"):
        evaluation_module._validate_pair_evidence(
            pair,
            source_bytes=source_bytes,
            agent_path=Path(pair["agent_image"]),
            legacy_path=Path(pair["legacy_image"]),
            expected_frozen_group=expected,
        )

    monkeypatch.setenv("MANJU_PRODUCTION_HMAC_KEY", PRODUCTION_KEY.decode("ascii"))
    production_item = evidence["artifacts"]["production_event_log"]
    production_path = evidence_path.parent / production_item["path"]
    lines = production_path.read_text(encoding="utf-8").splitlines()
    production = evidence["production"]
    head_key = f"{production['project_id']}/{production['run_id']}"
    current_head = json.loads(lines[-1])["event_hash"]
    monkeypatch.setenv("MANJU_M8_PRODUCTION_HEADS_JSON", json.dumps({head_key: current_head}))
    evaluation_module._validate_pair_evidence(
        pair,
        source_bytes=source_bytes,
        agent_path=Path(pair["agent_image"]),
        legacy_path=Path(pair["legacy_image"]),
        expected_frozen_group=expected,
    )
    monkeypatch.setenv("MANJU_M8_PRODUCTION_HEADS_JSON", json.dumps({head_key: "0" * 64}))
    with pytest.raises(M8EvaluationError, match="signed ProductionRun provenance is invalid"):
        evaluation_module._validate_pair_evidence(
            pair,
            source_bytes=source_bytes,
            agent_path=Path(pair["agent_image"]),
            legacy_path=Path(pair["legacy_image"]),
            expected_frozen_group=expected,
        )
    monkeypatch.setenv("MANJU_M8_PRODUCTION_HEADS_JSON", json.dumps({head_key: current_head}))
    changed = json.loads(lines[4])
    changed["hmac"] = "0" * 64
    lines[4] = json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    production_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    production_item["sha256"] = hashlib.sha256(production_path.read_bytes()).hexdigest()
    production_item["bytes"] = production_path.stat().st_size
    _write_json(evidence_path, evidence)
    with pytest.raises(M8EvaluationError, match="signed ProductionRun provenance is invalid"):
        evaluation_module._validate_pair_evidence(
            pair,
            source_bytes=source_bytes,
            agent_path=Path(pair["agent_image"]),
            legacy_path=Path(pair["legacy_image"]),
            expected_frozen_group=expected,
        )


def test_full_scope_all_unusable_cannot_pass_relative_noninferiority(tmp_path: Path) -> None:
    source = _source_manifest(tmp_path)
    contract_path = tmp_path / "frozen" / "contract.json"
    sample_path = tmp_path / "frozen" / "samples.json"
    freeze_m8(
        source_manifest_path=source,
        contract_output=contract_path,
        sample_output=sample_path,
    )
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    generate_blind_materials(
        pair_input_path=_pairs_for_frozen_sample(tmp_path, sample_path),
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        sample_manifest_path=sample_path,
    )
    reviews = _reviews_for_package(tmp_path, public_dir, mapping_path)
    for review_path in reviews:
        review = json.loads(review_path.read_text(encoding="utf-8"))
        for row in review["rows"]:
            row["scores"] = {
                side: {dimension: 1 for dimension in DIMENSIONS}
                for side in ("A", "B")
            }
            row["serious_error"] = {"A": True, "B": True}
            row["serious_error_codes"] = {
                "A": ["unusable_artifact_or_multi_panel_output"],
                "B": ["unusable_artifact_or_multi_panel_output"],
            }
            row["notes"] = {
                "A": "The declared shot is visibly unusable.",
                "B": "The declared shot is visibly unusable.",
            }
        _write_json(review_path, review)
    result = aggregate_reviews(
        materials_manifest_path=public_dir / "manifest.json",
        private_mapping_path=mapping_path,
        review_paths=reviews,
        output_path=tmp_path / "failed-summary.json",
        contract_path=contract_path,
        sample_manifest_path=sample_path,
    )
    summary = json.loads((tmp_path / "failed-summary.json").read_text(encoding="utf-8"))
    assert result["status"] == "visual_gate_failed"
    assert summary["gates"]["absolute_quality_floor"]["passed"] is False
    assert summary["gates"]["absolute_severe_error_limit"]["passed"] is False


def test_mapping_publish_failure_never_exposes_ready_public_package(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    public_dir = tmp_path / "public"
    with pytest.raises((M8EvaluationError, OSError)):
        generate_blind_materials(
            pair_input_path=pair_file,
            public_output_dir=public_dir,
            private_mapping_output=blocked_parent / "mapping.json",
            seed=2,
        )
    assert not public_dir.exists()


def test_pair_image_symlink_is_rejected_before_resolution(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    payload = json.loads(pair_file.read_text(encoding="utf-8"))
    original = Path(payload["pairs"][0]["agent_image"])
    link = tmp_path / "agent-link.png"
    try:
        link.symlink_to(original)
    except OSError:
        pytest.skip("this host does not permit creating symlinks")
    payload["pairs"][0]["agent_image"] = str(link)
    _write_json(pair_file, payload)
    with pytest.raises(M8EvaluationError, match="link or reparse point"):
        generate_blind_materials(
            pair_input_path=pair_file,
            public_output_dir=tmp_path / "public",
            private_mapping_output=tmp_path / "private" / "mapping.json",
            seed=2,
        )


def test_pair_input_rejects_reparse_parent_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    payload = json.loads(pair_file.read_text(encoding="utf-8"))
    blocked_parent = Path(payload["pairs"][0]["agent_image"]).parent
    original = evaluation_module._is_link_or_reparse
    monkeypatch.setattr(
        evaluation_module,
        "_is_link_or_reparse",
        lambda path: path == blocked_parent or original(path),
    )
    with pytest.raises(M8EvaluationError, match="link or reparse point"):
        generate_blind_materials(
            pair_input_path=pair_file,
            public_output_dir=tmp_path / "public",
            private_mapping_output=tmp_path / "private" / "mapping.json",
            seed=2,
        )


def test_attest_pair_rejects_reparse_parent_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pair_file = _pair_input(tmp_path, count=1)
    evidence_path = Path(json.loads(pair_file.read_text(encoding="utf-8"))["pairs"][0]["evidence_file"])
    blocked_parent = evidence_path.parent
    original = evaluation_module._is_link_or_reparse
    monkeypatch.setattr(
        evaluation_module,
        "_is_link_or_reparse",
        lambda path: path == blocked_parent or original(path),
    )
    with pytest.raises(M8EvaluationError, match="link or reparse point"):
        attest_pair_evidence(evidence_path)


def test_aggregate_rejects_tampered_public_media(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=4,
    )
    media = next((public_dir / "groups").rglob("image*.png"))
    media.write_bytes(media.read_bytes() + b"tampered")

    with pytest.raises(M8EvaluationError, match="integrity failed"):
        aggregate_reviews(
            materials_manifest_path=public_dir / "manifest.json",
            private_mapping_path=mapping_path,
            review_paths=[tmp_path / "unused-1.json", tmp_path / "unused-2.json", tmp_path / "unused-3.json"],
            output_path=tmp_path / "summary.json",
        )


def test_review_requires_explained_severe_error(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=5,
    )
    reviews = _reviews_for_package(tmp_path, public_dir, mapping_path)
    review = json.loads(reviews[0].read_text(encoding="utf-8"))
    review["rows"][0]["serious_error"]["A"] = True
    review["rows"][0]["serious_error_codes"]["A"] = ["source_fact_invented_or_omitted"]
    _write_json(reviews[0], review)

    with pytest.raises(M8EvaluationError, match="concrete note"):
        aggregate_reviews(
            materials_manifest_path=public_dir / "manifest.json",
            private_mapping_path=mapping_path,
            review_paths=reviews,
            output_path=tmp_path / "summary.json",
        )


def test_aggregate_rejects_mapping_changed_after_publication(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=4,
    )
    reviews = _reviews_for_package(tmp_path, public_dir, mapping_path)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    first = next(iter(mapping["groups"].values()))
    first["A"], first["B"] = first["B"], first["A"]
    _write_json(mapping_path, mapping)

    with pytest.raises(M8EvaluationError, match="public commitment"):
        aggregate_reviews(
            materials_manifest_path=public_dir / "manifest.json",
            private_mapping_path=mapping_path,
            review_paths=reviews,
            output_path=tmp_path / "summary.json",
        )


def test_reviews_are_bound_to_the_manifest_the_reviewer_saw(tmp_path: Path) -> None:
    pair_file = _pair_input(tmp_path, count=2)
    public_dir = tmp_path / "reviewer-materials"
    mapping_path = tmp_path / "private" / "mapping.json"
    generate_blind_materials(
        pair_input_path=pair_file,
        public_output_dir=public_dir,
        private_mapping_output=mapping_path,
        seed=9,
    )
    reviews = _reviews_for_package(tmp_path, public_dir, mapping_path)
    manifest_path = public_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["note"] += " changed after review"
    _write_json(manifest_path, manifest)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["public_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _write_json(mapping_path, mapping)
    with pytest.raises(M8EvaluationError, match="reviewed material manifest"):
        aggregate_reviews(
            materials_manifest_path=manifest_path,
            private_mapping_path=mapping_path,
            review_paths=reviews,
            output_path=tmp_path / "summary.json",
        )
