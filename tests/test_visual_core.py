from __future__ import annotations

import copy
import json
import os
from unittest import mock

import pytest

from manju.pipeline.visual.approvals import (
    DecisionValidationError,
    decision_template,
    is_placeholder_review_text,
    validate_common_decision,
)
from manju.pipeline.visual.commands import (
    STAGE_COMMANDS,
    assert_command_allowed,
    next_visual_command,
    recommended_visual_command,
)
from manju.pipeline.visual.constraints import (
    ConstraintPriority,
    VisualConstraint,
    compile_fallback_constraints,
    compile_shot_constraints,
    detect_constraint_conflicts,
    fallback_constraint_envelope,
    prioritize_reference_assets,
    prompt_constraint_envelope,
)
from manju.pipeline.visual.calibration import (
    calibrate_verdict,
    calibration_report,
    effective_confidence,
)
from manju.pipeline.visual.escalation import build_constraint_isolation_tasks
from manju.pipeline.visual.events import event_from_dict, new_event
from manju.pipeline.visual.identity import (
    compatibility_report,
    create_run_identity,
    identity_from_dict,
    invocation_contract_hash,
)
from manju.pipeline.visual.ownership import FACT_OWNERSHIP, ownership_manifest
from manju.pipeline.visual.reducer import reduce_visual_state, replay_visual_events
from manju.pipeline.visual.review import (
    ConstraintVerdict,
    blocking_verdict_is_actionable,
    normalize_issue_verdict,
)
from manju.pipeline.visual.store import VisualEventStore, recover_current_state


def _identity(run_id: str = "run-a"):
    return create_run_identity(
        {"storyboard": "abc", "budget": 2},
        run_id=run_id,
        created_at="2026-08-03T00:00:00+08:00",
    )


def _state(run_id: str = "run-a") -> dict:
    return {
        "run_id": run_id,
        "status": "running",
        "stage": "new",
        "counters": {"model_calls": 0, "image_calls": 0, "vision_calls": 0},
    }


def test_each_durable_fact_has_exactly_one_authoritative_owner() -> None:
    facts = [item.fact for item in FACT_OWNERSHIP]
    assert len(facts) == len(set(facts))
    assert all(item.owner and not item.recovery_reads_projection for item in FACT_OWNERSHIP)
    manifest = ownership_manifest()
    assert manifest["rule"].startswith("one authoritative owner")


@pytest.mark.parametrize("stage,command", sorted(STAGE_COMMANDS.items()))
def test_normal_stage_routing_is_code_owned(stage: str, command: str) -> None:
    state = {"status": "running", "stage": stage}
    assert next_visual_command(state) == command
    assert recommended_visual_command(state) == command
    assert_command_allowed(state, command)
    with pytest.raises(ValueError, match="invalid"):
        assert_command_allowed(state, "stop_needs_review")


def test_nonrunning_and_unknown_states_do_not_produce_runnable_commands() -> None:
    assert next_visual_command({"status": "awaiting_approval", "stage": "planned"}) == ""
    assert next_visual_command({"status": "running", "stage": "unknown"}) == ""
    assert recommended_visual_command({"status": "failed", "stage": "unknown"}) == "stop_needs_review"


def test_run_identity_is_immutable_and_contract_compatibility_is_separate() -> None:
    contract = {"storyboard": "one", "fingerprint": "derived", "created_at": "ignored"}
    identity = create_run_identity(
        contract, run_id="stable-id", created_at="fixed", run_kind="migration"
    )
    assert identity_from_dict(identity.to_dict()) == identity
    assert compatibility_report(identity, {**contract, "created_at": "changed"})["compatible"]
    report = compatibility_report(identity, {**contract, "storyboard": "two"})
    assert not report["compatible"]
    assert report["run_id"] == "stable-id"
    assert report["run_identity_unchanged"] is True
    assert invocation_contract_hash(contract) == identity.invocation_contract_hash


def test_event_checksum_sequence_chain_and_tamper_detection() -> None:
    first = new_event(
        1, "run_created", {"run_id": "r"},
        event_id="event-1", created_at="fixed",
    )
    second = new_event(
        2, "status_changed", {"run_id": "r", "status": "completed"},
        previous_checksum=first.checksum, event_id="event-2", created_at="fixed",
    )
    assert replay_visual_events([first, second])["status"] == "completed"
    tampered = second.to_dict()
    tampered["payload"]["status"] = "failed"
    with pytest.raises(ValueError, match="checksum mismatch"):
        event_from_dict(tampered)
    gap = new_event(
        3, "status_changed", {"run_id": "r", "status": "failed"},
        previous_checksum=first.checksum, event_id="event-3", created_at="fixed",
    )
    with pytest.raises(ValueError, match="sequence gap"):
        replay_visual_events([first, gap])


def test_reducer_is_pure_and_replay_is_deterministic() -> None:
    original = {"run_id": "r", "status": "running", "stage": "new", "nested": {"x": 1}}
    before = copy.deepcopy(original)
    event = new_event(
        1, "stage_changed", {"run_id": "r", "stage": "inspected"},
        event_id="event-1", created_at="fixed",
    )
    reduced = reduce_visual_state(original, event)
    assert original == before
    assert reduced["stage"] == "inspected"
    created = new_event(
        1, "run_created", {"run_id": "r"},
        event_id="created", created_at="fixed",
    )
    changed = new_event(
        2, "stage_changed", {"run_id": "r", "stage": "inspected"},
        previous_checksum=created.checksum, event_id="changed", created_at="fixed",
    )
    assert replay_visual_events([created, changed]) == replay_visual_events([created, changed])


def test_event_store_commit_recover_idempotency_and_current_pointer(tmp_path) -> None:
    store = VisualEventStore(str(tmp_path), "run-a")
    identity = _identity()
    store.write_identity(identity)
    state = _state()
    first = store.commit_state(state, reason="bootstrap")
    assert first is not None and first.sequence == 1
    assert store.commit_state(state, reason="unchanged") is None
    recovered = store.recover_state()
    assert recovered is not None
    assert recovered["run_id"] == "run-a"
    assert recovered["event_sequence"] == 1
    assert recover_current_state(str(tmp_path)) == recovered
    assert store.commit_state(recovered, reason="recovered-but-unchanged") is None


def test_event_store_recovery_does_not_require_snapshot(tmp_path) -> None:
    store = VisualEventStore(str(tmp_path), "run-a")
    store.commit_state(_state(), reason="bootstrap")
    os.unlink(store.snapshot_path)
    assert store.recover_state()["stage"] == "new"


def test_event_store_atomic_append_failure_preserves_old_log(tmp_path) -> None:
    store = VisualEventStore(str(tmp_path), "run-a")
    store.commit_state(_state(), reason="bootstrap")
    old_body = open(store.event_path, encoding="utf-8").read()
    real_replace = os.replace

    def fail_event_replace(source: str, target: str) -> None:
        if os.path.abspath(target) == os.path.abspath(store.event_path):
            raise OSError("injected replace failure")
        real_replace(source, target)

    with mock.patch("manju.pipeline.visual.store.os.replace", side_effect=fail_event_replace):
        with pytest.raises(OSError, match="injected"):
            store.commit_state({**_state(), "stage": "inspected"}, reason="changed")
    assert open(store.event_path, encoding="utf-8").read() == old_body
    assert store.recover_state()["stage"] == "new"


def test_event_store_snapshot_failure_keeps_new_event_recoverable(tmp_path) -> None:
    store = VisualEventStore(str(tmp_path), "run-a")
    store.commit_state(_state(), reason="bootstrap")
    real_replace = os.replace

    def fail_snapshot_replace(source: str, target: str) -> None:
        if os.path.abspath(target) == os.path.abspath(store.snapshot_path):
            raise OSError("injected snapshot failure")
        real_replace(source, target)

    with mock.patch("manju.pipeline.visual.store.os.replace", side_effect=fail_snapshot_replace):
        with pytest.raises(OSError, match="snapshot"):
            store.commit_state({**_state(), "stage": "inspected"}, reason="changed")
    assert store.recover_state()["stage"] == "inspected"


def test_event_store_rejects_identity_overwrite(tmp_path) -> None:
    store = VisualEventStore(str(tmp_path), "run-a")
    store.write_identity(_identity())
    incompatible = create_run_identity(
        {"storyboard": "different"}, run_id="run-a", created_at="later"
    )
    with pytest.raises(ValueError, match="immutable"):
        store.write_identity(incompatible)


def test_constraint_compilation_orders_hard_contract_before_narrative() -> None:
    foundation = {
        "prop-a": {
            "asset_type": "key_prop",
            "reference_contract": {
                "scale_contract": {
                    "required": True,
                    "source_cues": ["known dimension"],
                    "shot_policy": "preserve physical scale",
                }
            },
        }
    }
    shot = {
        "shot_id": "s1",
        "visible_character_ids": ["char-a"],
        "visible_prop_ids": ["prop-a"],
        "reference_asset_ids": ["prop-a"],
        "description": "required action",
        "prompt": "wide cinematic composition",
    }
    constraints = compile_shot_constraints(shot, foundation)
    priorities = [int(item.priority) for item in constraints]
    assert priorities == sorted(priorities, reverse=True)
    scale = next(item for item in constraints if item.attribute == "physical_scale")
    narrative = next(item for item in constraints if item.attribute == "narrative_composition_context")
    assert scale.hard and not narrative.hard
    envelope = prompt_constraint_envelope(constraints)
    final_prompt = envelope + "\nNARRATIVE PROMPT: " + shot["prompt"]
    assert final_prompt.index("physical_scale") < final_prompt.rindex("NARRATIVE PROMPT")


def test_conflicting_hard_constraints_fail_preflight() -> None:
    common = {
        "subject": "asset",
        "attribute": "physical_scale",
        "priority": ConstraintPriority.PHYSICAL_SCALE,
        "source": "test",
        "hard": True,
        "asset_ids": ("asset",),
    }
    constraints = [
        VisualConstraint(constraint_id="a", value="small", **common),
        VisualConstraint(constraint_id="b", value="large", **common),
    ]
    conflicts = detect_constraint_conflicts(constraints)
    assert conflicts[0]["severity"] == "blocking"
    assert set(conflicts[0]["constraint_ids"]) == {"a", "b"}


def test_reference_assets_prioritize_issue_focus_then_contract_strength() -> None:
    constraints = [
        VisualConstraint(
            "id", "a", "identity", "preserve", ConstraintPriority.IDENTITY,
            "test", True, ("identity",),
        ),
        VisualConstraint(
            "style", "a", "style", "preserve", ConstraintPriority.STYLE,
            "test", False, ("focus",),
        ),
    ]
    ordered = prioritize_reference_assets(
        ["other", "identity", "focus"], constraints, focus_asset_ids=["focus"]
    )
    assert ordered == ["focus", "identity", "other"]


def test_escalation_selects_one_evidence_backed_priority_blocker_per_shot() -> None:
    def verdict(confidence: float = 0.9) -> dict:
        return {
            "verdict": "fail", "confidence": confidence,
            "evidence": [{"image_path": "shot.png", "problem": "visible mismatch"}],
        }

    issues = [{
        "issue_id": "effect", "shot_id": "s1", "blocking": True,
        "correction_target": "effect_alignment", "focus_asset_ids": ["prop"],
        "constraint_verdict": verdict(0.99),
    }, {
        "issue_id": "scale", "shot_id": "s1", "blocking": True,
        "correction_target": "prop_geometry", "focus_asset_ids": ["prop"],
        "constraint_verdict": verdict(0.8),
    }, {
        "issue_id": "unverifiable", "shot_id": "s2", "blocking": True,
        "correction_target": "character_identity", "focus_asset_ids": ["identity"],
        "constraint_verdict": {
            "verdict": "unverifiable", "confidence": 0.99, "evidence": [],
        },
    }]

    tasks = build_constraint_isolation_tasks(issues, scale_asset_ids={"prop"})

    assert len(tasks) == 1
    assert tasks[0]["active_issue_id"] == "scale"
    assert tasks[0]["deferred_issue_ids"] == ["effect"]
    assert "measurable_relative_size" in tasks[0]["required_provider_capabilities"]


def test_confidence_calibration_requires_samples_and_reports_ece() -> None:
    insufficient = calibration_report(
        [{"confidence": 0.9, "correct": index % 2 == 0} for index in range(49)]
    )
    assert insufficient["status"] == "uncalibrated"
    assert effective_confidence(0.9, insufficient) == pytest.approx(0.9)

    profile = calibration_report(
        [{"confidence": 0.9, "correct": index % 2 == 0} for index in range(50)]
    )
    assert profile["status"] == "calibrated"
    assert profile["expected_calibration_error"] == pytest.approx(0.4)
    calibrated = calibrate_verdict({"verdict": "fail", "confidence": 0.9}, profile)
    assert calibrated["raw_confidence"] == pytest.approx(0.9)
    assert calibrated["confidence"] == pytest.approx(0.5)
    assert calibrated["calibration_applied"] is True


def test_fallback_expression_keeps_hard_facts_and_hard_conflicts_closed() -> None:
    hard = VisualConstraint(
        "scale", "asset", "physical_scale", {"source_cues": ["known dimension"]},
        ConstraintPriority.PHYSICAL_SCALE, "source", True, ("asset",),
    )
    soft = VisualConstraint(
        "composition", "shot", "composition", "large foreground hero object",
        ConstraintPriority.COMPOSITION, "prompt", False,
    )
    plan = compile_fallback_constraints([hard, soft], trigger="provider_escalation")
    assert plan["status"] == "ready"
    assert plan["hard_facts_unchanged"] is True
    assert plan["invariant_hard_facts"][0]["value"] == hard.value
    assert plan["degraded_expression"][0]["original_value"] == hard.value
    assert plan["relaxable_soft_context"][0]["constraint_id"] == "composition"
    assert "HARD FACTS REMAIN UNCHANGED" in fallback_constraint_envelope(
        [hard, soft], trigger="provider_escalation"
    )

    conflict = VisualConstraint(
        "scale-b", "asset", "physical_scale", "different",
        ConstraintPriority.PHYSICAL_SCALE, "source", True, ("asset",),
    )
    blocked = compile_fallback_constraints([hard, conflict], trigger="provider_escalation")
    assert blocked["status"] == "blocked_hard_conflict"
    assert blocked["hard_conflicts"]


def test_review_fail_requires_evidence_and_unverifiable_is_not_actionable() -> None:
    with pytest.raises(ValueError, match="requires evidence"):
        ConstraintVerdict("c", "fail", (), 0.9, "vision", {})
    unverifiable = normalize_issue_verdict({"blocking": True, "problem": "claim only"})
    assert unverifiable["verdict"] == "unverifiable"
    assert not blocking_verdict_is_actionable(unverifiable)
    legacy = normalize_issue_verdict({
        "blocking": True,
        "evidence_valid": True,
        "image_path": "shot.png",
        "problem": "visible mismatch",
    })
    assert blocking_verdict_is_actionable(legacy)


def test_approval_template_prefills_review_contract_and_errors_are_addressable() -> None:
    request = {
        "request_id": "req-1",
        "state_fingerprint": "state-1",
        "item_ids": ["one", "two"],
        "reviewed_image_fingerprints": {"one": "sha-one"},
        "allowed_decisions": ["approve", "reject"],
        "issues": [{"issue_id": "issue-1", "blocking": True}],
    }
    decision = decision_template(request)
    assert decision["reviewed_item_ids"] == ["one", "two"]
    assert decision["reviewed_image_fingerprints"] == {"one": "sha-one"}
    assert decision["issue_override_reasons"] == {"issue-1": ""}
    decision["decision"] = "approve"
    decision["reviewer"] = ""
    with pytest.raises(DecisionValidationError) as caught:
        validate_common_decision(request, decision, "approve")
    assert caught.value.issues[0].path == "$.reviewer"
    assert caught.value.issues[0].example
    assert is_placeholder_review_text("automatic")
    assert not is_placeholder_review_text("QA Reviewer")
