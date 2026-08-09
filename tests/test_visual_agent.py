import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import ssl
from unittest.mock import patch

from PIL import Image
from click.testing import CliRunner

from manju.knowledge.production_playbook import get_playbook_sections, load_production_playbook
from manju.cli import cli
from manju.pipeline.storyboard_schema import normalize_storyboard, validate_storyboard
from manju.pipeline.visual_agent import (
    _apply_repair_convergence_gate,
    _apply_pending_decision,
    _apply_scene_convergence_gate,
    _authorization_snapshot,
    _backfill_foundation_reference_contracts,
    _correction_contract,
    _declared_scale_contract,
    _is_placeholder_review_text,
    _load_paid_ledger,
    _lock_candidate,
    _normalize_shot_canvas,
    _normalize_recoverable_paid_state,
    _normalize_visual_issues,
    _next_contract_attempt_number,
    _paid_job_finalization_payload,
    _paid_tool_trace_accounting,
    _persist_artifacts,
    _prepare_post_foundation_reset_transfer,
    _prepare_post_transfer_scale_evidence_reconstruction,
    _close_blocked_post_transfer_scale_evidence_reconstruction,
    _publish_paid_output,
    _record_shot_dimensions,
    _prepare_foundation_reference_reset,
    _reconcile_durable_progress,
    _reconcile_revision_attempt_history,
    _register_approval_grant,
    _run_paid_image_jobs,
    _save_paid_ledger,
    _asset_prompt,
    _default_vision_provider,
    _build_foundation_assets,
    _build_inventory,
    _revision_provider_references,
    _revision_attempt_summary,
    _resume_invocation_contract,
    _stabilize_new_blockers_on_unchanged_images,
    _storyboard_asset_preflight,
    _shot_prompt,
    _tool_inspect_scene_group,
    _tool_generate_foundation_candidates,
    _tool_request_foundation_lock,
    _tool_request_manual_review,
    _tool_request_scene_group_approval,
    _tool_revise_scene_group,
    _tool_request_foundation_approval,
    _validate_human_decision,
    _visual_invocation_lease,
    reconcile_paid_artifacts,
    reconcile_visual_metadata,
    prepare_provider_escalation,
    run_image_agent,
)


def _storyboard(two_shots=False):
    shots = [{
        "shot_id": "1.1", "duration_seconds": 3,
        "visual": {
            "shot_type": "近景", "composition": "中置", "composition_emotion": "紧张",
            "camera_movement": "固定", "description": "阿宁看向门口", "color_tone": "冷色",
            "visible_character_ids": ["c1"],
        },
        "audio": {"speaker": "", "dialogue": "", "narration": "", "sound_music": ""},
        "prompts": {"image_cn": "阿宁看向门口", "image_en": "Ani looks at the door", "video": ""},
    }]
    if two_shots:
        shots.append({
            "shot_id": "1.2", "duration_seconds": 3,
            "visual": {
                "shot_type": "全景", "composition": "纵深", "composition_emotion": "警觉",
                "camera_movement": "固定", "description": "阿宁退后一步", "color_tone": "冷色",
                "visible_character_ids": ["c1"],
            },
            "audio": {"speaker": "", "dialogue": "", "narration": "", "sound_music": ""},
            "prompts": {"image_cn": "阿宁退后", "image_en": "Ani steps back", "video": ""},
        })
    return normalize_storyboard({
        "title": "视觉测试", "creative_bible": {
            "style_anchor": "cinematic graphic novel", "aspect_ratio": "9:16",
            "characters": [{
                "character_id": "c1", "name": "阿宁", "role": "主角",
                "anchor_description": "短黑发，深色外套",
            }],
        },
        "scenes": [{
            "scene_id": "1", "heading": "INT. 旧房间 - 夜", "purpose": "发现异常",
            "visual_mood": "紧张", "continuity": {}, "shots": shots,
        }],
    })


def _supervisor(snapshot):
    return {"action": snapshot["recommended_action"], "params": {}, "summary": "mock decision"}


class MockImageProvider:
    def __init__(self, fail_call=0):
        self.calls = []
        self.fail_call = fail_call

    def __call__(self, prompt, output_path, references, size):
        self.calls.append({"path": output_path, "references": list(references), "prompt": prompt})
        if self.fail_call and len(self.calls) == self.fail_call:
            raise OSError("simulated provider interruption")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Image.new("RGB", (32, 32), (30, 60, 120)).save(output_path)
        return output_path


class ConcurrencyImageProvider(MockImageProvider):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, prompt, output_path, references, size):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.03)
            return super().__call__(prompt, output_path, references, size)
        finally:
            with self.lock:
                self.active -= 1


class MockVisionProvider:
    def __init__(self, blocking_once=False, advisory=False, unavailable_review=False):
        self.blocking_once = blocking_once
        self.advisory = advisory
        self.unavailable_review = unavailable_review
        self.review_calls = 0

    def __call__(self, task, paths, context):
        if task == "rank_foundation_candidates":
            return {"ranking": context["candidate_ids"], "summary": "ranked"}
        self.review_calls += 1
        if self.unavailable_review:
            return None
        if (self.blocking_once or self.advisory) and self.review_calls == 1:
            shot = context["group"]["shots"][0]
            return {"issues": [{
                "issue_id": "issue_1", "shot_id": shot["shot_id"],
                "category": "identity", "severity": "major",
                "blocking": not self.advisory, "problem": "identity differs",
                "instruction": "match the locked identity",
                "storyboard_path": shot["storyboard_path"],
                "reference_asset_ids": [context["group"]["reference_asset_ids"][0]],
                "image_path": context["generated_paths"][shot["shot_id"]],
            }]}
        return {"issues": []}


class RepeatedDuplicateBlockingVision:
    def __call__(self, task, paths, context):
        if task == "rank_foundation_candidates":
            return {"ranking": context["candidate_ids"], "summary": "ranked"}
        issues = []
        for index, shot in enumerate(context["group"]["shots"], 1):
            issues.append({
                "issue_id": "storyboard_execution",
                "shot_id": shot["shot_id"],
                "category": "storyboard_execution",
                "severity": "major",
                "blocking": True,
                "problem": f"visible relationship {index} is incorrect",
                "instruction": f"correct visible relationship {index}",
                "storyboard_path": shot["storyboard_path"],
                "reference_asset_ids": [context["group"]["reference_asset_ids"][0]],
                "image_path": context["generated_paths"][shot["shot_id"]],
            })
        return {"issues": issues}


class VisualAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output_dir = self.temp.name
        self.storyboard_path = os.path.join(self.output_dir, "storyboard.json")
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(_storyboard(), handle, ensure_ascii=False)

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, image, vision, **kwargs):
        return run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=_supervisor, image_provider=image, vision_provider=vision,
            **kwargs,
        )

    def _read_json(self, path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _approve(self, manifest, override_reason=""):
        pending = manifest["pending_approval"]
        request_path = os.path.join(self.output_dir, pending["request_path"])
        decision_path = os.path.join(self.output_dir, pending["decision_path"])
        with open(request_path, encoding="utf-8") as handle:
            request = json.load(handle)
        with open(decision_path, encoding="utf-8") as handle:
            decision = json.load(handle)
        decision["decision"] = "approve"
        decision["override_reason"] = override_reason
        decision["reviewer"] = "Mock Human Reviewer"
        decision["reviewed_item_ids"] = request["item_ids"]
        decision["reviewed_image_fingerprints"] = request.get("reviewed_image_fingerprints", {})
        if request["stage"].startswith("manual_review_"):
            decision["issue_override_reasons"] = {
                str(issue["issue_id"]): (
                    f"Human reviewed the evidence for {issue['issue_id']} and accepts this specific deviation."
                )
                for issue in request.get("issues", [])
                if isinstance(issue, dict) and issue.get("blocking") is True and issue.get("issue_id")
            }
        if request["stage"].startswith("foundation_lock_"):
            decision["change_note"] = "Reviewed all current candidate images and selected the closest match."
            decision["selections"] = {
                asset_id: detail["candidates"][0]["candidate_id"]
                for asset_id, detail in request["candidate_summary"].items()
            }
            decision["reference_contract_checks"] = {
                asset_id: {
                    "candidate_id": decision["selections"][asset_id],
                    "single_object": True,
                    "single_view": True,
                    "clean_background": True,
                    "no_grid_or_state_sequence": True,
                    **({
                        "scale_evidence_present": True,
                        "scale_relation_matches": True,
                        "scale_comparator_complete": True,
                        "scale_comparator_in_focus": True,
                        "scale_comparator_contact_or_shared_plane": True,
                    } if contract.get("scale_contract", {}).get("required") else {}),
                }
                for asset_id, contract in request.get("reference_contracts", {}).items()
                if contract.get("role") == "canonical_geometry_anchor"
            }
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle, ensure_ascii=False, indent=2)

    def _drive(self, image, vision, **kwargs):
        for _ in range(20):
            manifest = self._run(image, vision, **kwargs)
            if manifest["status"] == "completed":
                return manifest
            self.assertEqual(manifest["status"], "awaiting_approval", manifest)
            reason = "human accepts semantic evidence" if manifest["stop_reason"].startswith("manual_review_") else ""
            self._approve(manifest, reason)
        self.fail("visual agent did not complete")

    def test_playbook_contains_curated_levels_without_private_source(self):
        playbook = load_production_playbook()
        self.assertEqual(playbook["version"], "1.0")
        self.assertTrue(get_playbook_sections(["foundation"], ["hard_gate"]))
        distributed = json.dumps(playbook["sections"], ensure_ascii=False).lower()
        self.assertNotIn("接单渠道", distributed)
        self.assertNotIn("source_path", distributed)

    def test_planning_and_approval_make_zero_paid_calls(self):
        image = MockImageProvider()
        manifest = self._run(image, MockVisionProvider())
        self.assertEqual(manifest["status"], "awaiting_approval")
        self.assertEqual(manifest["stop_reason"], "foundation_cost")
        self.assertEqual(image.calls, [])
        self.assertEqual(manifest["counters"]["image_calls"], 0)
        cost_plan = self._read_json(os.path.join(self.output_dir, "cost_plan.json"))
        self.assertEqual(cost_plan["current_pending_approval_stage"], "foundation_cost")
        self.assertTrue(cost_plan["current_pending_approval_is_paid_cost_gate"])
        self.assertGreater(cost_plan["current_pending_approval_maximum_paid_calls"], 0)
        self.assertEqual(cost_plan["current_stage_paid_calls_actionable_now"], 0)
        self.assertFalse(cost_plan["unused_grant_calls_are_provider_quota"])

    def test_default_supervisor_path_is_deterministic_and_spends_no_model_call(self):
        image = MockImageProvider()
        manifest = run_image_agent(
            self.storyboard_path,
            self.output_dir,
            execute_paid_calls=False,
            foundation_candidates=1,
            image_provider=image,
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(manifest["status"], "awaiting_approval")
        self.assertEqual(manifest["pending_approval"]["stage"], "foundation_cost")
        self.assertEqual(manifest["counters"]["model_calls"], 0)
        self.assertEqual(image.calls, [])
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            trace = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(
            [item["payload"]["action"] for item in trace if item["event"] == "deterministic_route"],
            ["inspect_storyboard", "build_visual_bible", "request_foundation_approval"],
        )

    def test_event_store_rebuilds_deleted_compatibility_projections_without_api_calls(self):
        image = MockImageProvider()
        vision = MockVisionProvider()
        completed = self._drive(image, vision, foundation_candidates=1)
        before = dict(completed["counters"])
        run_id = completed["run_id"]
        event_path = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", run_id, "events.jsonl"
        )
        self.assertTrue(os.path.isfile(event_path))
        for relative in (
            os.path.join("stages", "visual_agent", "runs", run_id, "state.json"),
            "visual_agent_run.json", "visual_review.json", "cost_plan.json",
            "visual_plan.json", "visual_repair_plan.json",
        ):
            os.unlink(os.path.join(self.output_dir, relative))
        rebuild = reconcile_paid_artifacts(self.storyboard_path, self.output_dir)
        self.assertEqual(rebuild["status"], "completed")
        self.assertEqual(rebuild["run_id"], run_id)
        self.assertEqual(rebuild["model_calls_made"], 0)
        self.assertEqual(rebuild["vision_calls_made"], 0)
        self.assertEqual(rebuild["image_calls_made"], 0)
        recovered = self._read_json(os.path.join(self.output_dir, "visual_agent_run.json"))
        self.assertEqual(recovered["counters"], before)
        self.assertTrue(os.path.isfile(os.path.join(self.output_dir, "visual_review.json")))
        self.assertEqual(recovered["_projection"]["authoritative_source"], "visual_event_store")

    def test_incompatible_invocation_creates_new_uuid_without_mutating_old_event_log(self):
        first = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            foundation_candidates=1, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        old_event_path = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", first["run_id"], "events.jsonl"
        )
        with open(old_event_path, "rb") as handle:
            old_events = handle.read()
        second = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            foundation_candidates=1, max_calls=41,
            image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
        )
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(len(second["run_id"]), 32)
        self.assertNotEqual(
            second["run_id"],
            second["run_identity"]["invocation_contract_hash"],
        )
        with open(old_event_path, "rb") as handle:
            self.assertEqual(handle.read(), old_events)

    def test_default_three_candidates_complete_and_attach_storyboard(self):
        image = MockImageProvider()
        manifest = self._drive(image, MockVisionProvider())
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["toolset_version"], "4.0.0-rc2")
        self.assertEqual(manifest["counters"]["image_calls"], 16)
        self.assertEqual(manifest["quality_gate"]["quality_outcome"], "passed")
        self.assertTrue(manifest["quality_gate"]["passed_without_override"])
        with open(self.storyboard_path, encoding="utf-8") as handle:
            storyboard = json.load(handle)
        self.assertEqual(validate_storyboard(storyboard), [])
        self.assertEqual(storyboard["scenes"][0]["shots"][0]["status"]["image"], "completed")
        image_path = os.path.join(
            self.output_dir, storyboard["scenes"][0]["shots"][0]["assets"]["image"]
        )
        with Image.open(image_path) as generated:
            self.assertAlmostEqual(generated.width / generated.height, 9 / 16, delta=0.02)

    def test_blocking_issue_revises_only_target_shot_once(self):
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(_storyboard(two_shots=True), handle, ensure_ascii=False)
        image = MockImageProvider()
        vision = MockVisionProvider(blocking_once=True)
        manifest = self._drive(image, vision, foundation_candidates=1)
        self.assertEqual(manifest["counters"]["image_calls"], 8)  # 5 foundation + 2 first pass + 1 retry
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        self.assertEqual(review["scene_groups"]["scene_1"]["retry_count"], 1)
        retry_paths = [call["path"] for call in image.calls if "retry" in call["path"]]
        self.assertEqual(len(retry_paths), 1)
        self.assertIn("1.1", retry_paths[0])

    def test_advisory_issue_does_not_consume_retry(self):
        image = MockImageProvider()
        manifest = self._drive(image, MockVisionProvider(advisory=True), foundation_candidates=1)
        self.assertEqual(manifest["counters"]["image_calls"], 6)
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        self.assertEqual(review["scene_groups"]["scene_1"]["retry_count"], 0)

    def test_vision_unavailable_pauses_for_human_override(self):
        image = MockImageProvider()
        vision = MockVisionProvider(unavailable_review=True)
        manifest = self._drive(image, vision, foundation_candidates=1)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["stop_reason"], "completed_with_manual_override")
        self.assertEqual(manifest["quality_gate"]["mode"], "human_override")
        self.assertEqual(manifest["quality_gate"]["verification_mode"], "human_only")
        self.assertEqual(manifest["quality_gate"]["automated_review_status"], "unavailable")
        self.assertFalse(manifest["quality_gate"]["automated_review_completed"])
        self.assertEqual(manifest["quality_gate"]["blocking_status"], "unknown")
        self.assertIsNone(manifest["quality_gate"]["blocking_issue_count"])
        self.assertTrue(manifest["quality_gate"]["unverified_checks"])
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        self.assertIn("human_override", review["scene_groups"]["scene_1"])
        self.assertEqual(review["verification_mode"], "human_only")
        self.assertTrue(review["unverified_checks"])

    def test_vision_blockers_overridden_by_human_are_not_reported_as_human_only(self):
        image = MockImageProvider()
        vision = MockVisionProvider(blocking_once=True)
        manifest = self._drive(
            image, vision, foundation_candidates=1, max_auto_retries=0,
        )
        self.assertEqual(manifest["quality_gate"]["verification_mode"], "vision_with_human_override")
        self.assertTrue(manifest["quality_gate"]["vision_override_group_ids"])
        self.assertEqual(manifest["quality_gate"]["human_only_group_ids"], [])
        self.assertEqual(len(manifest["quality_gate"]["overridden_issue_ids"]), 1)
        self.assertTrue(manifest["quality_gate"]["overridden_issue_ids"][0].startswith("visual_scene_1_"))
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        self.assertEqual(review["issues"][0]["provider_issue_id"], "issue_1")
        self.assertEqual(manifest["quality_gate"]["unverified_checks"], [])
        self.assertEqual(manifest["quality_gate"]["quality_outcome"], "overridden")
        self.assertFalse(manifest["quality_gate"]["passed_without_override"])
        self.assertEqual(manifest["quality_gate"]["blocking_issue_count"], 1)
        self.assertEqual(manifest["quality_gate"]["overridden_blocking_issue_count"], 1)

    def test_transient_vision_ssl_failure_retries_then_succeeds(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                body = json.dumps({"issues": []})
                return json.dumps({
                    "choices": [{"message": {"content": body}}],
                }).encode("utf-8")

        with patch("manju.pipeline.visual_agent._vision_config", return_value={
            "api_base": "https://example.invalid/v1", "api_key": "mock-key",
            "model": "mock-vision", "max_attempts": 3,
        }), patch(
            "manju.pipeline.visual_agent.urllib.request.urlopen",
            side_effect=[ssl.SSLEOFError("transient EOF"), Response()],
        ) as mocked, patch("manju.pipeline.visual_agent.time.sleep") as sleeper:
            result = _default_vision_provider("review_scene_group", [], {})

        self.assertEqual(result["issues"], [])
        self.assertEqual(result["_manju_vision_meta"]["attempts"], 2)
        self.assertEqual(len(result["_manju_vision_meta"]["failures"]), 1)
        self.assertEqual(mocked.call_count, 2)
        sleeper.assert_called_once()

    def test_nonretryable_vision_auth_failure_stops_immediately(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 401, "unauthorized", {}, None,
        )
        with patch("manju.pipeline.visual_agent._vision_config", return_value={
            "api_base": "https://example.invalid/v1", "api_key": "mock-key",
            "model": "mock-vision", "max_attempts": 3,
        }), patch(
            "manju.pipeline.visual_agent.urllib.request.urlopen", side_effect=error,
        ) as mocked, patch("manju.pipeline.visual_agent.time.sleep") as sleeper:
            result = _default_vision_provider("review_scene_group", [], {})

        self.assertTrue(result["_manju_vision_unavailable"])
        self.assertEqual(result["_manju_vision_meta"]["attempts"], 1)
        self.assertFalse(result["_manju_vision_meta"]["failures"][0]["retryable"])
        self.assertEqual(mocked.call_count, 1)
        sleeper.assert_not_called()

    def test_vision_only_recheck_reuses_images_without_paid_calls(self):
        image = MockImageProvider()
        first = self._drive(
            image, MockVisionProvider(unavailable_review=True), foundation_candidates=1,
        )
        image_calls_before = first["counters"]["image_calls"]
        self.assertEqual(first["quality_gate"]["quality_outcome"], "overridden")

        with tempfile.TemporaryDirectory() as relocated_root:
            relocated_output = os.path.join(relocated_root, "copied_visual_output")
            shutil.copytree(self.output_dir, relocated_output)
            relocated_storyboard = os.path.join(relocated_output, "storyboard.json")
            rechecked = run_image_agent(
                relocated_storyboard, relocated_output,
                execute_paid_calls=False, recheck_vision=True,
                foundation_candidates=1, supervisor_provider=_supervisor,
                image_provider=image, vision_provider=MockVisionProvider(),
            )

            self.assertEqual(rechecked["status"], "completed")
            self.assertEqual(rechecked["stop_reason"], "completed")
            self.assertEqual(rechecked["quality_gate"]["quality_outcome"], "passed")
            self.assertTrue(rechecked["quality_gate"]["automated_review_completed"])
            self.assertEqual(rechecked["counters"]["image_calls"], image_calls_before)
            self.assertNotEqual(rechecked["run_id"], first["run_id"])
            self.assertEqual(len(rechecked["verification_history"]), 1)

    def test_vision_only_blockers_are_aggregated_without_fake_approval(self):
        image = MockImageProvider()
        first = self._drive(
            image, MockVisionProvider(unavailable_review=True), foundation_candidates=1,
        )
        image_calls_before = first["counters"]["image_calls"]

        blocked = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(blocking_once=True),
        )

        self.assertEqual(blocked["status"], "needs_review")
        self.assertEqual(blocked["stop_reason"], "vision_recheck_blocked")
        self.assertEqual(blocked["pending_approval"], {})
        self.assertEqual(blocked["quality_gate"]["quality_outcome"], "blocked")
        self.assertEqual(blocked["quality_gate"]["automated_review_status"], "completed")
        self.assertTrue(blocked["quality_gate"]["automated_review_completed"])
        self.assertEqual(blocked["quality_gate"]["blocking_status"], "blocked")
        self.assertEqual(blocked["quality_gate"]["blocking_issue_count"], 1)
        self.assertEqual(blocked["counters"]["image_calls"], image_calls_before)
        self.assertEqual(blocked["repair_plan"]["status"], "proposed")
        self.assertEqual(blocked["repair_plan"]["shot_ids"], ["1.1"])
        self.assertEqual(blocked["repair_plan"]["maximum_paid_calls"], 1)
        repair_plan = self._read_json(os.path.join(self.output_dir, "visual_repair_plan.json"))
        self.assertEqual(repair_plan["source_run_id"], blocked["run_id"])

        repeated = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(repeated["stop_reason"], "completed")
        self.assertNotEqual(repeated["run_id"], blocked["run_id"])
        self.assertEqual(repeated["counters"]["image_calls"], image_calls_before)

    def test_vision_only_recheck_scans_all_groups_before_blocking(self):
        storyboard = _storyboard()
        second_scene = json.loads(json.dumps(storyboard["scenes"][0], ensure_ascii=False))
        second_scene["scene_id"] = "2"
        second_scene["heading"] = "INT. 另一房间 - 夜"
        second_scene["shots"][0]["shot_id"] = "2.1"
        storyboard["scenes"].append(second_scene)
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(normalize_storyboard(storyboard), handle, ensure_ascii=False)

        image = MockImageProvider()
        first = self._drive(
            image, MockVisionProvider(unavailable_review=True), foundation_candidates=1,
        )
        vision = MockVisionProvider(blocking_once=True)
        blocked = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=vision,
        )

        self.assertEqual(blocked["status"], "needs_review")
        self.assertEqual(vision.review_calls, 2)
        self.assertEqual(blocked["counters"]["image_calls"], first["counters"]["image_calls"])
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        self.assertEqual(review["scene_groups"]["scene_1"]["status"], "blocked")
        self.assertEqual(review["scene_groups"]["scene_2"]["status"], "accepted")
        self.assertEqual(review["repair_plan"]["group_ids"], ["scene_1"])

    def test_new_vision_recheck_uses_run_scoped_budget_after_cumulative_limit(self):
        image = MockImageProvider()
        first = self._drive(image, MockVisionProvider(), foundation_candidates=1)
        state_path = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", first["run_id"], "state.json"
        )
        state = self._read_json(state_path)
        state["counters"]["model_calls"] = state["budgets"]["effective_max_calls"]
        state["counters"]["tool_steps"] = state["budgets"]["effective_max_steps"]
        state["run_budget_usage"] = {
            "model_calls": state["counters"]["model_calls"],
            "tool_steps": state["counters"]["tool_steps"],
        }
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)

        rechecked = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertEqual(rechecked["status"], "completed")
        self.assertEqual(rechecked["counters"]["image_calls"], first["counters"]["image_calls"])
        self.assertGreater(
            rechecked["counters"]["model_calls"], rechecked["budgets"]["effective_max_calls"]
        )
        self.assertLess(
            rechecked["run_budget_usage"]["model_calls"],
            rechecked["budgets"]["effective_max_calls"],
        )
        self.assertLess(
            rechecked["run_budget_usage"]["tool_steps"],
            rechecked["budgets"]["effective_max_steps"],
        )

    def test_incomplete_recheck_has_structured_plan_and_new_run_accepts_new_budget(self):
        image = MockImageProvider()
        first = self._drive(image, MockVisionProvider(), foundation_candidates=1)
        interrupted = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, max_calls=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertEqual(interrupted["status"], "needs_review")
        self.assertEqual(interrupted["stop_reason"], "model_budget_exhausted")
        self.assertEqual(interrupted["budgets"]["effective_max_calls"], 1)
        self.assertEqual(interrupted["run_budget_usage"]["model_calls"], 1)
        self.assertEqual(interrupted["repair_plan"]["status"], "verification_incomplete")
        self.assertFalse(interrupted["repair_plan"]["requires_new_paid_grant"])
        plan = self._read_json(os.path.join(self.output_dir, "visual_repair_plan.json"))
        self.assertEqual(plan["status"], "verification_incomplete")
        self.assertEqual(plan["incomplete_reason"], "model_budget_exhausted")

        recovered = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, max_calls=4, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertNotEqual(recovered["run_id"], interrupted["run_id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["budgets"]["effective_max_calls"], 4)
        self.assertEqual(recovered["counters"]["image_calls"], first["counters"]["image_calls"])

    def test_explicit_vision_repair_uses_new_grant_and_only_target_shots(self):
        image = MockImageProvider()
        first = self._drive(
            image, MockVisionProvider(unavailable_review=True), foundation_candidates=1,
        )
        blocked = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(blocking_once=True),
        )
        self.assertEqual(blocked["stop_reason"], "vision_recheck_blocked")

        approval = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, repair_vision_blockers=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertEqual(approval["status"], "awaiting_approval")
        self.assertEqual(approval["stop_reason"], "scene_group_cost_scene_1")
        self.assertTrue(approval["vision_repair_mode"])
        self.assertEqual(approval["repair_source_run_id"], blocked["run_id"])
        self.assertEqual(approval["pending_approval"]["maximum_paid_calls"], 1)
        self.assertEqual(approval["pending_approval"]["item_ids"], ["1.1"])
        self.assertEqual(approval["counters"]["image_calls"], first["counters"]["image_calls"])

        self._approve(approval)
        repaired = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=True, repair_vision_blockers=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertEqual(repaired["status"], "completed")
        self.assertEqual(repaired["counters"]["image_calls"], first["counters"]["image_calls"] + 1)
        self.assertEqual(repaired["repair_plan"]["status"], "completed")
        state = self._read_json(os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", repaired["run_id"], "state.json"
        ))
        ledger = _load_paid_ledger(state)
        repair_grant = state["group_states"]["scene_1"]["grant_id"]
        self.assertEqual(ledger["grants"][repair_grant]["maximum_paid_calls"], 1)
        self.assertEqual(ledger["grants"][repair_grant]["used_calls"], 1)
        self.assertNotIn(repair_grant, ledger["historical_grant_ids"])
        self.assertTrue(ledger["historical_grant_ids"])
        cost_plan = self._read_json(os.path.join(self.output_dir, "cost_plan.json"))
        self.assertEqual(cost_plan["approved_paid_calls"], 1)
        self.assertEqual(cost_plan["used_paid_calls"], 1)
        self.assertGreater(cost_plan["historical_paid_calls"], 0)

    def test_repair_blockers_replan_without_manual_override_and_keep_unique_issue_ids(self):
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(_storyboard(two_shots=True), handle, ensure_ascii=False)
        image = MockImageProvider()
        first = self._drive(
            image, MockVisionProvider(unavailable_review=True), foundation_candidates=1,
        )
        blocked = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=RepeatedDuplicateBlockingVision(),
        )
        self.assertEqual(blocked["stop_reason"], "vision_recheck_blocked")
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        issue_ids = [issue["issue_id"] for issue in review["issues"] if issue["blocking"]]
        self.assertEqual(len(issue_ids), 2)
        self.assertEqual(len(set(issue_ids)), 2)
        self.assertEqual(
            {issue["provider_issue_id"] for issue in review["issues"] if issue["blocking"]},
            {"storyboard_execution"},
        )

        approval = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, repair_vision_blockers=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=RepeatedDuplicateBlockingVision(),
        )
        self.assertEqual(approval["pending_approval"]["maximum_paid_calls"], 2)
        source_review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        previous_generated = dict(source_review["scene_groups"]["scene_1"]["generated"])
        self._approve(approval)
        repaired = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=True, repair_vision_blockers=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=RepeatedDuplicateBlockingVision(),
        )

        self.assertEqual(repaired["status"], "needs_review")
        self.assertEqual(repaired["stop_reason"], "vision_repair_blocked")
        self.assertEqual(repaired["pending_approval"], {})
        self.assertEqual(repaired["quality_gate"]["blocking_issue_count"], 2)
        self.assertEqual(repaired["quality_gate"]["automated_review_status"], "completed")
        self.assertTrue(repaired["quality_gate"]["automated_review_completed"])
        self.assertEqual(repaired["repair_plan"]["status"], "proposed")
        self.assertEqual(repaired["repair_plan"]["shot_ids"], ["1.1", "1.2"])
        self.assertEqual(repaired["repair_plan"]["image_calls_before"], first["counters"]["image_calls"] + 2)
        self.assertTrue(repaired["repair_history"])
        self.assertEqual(repaired["repair_history"][-1]["status"], "reviewed")

        retry_calls = [call for call in image.calls if "retry" in call["path"]][-2:]
        repaired_review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        self.assertEqual(len(retry_calls), 2)
        for call in retry_calls:
            self.assertIn("EDIT the primary previous-shot reference", call["prompt"])
            self.assertNotIn("non-glyph ink bands", call["prompt"])
            board_manifest = self._read_json(call["references"][0] + ".manju.json")
            shot_id = "1.1" if "1.1" in call["path"] else "1.2"
            self.assertEqual(board_manifest["board_role"], "targeted_revision")
            self.assertEqual(board_manifest["primary_shot_reference"], previous_generated[shot_id])
            self.assertTrue(board_manifest["temporal_context"])
            self.assertNotEqual(board_manifest["temporal_context"][0]["shot_id"], shot_id)
            published = repaired_review["scene_groups"]["scene_1"]["generated"][shot_id]
            image_metadata = self._read_json(
                os.path.join(self.output_dir, published + ".manju.json")
            )
            production = image_metadata["production"]
            self.assertEqual(production["previous_shot_reference_role"], "primary_edit_reference")
            self.assertEqual(production["primary_reference_role"], "previous_shot_edit")
            self.assertEqual(
                production["reference_strategy"]["primary_role"], "previous_shot_edit"
            )
            self.assertTrue(production["previous_shot_reference_path"])
            self.assertTrue(production["revision_reference_board"])
            self.assertTrue(production["temporal_context_shot_ids"])
            self.assertTrue(production["temporal_context_paths"])

        next_approval = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, repair_vision_blockers=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=RepeatedDuplicateBlockingVision(),
        )
        self.assertNotEqual(next_approval["run_id"], repaired["run_id"])
        self.assertEqual(next_approval["status"], "awaiting_approval")
        self.assertEqual(next_approval["pending_approval"]["maximum_paid_calls"], 2)
        image_calls_before_migration = next_approval["counters"]["image_calls"]
        migrated = run_image_agent(
            self.storyboard_path, self.output_dir,
            execute_paid_calls=False, recheck_vision=True,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=RepeatedDuplicateBlockingVision(),
        )
        self.assertEqual(migrated["stop_reason"], "vision_recheck_blocked")
        self.assertEqual(migrated["counters"]["image_calls"], image_calls_before_migration)
        self.assertEqual(migrated["pending_approval"], {})
        self.assertEqual(migrated["repair_plan"]["shot_ids"], ["1.1", "1.2"])
        self.assertEqual(migrated["repair_history"][-1]["status"], "superseded_by_recheck")

    def test_multi_reference_revision_places_previous_shot_first(self):
        previous = os.path.join(self.output_dir, "previous.png")
        context = os.path.join(self.output_dir, "context.png")
        locked = os.path.join(self.output_dir, "locked.png")
        Image.new("RGB", (32, 32), "red").save(previous)
        Image.new("RGB", (32, 32), "green").save(context)
        Image.new("RGB", (32, 32), "blue").save(locked)
        state = {
            "output_dir": self.output_dir,
            "run_id": "run",
            "provider_capabilities": {"reference_mode": "multi", "max_references": 3},
            "locked_assets": {"asset_1": {"path": "locked.png"}},
        }
        references, metadata = _revision_provider_references(
            state, ["asset_1"], "revision", previous,
            [{"group_id": "scene_1", "shot_id": "1.0", "path": context}],
        )
        self.assertEqual(references, [previous, context, locked])
        self.assertEqual(metadata["previous_shot_reference_role"], "primary_edit_reference")
        self.assertEqual(metadata["temporal_context_shot_ids"], ["1.0"])

    def test_prop_geometry_revision_places_canonical_prop_before_failed_shot(self):
        previous = os.path.join(self.output_dir, "previous.png")
        location = os.path.join(self.output_dir, "location.png")
        prop = os.path.join(self.output_dir, "prop.png")
        Image.new("RGB", (32, 32), "red").save(previous)
        Image.new("RGB", (32, 32), "green").save(location)
        Image.new("RGB", (32, 32), "blue").save(prop)
        state = {
            "output_dir": self.output_dir,
            "run_id": "run",
            "provider_capabilities": {"reference_mode": "multi", "max_references": 3},
            "foundation_assets": [
                {"asset_id": "location_1", "asset_type": "location_master", "spec": {}},
                {"asset_id": "prop_p1", "asset_type": "key_prop", "spec": {"prop_id": "p1"}},
            ],
            "locked_assets": {
                "location_1": {"path": "location.png"},
                "prop_p1": {"path": "prop.png"},
            },
        }
        references, metadata = _revision_provider_references(
            state,
            ["location_1", "prop_p1"],
            "revision",
            previous,
            shot={"visible_prop_ids": ["p1"]},
            issues=[{
                "category": "storyboard_execution",
                "correction_target": "prop_geometry",
                "focus_asset_ids": ["prop_p1"],
            }],
        )
        self.assertEqual(references, [prop, previous, location])
        self.assertEqual(metadata["primary_reference_role"], "canonical_prop_geometry")
        self.assertEqual(metadata["primary_reference_asset_ids"], ["prop_p1"])
        self.assertEqual(metadata["previous_shot_reference_role"], "edit_target_reference")

        repeated_multi, repeated_multi_metadata = _revision_provider_references(
            state,
            ["location_1", "prop_p1"],
            "revision_multi_repeated",
            previous,
            [{"group_id": "scene_1", "shot_id": "1.0", "path": location}],
            shot={"visible_prop_ids": ["p1"]},
            issues=[{
                "category": "storyboard_execution",
                "correction_target": "prop_geometry",
                "focus_asset_ids": ["prop_p1"],
            }],
            revision_attempt_number=2,
        )
        self.assertEqual(repeated_multi[0], prop)
        self.assertIn(previous, repeated_multi)
        self.assertFalse(
            repeated_multi_metadata["reference_strategy"]["canonical_only_reference"]
        )

        state["provider_capabilities"] = {"reference_mode": "single", "max_references": 1}
        references, metadata = _revision_provider_references(
            state,
            ["location_1", "prop_p1"],
            "revision_single",
            previous,
            shot={"visible_prop_ids": ["p1"]},
            issues=[{
                "category": "storyboard_execution",
                "correction_target": "prop_geometry",
                "focus_asset_ids": ["prop_p1"],
            }],
        )
        board_manifest = self._read_json(references[0] + ".manju.json")
        self.assertEqual(board_manifest["primary_reference"], "prop.png")
        self.assertEqual(board_manifest["primary_reference_role"], "canonical_prop_geometry")
        self.assertEqual(board_manifest["edit_target_shot_reference"], "previous.png")
        self.assertEqual(metadata["primary_reference_role"], "canonical_prop_geometry")

        repeated_single, repeated_single_metadata = _revision_provider_references(
            state,
            ["location_1", "prop_p1"],
            "revision_single_repeated",
            previous,
            [{"group_id": "scene_1", "shot_id": "1.0", "path": location}],
            shot={"visible_prop_ids": ["p1"]},
            issues=[{
                "category": "storyboard_execution",
                "correction_target": "prop_geometry",
                "focus_asset_ids": ["prop_p1"],
            }],
            revision_attempt_number=2,
        )
        self.assertEqual(repeated_single, [prop])
        self.assertNotEqual(
            hashlib.sha256(Path(repeated_single[0]).read_bytes()).hexdigest(),
            hashlib.sha256(Path(previous).read_bytes()).hexdigest(),
        )
        self.assertEqual(
            repeated_single_metadata["primary_reference_role"], "clean_regeneration"
        )
        self.assertEqual(
            repeated_single_metadata["previous_shot_reference_role"],
            "excluded_nonconverging_source",
        )
        self.assertEqual(
            repeated_single_metadata["provider_reference_mode"],
            "canonical_locked_asset_only",
        )
        self.assertEqual(repeated_single_metadata["revision_reference_board"], "")
        self.assertEqual(
            repeated_single_metadata["reference_strategy"]["revision_attempt_number"], 2
        )
        self.assertTrue(
            repeated_single_metadata["reference_strategy"]["canonical_only_reference"]
        )
        self.assertNotIn(
            "previous.png", repeated_single_metadata["provider_reference_paths"]
        )
        durable_metadata = _paid_job_finalization_payload(repeated_single_metadata)
        self.assertEqual(
            durable_metadata["provider_reference_mode"], "canonical_locked_asset_only"
        )
        self.assertEqual(durable_metadata["provider_reference_paths"], ["prop.png"])
        self.assertIn("previous.png", durable_metadata["excluded_image_reference_paths"])
        _record_shot_dimensions(previous, {"method": "test"}, durable_metadata)
        recorded = self._read_json(previous + ".manju.json")["production"]
        self.assertEqual(recorded["provider_reference_mode"], "canonical_locked_asset_only")
        self.assertEqual(recorded["provider_reference_paths"], ["prop.png"])
        self.assertIn("previous.png", recorded["excluded_image_reference_paths"])

    def test_unchanged_image_new_blocker_requires_targeted_confirmation(self):
        image_path = os.path.join(self.output_dir, "shot.png")
        Image.new("RGB", (32, 32), "red").save(image_path)
        relative = os.path.relpath(image_path, self.output_dir)
        state = {
            "output_dir": self.output_dir,
            "run_id": "run",
            "counters": {},
            "locked_assets": {},
        }
        group = {
            "group_id": "scene_1",
            "reference_asset_ids": [],
            "shots": [{"shot_id": "1.1", "storyboard_path": "$.scenes[0].shots[0]"}],
        }
        generated = {"1.1": relative}
        fingerprint = hashlib.sha256(Path(image_path).read_bytes()).hexdigest()
        group_state = {"review_history": [{
            "image_fingerprints": {"1.1": fingerprint},
            "issues": [],
            "vision_available": True,
        }]}
        issue = {
            "issue_id": "new_issue", "group_id": "scene_1", "shot_id": "1.1",
            "category": "location_assets", "severity": "major", "blocking": True,
            "problem": "newly reported problem", "instruction": "correct it",
            "storyboard_path": "$.scenes[0].shots[0]", "reference_asset_ids": [],
            "focus_asset_ids": [], "correction_target": "location_structure",
            "image_path": relative, "evidence_valid": True,
        }
        stabilized = _stabilize_new_blockers_on_unchanged_images(
            state, group, generated, [issue], group_state,
            lambda _task, _paths, _context: {"issues": []},
        )
        self.assertFalse(stabilized[0]["blocking"])
        self.assertEqual(stabilized[0]["review_confirmation_status"], "not_confirmed")
        self.assertEqual(state["counters"]["vision_calls"], 1)

    def test_repeated_artifact_revision_escalates_then_excludes_failed_image(self):
        previous = os.path.join(self.output_dir, "failed.png")
        prop = os.path.join(self.output_dir, "prop.png")
        location = os.path.join(self.output_dir, "location.png")
        Image.new("RGB", (32, 48), "red").save(previous)
        Image.new("RGB", (32, 32), "blue").save(prop)
        Image.new("RGB", (32, 32), "green").save(location)
        state = {
            "output_dir": self.output_dir,
            "run_id": "run",
            "provider_capabilities": {"reference_mode": "multi", "max_references": 4},
            "foundation_assets": [
                {"asset_id": "prop_p1", "asset_type": "key_prop", "spec": {"prop_id": "p1"}},
                {"asset_id": "location_1", "asset_type": "location_master", "spec": {}},
            ],
            "locked_assets": {
                "prop_p1": {"path": "prop.png"},
                "location_1": {"path": "location.png"},
            },
        }
        issue = {
            "category": "storyboard_execution", "correction_target": "artifact",
            "focus_asset_ids": ["prop_p1"],
        }
        second_refs, second_meta = _revision_provider_references(
            state, ["location_1", "prop_p1"], "second", previous,
            shot={"visible_prop_ids": ["p1"]}, issues=[issue], revision_attempt_number=2,
        )
        self.assertEqual(second_refs[0], prop)
        self.assertIn(previous, second_refs)
        self.assertEqual(second_meta["primary_reference_role"], "focused_locked_assets")

        clean_refs, clean_meta = _revision_provider_references(
            state, ["location_1", "prop_p1"], "clean", previous,
            shot={"visible_prop_ids": ["p1"]}, issues=[issue], revision_attempt_number=3,
        )
        self.assertEqual(clean_refs[0], prop)
        self.assertNotIn(previous, clean_refs)
        self.assertEqual(clean_meta["primary_reference_role"], "clean_regeneration")
        self.assertEqual(
            clean_meta["previous_shot_reference_role"], "excluded_nonconverging_source"
        )

    def test_clean_regeneration_failure_stops_ordinary_manual_retry_loop(self):
        relative = os.path.join("assets", "shots", "run", "scene_1", "shot_1.1_retry03.png")
        image_path = os.path.join(self.output_dir, relative)
        os.makedirs(os.path.dirname(image_path), exist_ok=True)
        Image.new("RGB", (32, 48), "red").save(image_path)
        with open(image_path + ".manju.json", "w", encoding="utf-8") as handle:
            json.dump({
                "production": {"primary_reference_role": "clean_regeneration"},
            }, handle)
        issue = {
            "issue_id": "blocking_1", "shot_id": "1.1", "blocking": True,
            "category": "effect_alignment", "correction_target": "effect_alignment",
        }
        group_state = {
            "generated": {"1.1": relative}, "status": "revised", "approved": True,
            "review_history": [
                {"issues": [dict(issue)]},
                {"issues": [dict(issue)]},
                {"issues": [dict(issue)]},
            ],
        }
        state = {
            "output_dir": self.output_dir, "run_id": "run", "counters": {"image_calls": 9},
            "group_states": {"scene_1": group_state}, "pending_approval": {"stage": "manual_review"},
        }
        stopped = _apply_scene_convergence_gate(
            state, {"group_id": "scene_1"}, group_state, [issue]
        )
        self.assertTrue(stopped)
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(state["stop_reason"], "scene_group_non_converging")
        self.assertEqual(state["repair_plan"]["status"], "non_converging")
        self.assertEqual(state["repair_plan"]["maximum_paid_calls"], 0)
        self.assertFalse(state["repair_plan"]["requires_new_paid_grant"])
        self.assertEqual(state["pending_approval"], {})
        self.assertEqual(state["quality_gate"]["quality_outcome"], "blocked")
        self.assertEqual(state["quality_gate"]["blocking_status"], "blocked")
        evidence = state["repair_plan"]["convergence"]["evidence_by_shot"]["1.1"]
        self.assertEqual(evidence["lineage_status"], "legacy_unverified")
        self.assertEqual(evidence["executed_strategies"], ["clean_regeneration"])

    def test_convergence_lineage_survives_target_label_drift(self):
        old_issue = {
            "issue_id": "old", "shot_id": "1.1", "blocking": True,
            "category": "storyboard_execution", "correction_target": "artifact",
            "storyboard_path": "$.scenes[0].shots[0]",
            "reference_asset_ids": ["p1"], "focus_asset_ids": ["p1"],
        }
        current_issue = {
            **old_issue, "issue_id": "current", "correction_target": "effect_alignment",
        }
        contract = _correction_contract("scene_1", "1.1", [old_issue])
        group_state = {
            "generated": {}, "issues": [current_issue], "status": "revised", "approved": True,
            "review_history": [{"issues": [old_issue], "vision_available": True}],
            "revision_attempt_history": [{
                "attempt_id": "clean", "logical_job_id": "retry:scene_1:r03:1.1",
                "shot_id": "1.1", "correction_contract_id": contract["correction_contract_id"],
                "strategy": "clean_regeneration", "provider_outcome": "succeeded",
                "artifact_path": "shot.png",
            }],
        }
        state = {
            "run_id": "run", "output_dir": self.output_dir, "counters": {},
            "group_states": {"scene_1": group_state},
            "scene_groups": [{"group_id": "scene_1"}], "pending_approval": {"x": 1},
        }
        self.assertTrue(_apply_scene_convergence_gate(
            state, {"group_id": "scene_1"}, group_state, [current_issue]
        ))
        evidence = state["repair_plan"]["convergence"]["evidence_by_shot"]["1.1"]
        self.assertEqual(evidence["lineage_status"], "verified")
        self.assertEqual(evidence["executed_strategies"], ["clean_regeneration"])

    def test_visual_review_can_reuse_only_an_open_correction_contract(self):
        image_path = os.path.join(self.output_dir, "shot.png")
        Image.new("RGB", (32, 48), "red").save(image_path)
        relative = os.path.relpath(image_path, self.output_dir)
        open_id = "constraint_open"
        group = {
            "group_id": "scene_1", "reference_asset_ids": ["p1"],
            "shots": [{"shot_id": "1.1", "storyboard_path": "$.scenes[0].shots[0]"}],
        }
        state = {
            "output_dir": self.output_dir,
            "group_states": {"scene_1": {"revision_attempt_history": [{
                "shot_id": "1.1", "correction_contract_id": open_id,
                "artifact_path": relative,
            }]}},
        }
        base = {
            "issue_id": "provider", "shot_id": "1.1", "category": "storyboard_execution",
            "severity": "major", "blocking": True, "problem": "relation remains invalid",
            "instruction": "restore the declared relation",
            "storyboard_path": "$.scenes[0].shots[0]", "reference_asset_ids": ["p1"],
            "focus_asset_ids": ["p1"], "correction_target": "effect_alignment",
        }
        reused = _normalize_visual_issues(
            {"issues": [{**base, "correction_contract_id": open_id}]},
            state, group, {"1.1": relative},
        )[0]
        invented = _normalize_visual_issues(
            {"issues": [{**base, "correction_contract_id": "constraint_invented"}]},
            state, group, {"1.1": relative},
        )[0]
        self.assertEqual(reused["correction_contract_id"], open_id)
        self.assertNotEqual(invented["correction_contract_id"], "constraint_invented")
        self.assertTrue(invented["correction_contract_id"].startswith("constraint_"))

    def test_new_constraint_does_not_inherit_clean_attempt_or_group_retry_count(self):
        old_issue = {
            "issue_id": "old", "shot_id": "1.1", "blocking": True,
            "category": "storyboard_execution", "correction_target": "artifact",
            "storyboard_path": "$.scenes[0].shots[0]",
            "reference_asset_ids": ["p1"], "focus_asset_ids": ["p1"],
        }
        new_issue = {
            **old_issue, "issue_id": "new", "correction_target": "prop_geometry",
            "reference_asset_ids": ["p2"], "focus_asset_ids": ["p2"],
        }
        old_contract = _correction_contract("scene_1", "1.1", [old_issue])
        new_contract = _correction_contract("scene_1", "1.1", [new_issue])
        history = [{
            "attempt_id": "clean-old", "logical_job_id": "retry:scene_1:r07:1.1",
            "shot_id": "1.1", "correction_contract_id": old_contract["correction_contract_id"],
            "strategy": "clean_regeneration", "provider_outcome": "succeeded",
            "artifact_path": "shot.png",
        }]
        group_state = {
            "retry_count": 7, "generated": {}, "issues": [new_issue],
            "revision_attempt_history": history, "review_history": [],
        }
        state = {
            "run_id": "run", "output_dir": self.output_dir,
            "group_states": {"scene_1": group_state},
            "scene_groups": [{"group_id": "scene_1"}], "pending_approval": {},
        }
        self.assertEqual(
            _next_contract_attempt_number(
                group_state, "1.1", new_contract["correction_contract_id"]
            ),
            1,
        )
        self.assertFalse(_apply_scene_convergence_gate(
            state, {"group_id": "scene_1"}, group_state, [new_issue]
        ))

    def test_clean_regeneration_empty_provider_output_requests_exact_technical_retry(self):
        previous = os.path.join(self.output_dir, "previous.png")
        prop = os.path.join(self.output_dir, "prop.png")
        Image.new("RGB", (32, 48), "red").save(previous)
        Image.new("RGB", (32, 48), "blue").save(prop)
        previous_relative = os.path.relpath(previous, self.output_dir)
        issue = {
            "issue_id": "blocking", "shot_id": "1.1", "blocking": True,
            "category": "storyboard_execution", "correction_target": "effect_alignment",
            "storyboard_path": "$.scenes[0].shots[0]",
            "reference_asset_ids": ["prop_p1"], "focus_asset_ids": ["prop_p1"],
            "problem": "relationship is invalid", "instruction": "restore the declared relation",
        }
        contract = _correction_contract("scene_1", "1.1", [issue])
        prior_attempts = [{
            "attempt_id": f"prior-{number}",
            "logical_job_id": f"retry:scene_1:r0{number}:1.1",
            "shot_id": "1.1", "correction_contract_id": contract["correction_contract_id"],
            "strategy": "previous_shot_edit" if number == 1 else "focused_locked_assets",
            "provider_outcome": "succeeded", "artifact_path": previous_relative,
        } for number in (1, 2)]
        shot = {
            "shot_id": "1.1", "storyboard_path": "$.scenes[0].shots[0]",
            "visible_character_ids": [], "visible_prop_ids": ["p1"],
            "reference_asset_ids": ["prop_p1"], "prompt": "generic scene",
            "description": "a subject holds a referenced object",
        }
        group = {
            "group_id": "scene_1", "shot_ids": ["1.1"], "shots": [shot],
            "reference_asset_ids": ["prop_p1"],
        }
        group_state = {
            "status": "approved", "approved": True, "retry_count": 2,
            "generated": {"1.1": previous_relative}, "issues": [issue],
            "pending_paid_operation": "retry", "grant_id": "retry_grant",
            "revision_attempt_history": prior_attempts,
        }
        state = {
            "run_id": "run", "output_dir": self.output_dir, "paid_authorized": True,
            "storyboard": _storyboard(), "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_1": group_state},
            "foundation_assets": [{
                "asset_id": "prop_p1", "asset_type": "key_prop", "spec": {"prop_id": "p1"},
            }],
            "locked_assets": {"prop_p1": {"path": "prop.png", "version": 1}},
            "provider_capabilities": {"reference_mode": "multi", "max_references": 4},
            "size": "1024x1536", "target_aspect_ratio": 9 / 16, "aspect_mode": "cover",
            "budgets": {"image_parallelism": 1}, "counters": {"image_calls": 0},
            "approval_grants": {}, "pending_approval": {"unexpected": True},
        }
        _register_approval_grant(state, {
            "request_id": "retry_grant", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "fp", "maximum_paid_calls": 1,
        })

        result = _tool_revise_scene_group(state, lambda *_args: None)

        self.assertTrue(result["technical_failure"])
        self.assertTrue(result["requires_new_paid_approval"])
        self.assertEqual(result["technical_retry_shot_ids"], ["1.1"])
        self.assertEqual(result["maximum_paid_calls"], 1)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["stage"], "group_approval")
        self.assertEqual(group_state["pending_paid_operation"], "technical_retry")
        self.assertEqual(state["pending_approval"], {})
        self.assertEqual(state["repair_plan"]["status"], "technical_retry_approval_required")
        self.assertEqual(state["repair_plan"]["maximum_paid_calls"], 1)
        latest = group_state["revision_attempt_history"][-1]
        self.assertEqual(latest["strategy"], "clean_regeneration")
        self.assertEqual(latest["provider_outcome"], "failed")
        ledger = _load_paid_ledger(state)
        self.assertEqual(sum(item["used_calls"] for item in ledger["grants"].values()), 1)
        self.assertEqual(len(ledger["jobs"]), 1)

    def test_partial_revision_failure_retries_only_failed_shot_with_same_lineage(self):
        previous_paths = {}
        shots = []
        issues = []
        for number in range(1, 4):
            shot_id = f"1.{number}"
            path = os.path.join(self.output_dir, f"previous_{number}.png")
            Image.new("RGB", (32, 48), (number * 40, 30, 90)).save(path)
            previous_paths[shot_id] = os.path.relpath(path, self.output_dir)
            shots.append({
                "shot_id": shot_id,
                "storyboard_path": f"$.scenes[0].shots[{number - 1}]",
                "visible_character_ids": [], "visible_prop_ids": [],
                "reference_asset_ids": [], "prompt": f"generic scene {number}",
                "description": f"subject action {number}",
            })
            issues.append({
                "issue_id": f"blocking_{number}", "shot_id": shot_id,
                "blocking": True, "category": "storyboard_execution",
                "correction_target": "effect_alignment",
                "storyboard_path": f"$.scenes[0].shots[{number - 1}]",
                "reference_asset_ids": [], "focus_asset_ids": [],
                "problem": f"relationship {number} is invalid",
                "instruction": f"restore relationship {number}",
            })
        group = {
            "group_id": "scene_1", "shot_ids": [item["shot_id"] for item in shots],
            "shots": shots, "reference_asset_ids": [],
        }
        group_state = {
            "group_id": "scene_1", "status": "approved", "approved": True,
            "retry_count": 0, "generated": previous_paths, "issues": issues,
            "pending_paid_operation": "retry", "grant_id": "revision_grant",
            "revision_attempt_history": [],
        }
        state = {
            "run_id": "partial-revision", "output_dir": self.output_dir,
            "storyboard_path": self.storyboard_path, "paid_authorized": True,
            "storyboard": _storyboard(), "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_1": group_state}, "foundation_assets": [],
            "locked_assets": {}, "provider_capabilities": {
                "reference_mode": "multi", "max_references": 4,
            },
            "size": "1024x1536", "target_aspect_ratio": 9 / 16,
            "aspect_mode": "cover", "budgets": {
                "image_parallelism": 1, "max_auto_retries": 2,
            },
            "counters": {"image_calls": 0, "vision_calls": 0,
                         "vision_attempts": 0, "vision_failures": 0},
            "approval_grants": {}, "pending_approval": {}, "quality_gate": {},
            "status": "running", "stage": "group_retry", "stop_reason": "",
        }
        _register_approval_grant(state, {
            "request_id": "revision_grant", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "revision", "maximum_paid_calls": 3,
        })
        first_provider = MockImageProvider(fail_call=2)

        first = _tool_revise_scene_group(state, first_provider)

        self.assertEqual(first["technical_retry_shot_ids"], ["1.2"])
        self.assertEqual(first["maximum_paid_calls"], 1)
        self.assertEqual(group_state["technical_retry"]["succeeded_unreviewed_shot_ids"],
                         ["1.1", "1.3"])
        self.assertEqual(state["quality_gate"]["automated_review_status"],
                         "stale_after_partial_revision")
        self.assertIsNone(state["quality_gate"]["blocking_issue_count"])
        self.assertEqual(state["quality_gate"]["stale_after_revision_shot_ids"],
                         ["1.1", "1.3"])
        failed_before = next(
            item for item in _load_paid_ledger(state)["jobs"].values()
            if item["status"] == "failed"
        )
        original_logical_id = failed_before["logical_job_id"]
        original_contract_id = failed_before["correction_contract_id"]
        original_round = group_state["technical_retry"]["revision_round"]

        approval = _tool_request_scene_group_approval(state)
        self.assertEqual(approval["maximum_paid_calls"], 1)
        self.assertEqual(state["pending_approval"]["item_ids"], ["1.2"])
        group_state["grant_id"] = _register_approval_grant(state, {
            "request_id": "technical_grant", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "technical", "maximum_paid_calls": 1,
        })
        group_state["approved"] = True
        state["paid_authorized"] = True
        state["status"] = "running"
        state["stage"] = "group_retry"
        retry_provider = MockImageProvider()

        retried = _tool_revise_scene_group(state, retry_provider)

        self.assertEqual(retried["revised"], 1)
        self.assertEqual(len(retry_provider.calls), 1)
        self.assertEqual(group_state["retry_count"], original_round)
        self.assertEqual(state["stage"], "group_review")
        ledger = _load_paid_ledger(state)
        same_lineage = [
            item for item in ledger["jobs"].values()
            if item["logical_job_id"] == original_logical_id
        ]
        self.assertEqual(len(same_lineage), 2)
        self.assertEqual({item["correction_contract_id"] for item in same_lineage},
                         {original_contract_id})
        old_failed = next(item for item in same_lineage if item["status"] == "failed")
        self.assertEqual(old_failed["lifecycle_status"], "superseded")
        self.assertTrue(old_failed["superseded_by"])
        self.assertTrue(group_state["issues_stale"])
        self.assertEqual(group_state["issues_stale_after_revision_shot_ids"],
                         ["1.1", "1.2", "1.3"])

        inspected = _tool_inspect_scene_group(state, MockVisionProvider())
        self.assertEqual(inspected["blocking_count"], 0)
        self.assertNotIn("issues_stale", group_state)
        self.assertNotIn("stale_after_revision_shot_ids", state["quality_gate"])

    def test_resume_migration_contract_changes_for_material_invocation_inputs(self):
        storyboard = _storyboard()
        base = _resume_invocation_contract(
            storyboard,
            {"llm_model": "llm-a", "image_model": "image-a", "vision_model": "vision-a"},
            {"reference_mode": "multi", "max_references": 4},
            3, 2, None, None, "1024x1536", 9 / 16, "cover", 4,
        )
        variants = [
            ({"llm_model": "llm-b", "image_model": "image-a", "vision_model": "vision-a"},
             {"reference_mode": "multi", "max_references": 4}, 3, None, "1024x1536"),
            ({"llm_model": "llm-a", "image_model": "image-a", "vision_model": "vision-a"},
             {"reference_mode": "single", "max_references": 1}, 3, None, "1024x1536"),
            ({"llm_model": "llm-a", "image_model": "image-a", "vision_model": "vision-a"},
             {"reference_mode": "multi", "max_references": 4}, 3, 80, "1024x1536"),
            ({"llm_model": "llm-a", "image_model": "image-a", "vision_model": "vision-a"},
             {"reference_mode": "multi", "max_references": 4}, 3, None, "1536x1024"),
        ]
        for models, capabilities, candidates, max_calls, size in variants:
            with self.subTest(models=models, capabilities=capabilities,
                              max_calls=max_calls, size=size):
                candidate = _resume_invocation_contract(
                    storyboard, models, capabilities, candidates, 2, None, max_calls,
                    size, 9 / 16, "cover", 4,
                )
                self.assertNotEqual(candidate["fingerprint"], base["fingerprint"])

    def test_reconciled_old_toolset_run_is_selected_only_for_same_contract(self):
        import manju.pipeline.visual_agent as visual_agent_module

        with patch.object(visual_agent_module, "VISUAL_TOOLSET_VERSION", "3.5.9"):
            old = run_image_agent(
                self.storyboard_path, self.output_dir, execute_paid_calls=False,
                foundation_candidates=1, supervisor_provider=_supervisor,
                image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
            )
        reconciled = reconcile_paid_artifacts(self.storyboard_path, self.output_dir)
        self.assertEqual(reconciled["run_id"], old["run_id"])

        compatible = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
        )
        self.assertEqual(compatible["run_id"], old["run_id"])

        with tempfile.TemporaryDirectory() as incompatible_root:
            incompatible_dir = os.path.join(incompatible_root, "copied_output")
            shutil.copytree(self.output_dir, incompatible_dir)
            incompatible_storyboard = os.path.join(incompatible_dir, "storyboard.json")
            incompatible = run_image_agent(
                incompatible_storyboard, incompatible_dir, execute_paid_calls=False,
                foundation_candidates=1, max_calls=41, supervisor_provider=_supervisor,
                image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
            )
            self.assertNotEqual(incompatible["run_id"], old["run_id"])

    def test_revision_attempt_summary_separates_logical_retry_provider_attempts_and_artifacts(self):
        summary = _revision_attempt_summary({"jobs": {
            "failed": {
                "operation_kind": "shot_revision", "logical_job_id": "retry:scene_1:r03:1.1",
                "status": "failed",
            },
            "succeeded": {
                "operation_kind": "shot_revision", "logical_job_id": "retry:scene_1:r03:1.1",
                "status": "succeeded",
            },
            "foundation": {
                "operation_kind": "", "logical_job_id": "foundation:style_r1_c1",
                "status": "succeeded",
            },
        }})
        self.assertEqual(summary, {
            "logical_retries": 1,
            "provider_attempts": 2,
            "artifacts_created": 1,
            "failed_provider_attempts": 1,
            "duplicate_logical_attempts": 1,
            "lineage_conflict_count": 0,
            "lineage_conflict_logical_job_ids": [],
        })

    def test_failed_paid_attempt_replay_preserves_provider_attempt_evidence(self):
        state = {
            "run_id": "failed-replay", "output_dir": self.output_dir,
            "approval_grants": {}, "counters": {},
        }
        grant_id = _register_approval_grant(state, {
            "request_id": "retry_grant", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "fp", "maximum_paid_calls": 1,
        })
        provider_calls = []

        def empty_provider(*args):
            provider_calls.append(args)
            return None

        job = {
            "job_id": "retry:scene_1:r03:1.1", "operation_kind": "shot_revision",
            "group_id": "scene_1", "shot_id": "1.1", "revision_attempt_number": 3,
            "revision_strategy": "clean_regeneration",
            "correction_contract_id": "constraint_test", "prompt": "generic",
            "output_path": os.path.join(self.output_dir, "missing.png"),
            "references": [], "size": "1024x1536",
        }
        first = _run_paid_image_jobs(state, empty_provider, [job], 1, grant_id)[0]
        replayed = _run_paid_image_jobs(state, empty_provider, [job], 1, grant_id)[0]

        self.assertTrue(first["provider_attempted"])
        self.assertTrue(replayed["provider_attempted"])
        self.assertEqual(replayed["recovered_attempt_state"], "failed")
        self.assertEqual(first["ledger_job_id"], replayed["ledger_job_id"])
        self.assertEqual(len(provider_calls), 1)

    def test_repair_convergence_gate_requires_reference_reset_before_more_paid_calls(self):
        def plan_with_count(count):
            return {
                "status": "reviewed",
                "groups": [{"issues": [{"issue_id": f"issue_{index}"} for index in range(count)]}],
            }

        state = {
            "vision_repair_mode": True,
            "repair_history": [plan_with_count(3), plan_with_count(2)],
        }
        proposed = {
            "status": "proposed", "requires_new_paid_grant": True,
            "maximum_paid_calls": 3,
            "groups": [{"issues": [{"issue_id": f"new_{index}"} for index in range(3)]}],
        }
        gated = _apply_repair_convergence_gate(state, proposed)
        self.assertEqual(gated["status"], "reference_reset_required")
        self.assertFalse(gated["requires_new_paid_grant"])
        self.assertEqual(gated["maximum_paid_calls"], 0)
        self.assertEqual(gated["estimated_shot_repair_calls_after_reference_reset"], 3)

    def test_local_metadata_reconcile_backfills_legacy_sidecar_without_agent_calls(self):
        run_id = "legacy_revision_run"
        shot_relative = os.path.join(
            "assets", "shots", run_id, "scene_1", "shot_1.5_retry06_legacy.png"
        )
        shot_path = os.path.join(self.output_dir, shot_relative)
        os.makedirs(os.path.dirname(shot_path), exist_ok=True)
        Image.new("RGB", (32, 48), "red").save(shot_path)
        board_name = "scene_1_1.5_retry06_fixture.png"
        board_path = os.path.join(
            self.output_dir, "assets", "reference_boards", run_id, board_name
        )
        os.makedirs(os.path.dirname(board_path), exist_ok=True)
        Image.new("RGB", (32, 32), "blue").save(board_path)
        with open(shot_path + ".manju.json", "w", encoding="utf-8") as handle:
            json.dump({
                "references": [board_name],
                "production": {"shot_id": "1.5", "group_id": "scene_1"},
                "dimensions": {"method": "native"},
            }, handle, ensure_ascii=False, indent=2)
        ordinary_relative = os.path.join(
            "assets", "shots", "older_run", "scene_1", "shot_1.1_retry01_older.png"
        )
        ordinary_path = os.path.join(self.output_dir, ordinary_relative)
        os.makedirs(os.path.dirname(ordinary_path), exist_ok=True)
        Image.new("RGB", (32, 48), "gray").save(ordinary_path)
        with open(ordinary_path + ".manju.json", "w", encoding="utf-8") as handle:
            json.dump({
                "references": ["ordinary_reference_board.png"],
                "production": {"shot_id": "1.1", "group_id": "scene_1"},
            }, handle, ensure_ascii=False, indent=2)
        with open(board_path + ".manju.json", "w", encoding="utf-8") as handle:
            json.dump({
                "board_id": "scene_1_1.5_retry06",
                "board_role": "targeted_revision",
                "primary_shot_reference": "assets/shots/prior/scene_1/shot_1.5_retry05.png",
                "temporal_context": [{
                    "group_id": "scene_1", "shot_id": "1.4",
                    "path": "assets/shots/prior/scene_1/shot_1.4_retry03.png",
                    "role": "adjacent_continuity_reference",
                }],
            }, handle, ensure_ascii=False, indent=2)
        state_dir = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", run_id
        )
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "state.json")
        state = {
            "run_id": run_id,
            "group_states": {"scene_1": {"generated": {
                "1.1": ordinary_relative, "1.5": shot_relative,
            }}},
            "counters": {"image_calls": 51, "model_calls": 60, "vision_calls": 18},
            "quality_gate": {"quality_outcome": "passed", "blocking_issue_count": 0},
        }
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        manifest_path = os.path.join(self.output_dir, "visual_agent_run.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "status": "completed"}, handle, indent=2)
        state_before = Path(state_path).read_bytes()
        manifest_before = Path(manifest_path).read_bytes()

        first = reconcile_visual_metadata(self.storyboard_path, self.output_dir)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["updated_sidecars"], 1)
        self.assertEqual(first["scanned_revision_sidecars"], 1)
        self.assertEqual(first["skipped_non_targeted_sidecars"], 1)
        self.assertEqual(first["image_calls_before"], 51)
        self.assertEqual(first["image_calls_after"], 51)
        self.assertEqual(first["model_calls_made"], 0)
        self.assertEqual(first["vision_calls_made"], 0)
        self.assertEqual(first["image_calls_made"], 0)
        production = self._read_json(shot_path + ".manju.json")["production"]
        self.assertEqual(production["previous_shot_reference_role"], "primary_edit_reference")
        self.assertEqual(production["primary_reference_role"], "previous_shot_edit")
        self.assertEqual(production["primary_reference_asset_ids"], [])
        self.assertEqual(production["reference_strategy"], {})
        self.assertEqual(production["temporal_context_shot_ids"], ["1.4"])
        self.assertEqual(
            production["temporal_context_paths"],
            ["assets/shots/prior/scene_1/shot_1.4_retry03.png"],
        )
        self.assertEqual(Path(state_path).read_bytes(), state_before)
        self.assertEqual(Path(manifest_path).read_bytes(), manifest_before)

        second = reconcile_visual_metadata(self.storyboard_path, self.output_dir)
        self.assertEqual(second["updated_sidecars"], 0)
        self.assertEqual(second["already_complete_sidecars"], 1)
        with patch("manju.pipeline.visual_agent.run_image_agent", side_effect=AssertionError("agent called")):
            cli_result = CliRunner().invoke(cli, [
                "image-agent", self.storyboard_path, "-o", self.output_dir,
                "--reconcile-metadata", "--no-image-api",
            ])
        self.assertEqual(cli_result.exit_code, 0, cli_result.output)
        self.assertIn('"image_calls_made": 0', cli_result.output)

    def test_recheck_and_repair_modes_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            run_image_agent(
                self.storyboard_path, self.output_dir,
                recheck_vision=True, repair_vision_blockers=True,
                supervisor_provider=_supervisor,
                image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
            )

    def test_blanket_manual_override_cannot_cover_specific_vision_blockers(self):
        image = MockImageProvider()
        vision = MockVisionProvider(blocking_once=True)
        manifest = None
        for _ in range(20):
            manifest = self._run(image, vision, foundation_candidates=1, max_auto_retries=0)
            if manifest["stop_reason"].startswith("manual_review_"):
                break
            self.assertEqual(manifest["status"], "awaiting_approval")
            self._approve(manifest)
        self.assertIsNotNone(manifest)
        pending = manifest["pending_approval"]
        request = self._read_json(os.path.join(self.output_dir, pending["request_path"]))
        decision_path = os.path.join(self.output_dir, pending["decision_path"])
        decision = self._read_json(decision_path)
        decision.update({
            "decision": "approve",
            "override_reason": "Reviewed all images and everything meets the storyboard requirements.",
            "reviewer": "Mock Human Reviewer",
            "reviewed_item_ids": request["item_ids"],
            "reviewed_image_fingerprints": request.get("reviewed_image_fingerprints", {}),
        })
        decision.pop("issue_override_reasons", None)
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle, ensure_ascii=False, indent=2)

        rejected = self._run(image, vision, foundation_candidates=1, max_auto_retries=0)

        self.assertEqual(rejected["status"], "failed")
        self.assertIn("issue_override_reasons", rejected["stop_reason"])

    def test_stale_approval_is_rejected_before_paid_call(self):
        image = MockImageProvider()
        manifest = self._run(image, MockVisionProvider())
        decision_path = os.path.join(self.output_dir, manifest["pending_approval"]["decision_path"])
        decision = self._read_json(decision_path)
        decision["decision"] = "approve"
        decision["state_fingerprint"] = "stale"
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle)
        failed = self._run(image, MockVisionProvider())
        self.assertEqual(failed["status"], "failed")
        self.assertIn("invalid_approval", failed["stop_reason"])
        self.assertEqual(image.calls, [])

    def test_placeholder_reviewer_cannot_impersonate_human_approval(self):
        image = MockImageProvider()
        manifest = self._run(image, MockVisionProvider(), foundation_candidates=1)
        pending = manifest["pending_approval"]
        request = self._read_json(os.path.join(self.output_dir, pending["request_path"]))
        decision_path = os.path.join(self.output_dir, pending["decision_path"])
        decision = self._read_json(decision_path)
        decision.update({
            "decision": "approve", "reviewer": "auto",
            "reviewed_item_ids": request["item_ids"],
            "reviewed_image_fingerprints": request.get("reviewed_image_fingerprints", {}),
        })
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle)
        rejected = self._run(image, MockVisionProvider(), foundation_candidates=1)
        self.assertEqual(rejected["status"], "failed")
        self.assertTrue(rejected["stop_reason"].startswith("invalid_approval:"))
        self.assertEqual(image.calls, [])

    def test_same_pending_resume_does_not_repeat_work(self):
        image = MockImageProvider()
        first = self._run(image, MockVisionProvider())
        second = self._run(image, MockVisionProvider())
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["counters"], second["counters"])
        self.assertEqual(image.calls, [])

    def test_normal_resume_starts_fresh_budget_and_archives_prior_invocation(self):
        image = MockImageProvider()
        first = self._run(image, MockVisionProvider(), max_calls=1)
        self.assertEqual(first["stop_reason"], "model_budget_exhausted")
        self.assertEqual(first["counters"]["model_calls"], 1)
        self.assertEqual(first["run_budget_usage"]["model_calls"], 1)
        self.assertEqual(first["invocation_budget_history"], [])

        second = self._run(image, MockVisionProvider(), max_calls=1)
        self.assertEqual(second["stop_reason"], "model_budget_exhausted")
        self.assertEqual(second["counters"]["model_calls"], 2)
        self.assertEqual(second["run_budget_usage"]["model_calls"], 1)
        self.assertEqual(second["budget_usage_scope"], "current_invocation")
        self.assertEqual(len(second["invocation_budget_history"]), 1)
        archived = second["invocation_budget_history"][0]
        self.assertEqual(archived["invocation_count"], 0)
        self.assertEqual(archived["usage"], {"model_calls": 1, "tool_steps": 1})
        self.assertEqual(archived["stop_reason_at_end"], "model_budget_exhausted")
        self.assertEqual(archived["cumulative_counters_at_end"]["model_calls"], 1)
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(any(item["event"] == "invocation_budget_started" for item in events))
        self.assertTrue(any(item["event"] == "invocation_budget_resume" for item in events))

    def test_durable_foundation_grant_is_visible_across_phases(self):
        state = {
            "run_id": "grant-snapshot", "output_dir": self.output_dir,
            "stage": "foundation_generate", "foundation_phase_index": 1,
            "foundation_assets": [{"phase": "character_identity"}],
            "foundation_budget_approved": True, "paid_authorized": True,
            "foundation_grant_id": "foundation_cost_test", "approval_grants": {},
            "counters": {}, "group_states": {}, "scene_groups": [],
        }
        _register_approval_grant(state, {
            "request_id": "foundation_cost_test", "stage": "foundation_cost",
            "state_fingerprint": "fp", "maximum_paid_calls": 9,
        })
        # Deliberately stale in-memory data must not override the ledger.
        state["approval_grants"]["foundation_cost_test"]["used_calls"] = 99
        snapshot = _authorization_snapshot(state)
        self.assertTrue(snapshot["foundation_budget_approved"])
        self.assertTrue(snapshot["current_paid_action_authorized"])
        self.assertEqual(snapshot["used_calls"], 0)
        self.assertEqual(snapshot["remaining_calls"], 9)
        self.assertEqual(snapshot["approval_state"], "active")
        self.assertEqual(snapshot["remaining_calls_scope"], "active_approval_grant")
        self.assertEqual(snapshot["provider_quota_state"], "unknown_not_queried")

    def test_local_approval_state_distinguishes_not_requested_pending_and_exhausted(self):
        state = {
            "run_id": "approval-semantics", "output_dir": self.output_dir,
            "stage": "planned", "foundation_phase_index": 0,
            "foundation_assets": [], "foundation_budget_approved": False,
            "paid_authorized": True, "foundation_grant_id": "",
            "approval_grants": {}, "pending_approval": {},
            "counters": {}, "group_states": {}, "scene_groups": [],
        }
        self.assertEqual(_authorization_snapshot(state)["approval_state"], "not_requested")
        state["pending_approval"] = {"stage": "foundation_cost"}
        self.assertEqual(_authorization_snapshot(state)["approval_state"], "pending")
        state["pending_approval"] = {}
        state["stage"] = "foundation_generate"
        state["foundation_budget_approved"] = True
        state["foundation_grant_id"] = "grant_exhausted"
        _register_approval_grant(state, {
            "request_id": "grant_exhausted", "stage": "foundation_cost",
            "state_fingerprint": "fp", "maximum_paid_calls": 0,
        })
        snapshot = _authorization_snapshot(state)
        self.assertEqual(snapshot["approval_state"], "exhausted")
        self.assertEqual(snapshot["provider_quota_state"], "unknown_not_queried")

    def test_stop_needs_review_requires_specific_reason(self):
        def empty_stop(_snapshot):
            return {"action": "stop_needs_review", "params": {}, "summary": "stop"}

        manifest = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=empty_stop, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(manifest["status"], "needs_review")
        self.assertEqual(manifest["stop_reason"], "three_invalid_actions")
        self.assertEqual(manifest["counters"]["model_calls"], 3)
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            errors = [json.loads(line) for line in handle if '"protocol_error"' in line]
        self.assertTrue(errors)
        self.assertIn("stop reason", " ".join(errors[0]["payload"]["errors"]))
        resumed = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True, resume=True,
            supervisor_provider=_supervisor, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(resumed["stop_reason"], "three_invalid_actions")

    def test_unusable_supervisor_response_uses_single_stage_code_fallback(self):
        manifest = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=lambda _snapshot: None,
            image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
        )

        self.assertEqual(manifest["status"], "awaiting_approval")
        self.assertEqual(manifest["pending_approval"]["stage"], "foundation_cost")
        self.assertEqual(
            manifest["recovery_patch_version"], "4.0.0-provider-escalation-rc2"
        )
        self.assertEqual(manifest["counters"]["model_calls"], 1)
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle]
        fallbacks = [event for event in events if event["event"] == "supervisor_fallback"]
        self.assertEqual(
            [event["payload"]["action"] for event in fallbacks],
            ["inspect_storyboard", "build_visual_bible", "request_foundation_approval"],
        )
        self.assertFalse(any(event["event"] == "protocol_error" for event in events))

    def test_legacy_empty_supervisor_stop_recovers_without_manual_override(self):
        def invalid_stop(_snapshot):
            return {"action": "stop_needs_review", "params": {}, "summary": "stop"}

        stopped = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=invalid_stop, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(stopped["stop_reason"], "three_invalid_actions")
        private_trace = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", stopped["run_id"], "trace.jsonl",
        )
        with open(private_trace, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle]
        for event in events:
            if event.get("event") == "protocol_error":
                event["payload"].update({
                    "action": "",
                    "parameter_keys": [],
                    "normalized_params": {},
                    "unknown_fields": [],
                    "errors": ["response must be a JSON object"],
                })
        with open(private_trace, "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

        resumed = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True, resume=True,
            supervisor_provider=_supervisor, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(resumed["pending_approval"]["stage"], "foundation_cost")
        with open(private_trace, encoding="utf-8") as handle:
            resumed_events = [json.loads(line) for line in handle]
        recovery = [event for event in resumed_events if event["event"] == "technical_supervisor_recovery"]
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["payload"]["previous_stop_reason"], "three_invalid_actions")

    def test_stop_action_common_aliases_are_normalized(self):
        def aliased_stop(_snapshot):
            return {
                "tool": "stop_needs_review",
                "arguments": {
                    "stop_reason": "需要导演确认人物造型",
                    "reasonCode": "director_check",
                    "current_evidence": "candidate board",
                },
                "decision_summary": "pause",
            }

        manifest = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=aliased_stop, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(manifest["stop_reason"], "supervisor_stopped:director_check")
        self.assertEqual(manifest["counters"]["model_calls"], 1)

    def test_unverified_upstream_storyboard_blocks_all_paid_calls(self):
        storyboard = _storyboard()
        storyboard.setdefault("metadata", {})["agent_status"] = "needs_review"
        storyboard["metadata"]["agent_verification_state"] = "audited_with_blockers"
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(storyboard, handle, ensure_ascii=False)
        image = MockImageProvider()
        manifest = self._run(image, MockVisionProvider())
        self.assertEqual(manifest["status"], "needs_review")
        self.assertEqual(manifest["stop_reason"], "upstream_storyboard_needs_review")
        self.assertEqual(manifest["quality_gate"]["quality_outcome"], "blocked")
        self.assertEqual(image.calls, [])
        self.assertEqual(manifest["counters"]["image_calls"], 0)

    def test_agent_storyboard_with_missing_or_unknown_verification_is_fail_closed(self):
        for verification in (None, "", "unknown"):
            with self.subTest(verification=verification):
                storyboard = _storyboard()
                storyboard.setdefault("metadata", {}).update({
                    "generation_engine": "agent", "agent_status": "completed",
                })
                if verification is not None:
                    storyboard["metadata"]["agent_verification_state"] = verification
                with open(self.storyboard_path, "w", encoding="utf-8") as handle:
                    json.dump(storyboard, handle, ensure_ascii=False)
                image = MockImageProvider()
                manifest = self._run(image, MockVisionProvider(), resume=False)
                self.assertEqual(manifest["stop_reason"], "upstream_storyboard_needs_review")
                self.assertEqual(image.calls, [])

    def test_explicit_manual_resume_continues_only_supervisor_stop(self):
        def human_review_stop(_snapshot):
            return {"action": "stop_needs_review", "params": {
                "reason": "A human should confirm this unusual source asset.",
                "reason_code": "unusual_source_asset",
                "evidence": ["storyboard input"],
            }, "summary": "pause"}

        first = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=human_review_stop, image_provider=MockImageProvider(),
            vision_provider=MockVisionProvider(),
        )
        self.assertEqual(first["stop_reason"], "supervisor_stopped:unusual_source_asset")
        unchanged = self._run(MockImageProvider(), MockVisionProvider())
        self.assertEqual(unchanged["status"], "needs_review")
        resumed = self._run(
            MockImageProvider(), MockVisionProvider(), resume_needs_review=True,
            resume_reviewer="Human Director", resume_note="Reviewed the source asset and approved continued planning.",
        )
        self.assertEqual(resumed["status"], "awaiting_approval")
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(any(item["event"] == "manual_resume" for item in events))

    def test_automated_human_notes_are_rejected_generically(self):
        for value in ("auto", "Automatic approval by script", "script selected candidate",
                      "\u81ea\u52a8\u5ba1\u6279\uff1a\u9009\u62e9\u5019\u9009\u8d44\u4ea7", "\u811a\u672c\u9009\u62e9\u5019\u9009\u4e00",
                      "\u5df2\u5ba1\u9605\u6240\u6709\u56fe\u7247\uff0c\u786e\u8ba4\u89c6\u89c9\u8d28\u91cf\u7b26\u5408\u5206\u955c\u8981\u6c42"):
            self.assertTrue(_is_placeholder_review_text(value), value)

    def test_trace_marks_paid_actions_from_ledger_not_model_summary(self):
        manifest = self._drive(MockImageProvider(), MockVisionProvider(), foundation_candidates=1)
        self.assertEqual(manifest["status"], "completed")
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        paid = [item for item in events if item["event"] == "tool_result" and item["payload"].get("paid_action")]
        self.assertTrue(paid)
        self.assertTrue(all(item["payload"]["paid_calls_after"] > item["payload"]["paid_calls_before"] for item in paid))

    def test_images_sharing_locked_references_run_in_parallel(self):
        image = ConcurrencyImageProvider()
        manifest = self._drive(
            image, MockVisionProvider(), foundation_candidates=3, image_parallelism=3
        )
        self.assertEqual(manifest["status"], "completed")
        self.assertGreaterEqual(image.peak, 2)
        self.assertEqual(manifest["budgets"]["image_parallelism"], 3)

    def test_per_shot_reference_boards_keep_location_and_only_needed_cast(self):
        storyboard = _storyboard(two_shots=True)
        storyboard["creative_bible"]["characters"].append({
            "character_id": "c2", "name": "阿哲", "role": "同伴",
            "anchor_description": "短发，灰色夹克",
        })
        storyboard["scenes"][0]["shots"][1]["visual"]["visible_character_ids"] = ["c1", "c2"]
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(storyboard, handle, ensure_ascii=False)
        image = MockImageProvider()
        manifest = self._drive(image, MockVisionProvider(), foundation_candidates=1)
        self.assertEqual(manifest["status"], "completed")
        plan = self._read_json(os.path.join(self.output_dir, "visual_plan.json"))
        shots = plan["scene_groups"][0]["shots"]
        first_refs = shots[0]["reference_asset_ids"]
        second_refs = shots[1]["reference_asset_ids"]
        self.assertTrue(any(value.startswith("location_") for value in first_refs))
        self.assertFalse(any("char_c2" in value for value in first_refs))
        self.assertEqual(len(second_refs), 8)
        shot_calls = [
            call for call in image.calls
            if "assets" in Path(call["path"]).parts and "shots" in Path(call["path"]).parts
        ]
        second_board = shot_calls[-1]["references"][0]
        board_manifest = self._read_json(second_board + ".manju.json")
        self.assertEqual(board_manifest["asset_ids"], second_refs)
        self.assertTrue(any(value.startswith("location_") for value in board_manifest["asset_ids"]))

    def test_accept_alias_applies_current_approval(self):
        image = MockImageProvider()
        first = self._run(image, MockVisionProvider(), foundation_candidates=1)
        decision_path = os.path.join(self.output_dir, first["pending_approval"]["decision_path"])
        decision = self._read_json(decision_path)
        decision["decision"] = "accept"
        request_path = os.path.join(self.output_dir, first["pending_approval"]["request_path"])
        request = self._read_json(request_path)
        decision["reviewer"] = "Mock Human Reviewer"
        decision["reviewed_item_ids"] = request["item_ids"]
        decision["reviewed_image_fingerprints"] = request.get("reviewed_image_fingerprints", {})
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle)
        resumed = self._run(image, MockVisionProvider(), foundation_candidates=1)
        self.assertNotEqual(resumed["status"], "failed")
        self.assertGreater(len(image.calls), 0)

    def test_new_run_replaces_public_trace_and_isolates_approvals(self):
        image = MockImageProvider()
        first = self._run(image, MockVisionProvider(), image_parallelism=2)
        second = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True, resume=False,
            image_parallelism=3, supervisor_provider=_supervisor,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertNotEqual(first["run_id"], second["run_id"])
        with open(os.path.join(self.output_dir, "visual_agent_trace.jsonl"), encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(events)
        self.assertEqual({event["run_id"] for event in events}, {second["run_id"]})
        self.assertIn(second["run_id"], second["pending_approval"]["decision_path"])

    def test_provider_capability_change_creates_new_run(self):
        image = MockImageProvider()
        first = self._run(image, MockVisionProvider(), provider_capabilities={
            "reference_mode": "single", "max_references": 1, "multi_reference_field": "images"
        })
        second = self._run(image, MockVisionProvider(), provider_capabilities={
            "reference_mode": "multi", "max_references": 8, "multi_reference_field": "images"
        })
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_multi_reference_provider_receives_locked_assets_directly(self):
        image = MockImageProvider()
        self._drive(
            image, MockVisionProvider(), foundation_candidates=1,
            provider_capabilities={
                "reference_mode": "multi", "max_references": 8,
                "multi_reference_field": "images",
            },
        )
        self.assertGreater(max(len(call["references"]) for call in image.calls), 1)

    def test_tool_failure_resume_keeps_completed_candidate_call(self):
        image = MockImageProvider(fail_call=2)
        vision = MockVisionProvider()
        first = self._run(image, vision, foundation_candidates=3)
        self._approve(first)
        failed = self._run(image, vision, foundation_candidates=3)
        self.assertEqual(failed["status"], "awaiting_approval")
        self.assertEqual(failed["stop_reason"], "foundation_retry_cost")
        self.assertEqual(failed["counters"]["image_calls"], 3)
        image.fail_call = 0
        self._approve(failed)
        resumed = self._run(image, vision, foundation_candidates=3)
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(resumed["stop_reason"], "foundation_lock_style")
        self.assertEqual(resumed["counters"]["image_calls"], 4)
        self.assertEqual(len(image.calls), 4)  # two successes, one failure, then only the missing candidate
        state = self._read_json(os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", resumed["run_id"], "state.json"
        ))
        self.assertTrue(state["foundation_primary_grant_id"])
        self.assertEqual(state["foundation_grant_id"], state["foundation_primary_grant_id"])
        self.assertEqual(state["foundation_retry_grant_id"], "")
        ledger = _load_paid_ledger(state)
        superseded = [item for item in ledger["jobs"].values() if item.get("superseded_by")]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["lifecycle_status"], "superseded")

    def test_foundation_metadata_and_identity_safe_style_prompt(self):
        style_asset = {
            "asset_id": "style_001", "asset_type": "style_board", "phase": "style",
            "label": "project style board", "spec": {},
        }
        prompt = _asset_prompt(style_asset, _storyboard(), 1)
        self.assertIn("Show no people", prompt)
        self.assertIn("never depict the named person or garment", prompt)
        self.assertIn("not a character identity reference", prompt)
        identity_asset = {
            "asset_id": "char_c1_identity", "asset_type": "character_identity",
            "phase": "character_identity", "label": "identity sheet", "spec": {},
        }
        identity_prompt = _asset_prompt(identity_asset, _storyboard(), 1)
        self.assertIn("do not create front-side-back", identity_prompt)
        self.assertNotIn("A turnaround shows consistent front, side and back views", identity_prompt)
        turnaround_asset = {
            **identity_asset,
            "asset_id": "char_c1_turnaround",
            "asset_type": "character_turnaround",
            "phase": "character_turnaround",
        }
        turnaround_prompt = _asset_prompt(turnaround_asset, _storyboard(), 1)
        self.assertIn("A turnaround shows consistent front, side and back views", turnaround_prompt)

        prop_asset = {
            "asset_id": "prop_p1", "asset_type": "key_prop", "phase": "prop",
            "label": "key prop", "spec": {"prop_id": "p1", "physical_spec": "side notch"},
        }
        prop_prompt = _asset_prompt(prop_asset, _storyboard(), 1)
        self.assertIn("exactly one complete key prop", prop_prompt)
        self.assertIn("exactly one canonical three-quarter view", prop_prompt)
        self.assertIn("no color or state sequence", prop_prompt)
        self.assertNotIn("reference from useful angles", prop_prompt)

        storyboard_with_prop = _storyboard()
        shot_visual = storyboard_with_prop["scenes"][0]["shots"][0]["visual"]
        shot_visual["key_props"] = [{"prop_id": "p1", "name": "fixture"}]
        storyboard_with_prop["scenes"][0]["shots"][0]["visible_prop_ids"] = ["p1"]
        inventory = _build_inventory(storyboard_with_prop)
        planned_prop = next(
            asset for asset in _build_foundation_assets(storyboard_with_prop, inventory)
            if asset["asset_type"] == "key_prop"
        )
        self.assertEqual(
            planned_prop["reference_contract"]["role"], "canonical_geometry_anchor"
        )
        self.assertEqual(
            planned_prop["reference_contract"]["dynamic_state_source"], "shot_prompt"
        )

        image = MockImageProvider()
        first = self._run(image, MockVisionProvider(), foundation_candidates=1)
        self._approve(first)
        generated = self._run(image, MockVisionProvider(), foundation_candidates=1)
        self.assertEqual(generated["stop_reason"], "foundation_lock_style")
        state = self._read_json(os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", generated["run_id"], "state.json"
        ))
        candidate = state["candidates"]["style_001"][0]
        metadata_path = os.path.join(self.output_dir, candidate["path"] + ".manju.json")
        metadata = self._read_json(metadata_path)["foundation_image"]
        self.assertEqual(metadata["original_size"], [32, 32])
        self.assertEqual(metadata["actual_size"], [32, 32])
        self.assertEqual(metadata["method"], "provider_output_unmodified")
        self.assertEqual(len(metadata["file_sha256"]), 64)

    def test_key_prop_lock_requires_explicit_canonical_contract_check(self):
        pending = {
            "stage": "foundation_lock_prop",
            "item_ids": ["prop_p1"],
            "reference_contracts": {
                "prop_p1": {"role": "canonical_geometry_anchor"},
            },
        }
        decision = {
            "reviewer": "Human Reviewer",
            "reviewed_item_ids": ["prop_p1"],
            "selections": {"prop_p1": "prop_p1_r01_c01"},
            "reference_contract_checks": {},
        }
        with self.assertRaisesRegex(ValueError, "requires a contract check"):
            _validate_human_decision(pending, decision, "approve")
        decision["reference_contract_checks"] = {
            "prop_p1": {
                "candidate_id": "prop_p1_r01_c01",
                "single_object": True,
                "single_view": True,
                "clean_background": True,
                "no_grid_or_state_sequence": True,
            },
        }
        _validate_human_decision(pending, decision, "approve")

    def test_asset_preflight_blocks_offscreen_cast_and_prop_binding_before_paid_calls(self):
        storyboard = _storyboard()
        storyboard["creative_bible"]["characters"].append({
            "character_id": "c2", "name": "阿哲", "name_en": "Azhe",
            "anchor_description": "短发，灰色夹克",
        })
        shot = storyboard["scenes"][0]["shots"][0]
        shot["visual"]["description"] = "阿宁看向画外的阿哲"
        shot["visual"]["visible_character_ids"] = ["c1", "c2"]
        shot["visual"]["key_props"] = [{"prop_id": "p1", "name": "包裹"}]
        shot["visible_prop_ids"] = []
        issues = _storyboard_asset_preflight(storyboard)
        self.assertEqual(
            {item["category"] for item in issues},
            {"visible_entity_consistency", "asset_binding"},
        )
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(storyboard, handle, ensure_ascii=False)
        image = MockImageProvider()
        manifest = self._run(image, MockVisionProvider())
        self.assertEqual(manifest["status"], "needs_review")
        self.assertEqual(manifest["stop_reason"], "storyboard_asset_binding_invalid")
        self.assertEqual(image.calls, [])
        self.assertTrue(manifest["quality_gate"]["unverified_checks"])

    def test_shot_prop_cannot_self_authorize_outside_scene_registry(self):
        storyboard = _storyboard()
        storyboard["scenes"][0]["key_props"] = [
            {"prop_id": "canonical_letter", "name": "letter"},
        ]
        shot = storyboard["scenes"][0]["shots"][0]
        shot["visible_prop_ids"] = ["invented_letter"]
        shot["visual"]["key_props"] = [
            {"prop_id": "invented_letter", "name": "letter"},
        ]
        issues = _storyboard_asset_preflight(storyboard)
        unknown = [item for item in issues if item.get("problem") == "visible_prop_ids contains unknown prop IDs"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["invalid_ids"], ["invented_letter"])

    def test_asset_taxonomy_deduplicates_wardrobe_and_set_pieces(self):
        storyboard = _storyboard()
        scene = storyboard["scenes"][0]
        scene["key_props"] = [
            {"prop_id": "coat", "name": "coat", "asset_kind": "wardrobe"},
            {"prop_id": "door", "name": "door", "asset_kind": "set_piece",
             "physical_spec": {"construction": "solid", "motion": "hinged"}},
            {"prop_id": "letter", "name": "letter", "asset_kind": "portable_prop"},
        ]
        inventory = _build_inventory(storyboard)
        assets = _build_foundation_assets(storyboard, inventory)
        prop_assets = [item for item in assets if item["asset_type"] == "key_prop"]
        self.assertEqual([item["spec"]["prop_id"] for item in prop_assets], ["letter"])
        location = next(item for item in assets if item["asset_type"] == "location_master")
        self.assertEqual(location["spec"]["required_set_pieces"][0]["prop_id"], "door")

    def test_shot_sidecar_is_self_describing(self):
        image = MockImageProvider()
        manifest = self._drive(image, MockVisionProvider(), foundation_candidates=1)
        plan = self._read_json(os.path.join(self.output_dir, "visual_plan.json"))
        shot_id = plan["scene_groups"][0]["shot_ids"][0]
        review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        relative = review["scene_groups"]["scene_1"]["generated"][shot_id]
        metadata = self._read_json(os.path.join(self.output_dir, relative + ".manju.json"))
        production = metadata["production"]
        self.assertEqual(production["shot_id"], shot_id)
        self.assertEqual(production["group_id"], "scene_1")
        self.assertTrue(production["logical_job_id"])
        self.assertTrue(production["ledger_job_id"])
        self.assertEqual(production["visible_character_ids"], ["c1"])
        self.assertEqual(manifest["status"], "completed")

    def test_paid_ledger_adopts_started_job_with_complete_file_without_provider_call(self):
        state = {
            "run_id": "ledger-test", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
        }
        grant_id = _register_approval_grant(state, {
            "request_id": "grant_1", "stage": "test_cost", "state_fingerprint": "fp",
            "maximum_paid_calls": 1,
        })
        target = os.path.join(self.output_dir, "paid.png")
        provider = MockImageProvider()
        job = {
            "job_id": "stable-job", "prompt": "generic asset", "output_path": target,
            "references": [], "size": "1024x1024",
        }
        first = _run_paid_image_jobs(state, provider, [job], 1, grant_id)
        self.assertTrue(first[0]["result"])
        second = _run_paid_image_jobs(state, provider, [job], 1, grant_id)
        self.assertTrue(second[0]["recovered"])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(state["counters"]["image_calls"], 1)

    def test_paid_attempt_uses_unique_path_and_publishes_atomically(self):
        state = {
            "run_id": "atomic-publish", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
        }
        grant_id = _register_approval_grant(state, {
            "request_id": "grant_atomic", "stage": "test_cost",
            "state_fingerprint": "fp", "maximum_paid_calls": 1,
        })
        target = os.path.join(self.output_dir, "published.png")
        provider = MockImageProvider()
        job = {
            "job_id": "atomic-job", "prompt": "generic asset", "output_path": target,
            "references": [], "size": "1024x1024",
        }

        result = _run_paid_image_jobs(state, provider, [job], 1, grant_id)[0]
        published = result["result"]
        self.assertEqual(published, target)
        self.assertTrue(os.path.isfile(target))
        ledger = _load_paid_ledger(state)
        entry = ledger["jobs"][result["ledger_job_id"]]
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["artifact_binding_version"], 2)
        self.assertNotIn(f"{os.sep}.attempts{os.sep}", published)
        entry = _load_paid_ledger(state)["jobs"][result["ledger_job_id"]]
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["published_file_sha256"], hashlib.sha256(
            Path(target).read_bytes()
        ).hexdigest())

    def test_reconcile_finishes_fingerprint_bound_interrupted_publication(self):
        state = {
            "run_id": "publishing-reconcile", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [], "group_states": {},
        }
        published = os.path.join(self.output_dir, "published.png")
        Image.new("RGB", (32, 32), (70, 80, 90)).save(published)
        fingerprint = hashlib.sha256(Path(published).read_bytes()).hexdigest()
        ledger = _load_paid_ledger(state)
        ledger["jobs"] = {"publishing_job": {
            "ledger_job_id": "publishing_job", "logical_job_id": "group:scene_1:v001:1.1",
            "grant_id": "g1", "status": "publishing", "artifact_binding_version": 2,
            "output_path": "published.png", "attempt_output_path": ".attempts/job.png",
            "published_output_path": "published.png", "publish_expected_sha256": fingerprint,
            "file_sha256": fingerprint,
        }}
        _save_paid_ledger(state, ledger)

        result = _reconcile_durable_progress(state)
        entry = _load_paid_ledger(state)["jobs"]["publishing_job"]
        self.assertEqual(result["recovered_jobs"], 1)
        self.assertEqual(entry["status"], "succeeded")
        self.assertTrue(entry["recovered_from_file"])
        self.assertEqual(entry["published_file_sha256"], fingerprint)

    def test_reconcile_does_not_adopt_ambiguous_legacy_shared_output(self):
        state = {
            "run_id": "ambiguous-reconcile", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [], "group_states": {},
        }
        target = os.path.join(self.output_dir, "shared.png")
        Image.new("RGB", (32, 32), (10, 20, 30)).save(target)
        relative = os.path.relpath(target, self.output_dir)
        ledger = _load_paid_ledger(state)
        ledger["jobs"] = {
            "old_started": {
                "ledger_job_id": "old_started", "logical_job_id": "retry:scene_1:r01:1.3",
                "grant_id": "g1", "status": "started", "output_path": relative,
            },
            "new_success": {
                "ledger_job_id": "new_success", "logical_job_id": "retry:scene_1:r01:1.3",
                "grant_id": "g2", "status": "succeeded", "output_path": relative,
            },
        }
        _save_paid_ledger(state, ledger)

        result = _reconcile_durable_progress(state)
        reconciled = _load_paid_ledger(state)
        self.assertEqual(result["recovered_jobs"], 0)
        self.assertEqual(reconciled["jobs"]["old_started"]["status"], "uncertain")
        self.assertNotIn("recovered_from_file", reconciled["jobs"]["old_started"])

    def test_cross_grant_retry_cannot_publish_into_an_inflight_attempt(self):
        state = {
            "run_id": "cross-grant", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [], "group_states": {},
            "target_aspect_ratio": 1.0, "aspect_mode": "cover",
        }
        first_grant = _register_approval_grant(state, {
            "request_id": "grant_one", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "fp1", "maximum_paid_calls": 1,
        })
        second_grant = _register_approval_grant(state, {
            "request_id": "grant_two", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "fp2", "maximum_paid_calls": 1,
        })
        logical_path = os.path.join(self.output_dir, "shot_retry01.png")
        old_key = "old_inflight"
        old_attempt = os.path.join(self.output_dir, ".attempts", "old_inflight.png")
        ledger = _load_paid_ledger(state)
        ledger["grants"][first_grant]["used_calls"] = 1
        ledger["jobs"][old_key] = {
            "ledger_job_id": old_key,
            "logical_job_id": "retry:scene_1:r01:1.3",
            "grant_id": first_grant, "status": "started",
            "output_path": os.path.relpath(logical_path, self.output_dir),
            "attempt_output_path": os.path.relpath(old_attempt, self.output_dir),
            "published_output_path": "", "artifact_binding_version": 2,
        }
        _save_paid_ledger(state, ledger)
        provider = MockImageProvider()
        job = {
            "job_id": "retry:scene_1:r01:1.3", "prompt": "generic retry",
            "output_path": logical_path, "references": [], "size": "1024x1024",
        }

        result = _run_paid_image_jobs(state, provider, [job], 1, second_grant)[0]
        self.assertNotEqual(os.path.abspath(result["result"]), os.path.abspath(old_attempt))
        reconciliation = _reconcile_durable_progress(state)
        reconciled = _load_paid_ledger(state)
        self.assertEqual(reconciliation["recovered_jobs"], 0)
        self.assertEqual(reconciled["jobs"][old_key]["status"], "uncertain")
        self.assertNotIn("recovered_from_file", reconciled["jobs"][old_key])
        self.assertEqual(reconciled["jobs"][result["ledger_job_id"]]["status"], "succeeded")

    def test_reconcile_never_promotes_failed_job_with_reused_output_file(self):
        state = {
            "run_id": "failed-reconcile", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [], "group_states": {},
        }
        target = os.path.join(self.output_dir, "retry05.png")
        Image.new("RGB", (32, 32), (10, 20, 30)).save(target)
        ledger = _load_paid_ledger(state)
        ledger["jobs"] = {
            "failed_job": {
                "ledger_job_id": "failed_job",
                "logical_job_id": "retry:scene_1:r05:1.3",
                "grant_id": "g1", "status": "failed",
                "output_path": os.path.relpath(target, self.output_dir),
                "prompt_fingerprint": "abc",
                "error": "provider returned no valid image",
            },
        }
        _save_paid_ledger(state, ledger)
        result = _reconcile_durable_progress(state)
        self.assertEqual(result["recovered_jobs"], 0)
        self.assertEqual(_load_paid_ledger(state)["jobs"]["failed_job"]["status"], "failed")

    def test_revision_history_is_reconciled_from_ledger_contracts(self):
        state = {
            "run_id": "history-reconcile", "output_dir": self.output_dir,
            "group_states": {"scene_1": {"revision_attempt_history": [{
                "ledger_job_id": "job_a", "provider_outcome": "not_started",
                "revision_attempt_number": 99, "correction_contract_id": "wrong",
            }]}},
        }
        ledger = {"jobs": {
            "job_a": {
                "ledger_job_id": "job_a", "logical_job_id": "retry:scene_1:r01:1.3",
                "group_id": "scene_1", "shot_id": "1.3", "grant_id": "g1",
                "status": "succeeded", "revision_attempt_number": 1,
                "correction_contract_id": "constraint_a",
                "published_output_path": "shot.png", "published_file_sha256": "sha-a",
            },
            "job_b": {
                "ledger_job_id": "job_b", "logical_job_id": "retry:scene_1:r01:1.3",
                "group_id": "scene_1", "shot_id": "1.3", "grant_id": "g2",
                "status": "failed", "revision_attempt_number": 1,
                "correction_contract_id": "constraint_b", "error": "provider failed",
            },
        }}

        _reconcile_revision_attempt_history(state, ledger)
        history = {
            item["ledger_job_id"]: item
            for item in state["group_states"]["scene_1"]["revision_attempt_history"]
        }
        self.assertEqual(history["job_a"]["provider_outcome"], "succeeded")
        self.assertEqual(history["job_a"]["revision_attempt_number"], 1)
        self.assertEqual(history["job_a"]["correction_contract_id"], "constraint_a")
        self.assertEqual(history["job_b"]["provider_outcome"], "failed")
        summary = _revision_attempt_summary(ledger)
        self.assertEqual(summary["duplicate_logical_attempts"], 1)
        self.assertEqual(summary["lineage_conflict_count"], 1)

    def test_normal_resume_retargets_relocated_output_without_writing_source(self):
        first = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            foundation_candidates=1, supervisor_provider=_supervisor,
            image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
        )
        source_state_path = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", first["run_id"], "state.json",
        )
        source_state_before = Path(source_state_path).read_bytes()
        with tempfile.TemporaryDirectory() as relocated_root:
            relocated_output = os.path.join(relocated_root, "copied_visual_output")
            shutil.copytree(self.output_dir, relocated_output)
            relocated_storyboard = os.path.join(relocated_output, "storyboard.json")
            resumed = run_image_agent(
                relocated_storyboard, relocated_output, execute_paid_calls=False,
                foundation_candidates=1, supervisor_provider=_supervisor,
                image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
            )
            relocated_state = self._read_json(os.path.join(
                relocated_output, "stages", "visual_agent", "runs",
                resumed["run_id"], "state.json",
            ))
            self.assertEqual(relocated_state["output_dir"], os.path.abspath(relocated_output))
            self.assertEqual(
                relocated_state["storyboard_path"], os.path.abspath(relocated_storyboard)
            )
        self.assertEqual(Path(source_state_path).read_bytes(), source_state_before)

    def test_invocation_lease_rejects_same_process_and_cross_process_resume(self):
        with _visual_invocation_lease(self.output_dir):
            with self.assertRaisesRegex(RuntimeError, "visual_agent_run_already_active"):
                run_image_agent(self.storyboard_path, self.output_dir, execute_paid_calls=False)

        script = """
import sys
from manju.pipeline.visual_agent import _visual_invocation_lease
with _visual_invocation_lease(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, self.output_dir],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(RuntimeError, "visual_agent_run_already_active"):
                run_image_agent(self.storyboard_path, self.output_dir, execute_paid_calls=False)
        finally:
            if process.stdin:
                process.stdin.write("release\n")
                process.stdin.flush()
            process.wait(timeout=10)

    def test_resume_reconciles_partial_paid_batch_and_replays_lock_idempotently(self):
        state = {
            "run_id": "reconcile-test", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [{
                "asset_id": "char_c1_identity", "asset_type": "character_identity",
                "phase": "character_identity", "dependencies": [],
            }],
            "candidates": {}, "locked_assets": {},
        }
        grant_id = _register_approval_grant(state, {
            "request_id": "grant_1", "stage": "foundation_cost", "state_fingerprint": "fp",
            "maximum_paid_calls": 2,
        })
        candidate_id = "char_c1_identity_r01_c01"
        target = os.path.join(self.output_dir, "candidate.png")
        Image.new("RGB", (32, 32), (30, 60, 90)).save(target)
        ledger = _load_paid_ledger(state)
        ledger["grants"][grant_id]["used_calls"] = 2
        ledger["jobs"] = {
            "complete": {
                "ledger_job_id": "complete",
                "logical_job_id": f"foundation:{candidate_id}",
                "grant_id": grant_id, "status": "started",
                "output_path": os.path.relpath(target, self.output_dir),
                "prompt_fingerprint": "abc",
            },
            "uncertain": {
                "ledger_job_id": "uncertain",
                "logical_job_id": "foundation:char_c1_identity_r01_c02",
                "grant_id": grant_id, "status": "started",
                "output_path": "missing.png", "prompt_fingerprint": "def",
            },
        }
        _save_paid_ledger(state, ledger)
        result = _reconcile_durable_progress(state)
        self.assertEqual(result["recovered_jobs"], 1)
        self.assertEqual(result["uncertain_paid_jobs"], [
            "foundation:char_c1_identity_r01_c02",
        ])
        self.assertEqual(state["counters"]["image_calls"], 2)
        candidate = state["candidates"]["char_c1_identity"][0]
        self.assertTrue(candidate["recovered_from_ledger"])

        asset = state["foundation_assets"][0]
        first_lock = _lock_candidate(state, asset, candidate, "reviewed generic identity")
        state["locked_assets"] = {}
        replayed = _lock_candidate(state, asset, candidate, "reviewed generic identity")
        self.assertEqual(replayed["version"], first_lock["version"])
        self.assertEqual(replayed["path"], first_lock["path"])
        self.assertTrue(replayed["recovered_from_file"])

    def test_reconcile_publishes_v2_foundation_attempt_before_candidate_recovery(self):
        state = {
            "run_id": "v2-produced-recovery", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [{
                "asset_id": "prop_generic", "asset_type": "prop", "phase": "prop",
                "dependencies": [],
            }],
            "candidates": {}, "locked_assets": {}, "group_states": {},
        }
        candidate_id = "prop_generic_r01_c01"
        published = os.path.join(self.output_dir, "candidates", "candidate.png")
        attempt = os.path.join(self.output_dir, "candidates", ".attempts", "attempt.png")
        os.makedirs(os.path.dirname(attempt), exist_ok=True)
        Image.new("RGB", (48, 48), (20, 40, 60)).save(attempt)
        fingerprint = hashlib.sha256(Path(attempt).read_bytes()).hexdigest()
        ledger = _load_paid_ledger(state)
        ledger["jobs"] = {"job-v2": {
            "ledger_job_id": "job-v2",
            "logical_job_id": f"foundation:{candidate_id}",
            "grant_id": "grant", "status": "produced", "artifact_binding_version": 2,
            "output_path": os.path.relpath(published, self.output_dir),
            "attempt_output_path": os.path.relpath(attempt, self.output_dir),
            "published_output_path": "", "file_sha256": fingerprint,
            "prompt_fingerprint": "prompt-fingerprint",
        }}
        _save_paid_ledger(state, ledger)

        result = _reconcile_durable_progress(state)

        entry = _load_paid_ledger(state)["jobs"]["job-v2"]
        candidate = state["candidates"]["prop_generic"][0]
        self.assertEqual(result["recovered_jobs"], 1)
        self.assertEqual(entry["status"], "succeeded")
        self.assertTrue(os.path.isfile(published))
        self.assertFalse(os.path.exists(attempt))
        self.assertEqual(candidate["path"], os.path.relpath(published, self.output_dir))
        self.assertEqual(candidate["ledger_job_id"], "job-v2")
        locked = _lock_candidate(
            state, state["foundation_assets"][0], candidate, "reviewed generic prop",
        )
        self.assertEqual(locked["status"], "locked")

    def test_unpublished_attempt_candidate_cannot_be_locked(self):
        attempt = os.path.join(self.output_dir, "candidates", ".attempts", "candidate.png")
        os.makedirs(os.path.dirname(attempt), exist_ok=True)
        Image.new("RGB", (32, 32), (1, 2, 3)).save(attempt)
        state = {"run_id": "lock-gate", "output_dir": self.output_dir}
        asset = {"asset_id": "prop_generic", "asset_type": "prop", "dependencies": []}
        candidate = {
            "candidate_id": "prop_generic_r01_c01",
            "path": os.path.relpath(attempt, self.output_dir),
        }
        with self.assertRaisesRegex(ValueError, "unpublished paid attempt"):
            _lock_candidate(state, asset, candidate, "reviewed generic prop")

    def test_parallel_paid_result_is_published_while_sibling_is_still_running(self):
        state = {
            "run_id": "incremental-publish", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
        }
        grant_id = _register_approval_grant(state, {
            "request_id": "grant_parallel", "stage": "test_cost",
            "state_fingerprint": "fp", "maximum_paid_calls": 2,
        })
        slow_release = threading.Event()
        fast_returned = threading.Event()

        def provider(prompt, output_path, _references, _size):
            if prompt == "slow":
                slow_release.wait()
            Image.new("RGB", (32, 32), (10, 20, 30)).save(output_path)
            if prompt == "fast":
                fast_returned.set()
            return output_path

        fast_target = os.path.join(self.output_dir, "fast.png")
        slow_target = os.path.join(self.output_dir, "slow.png")
        outcome = []
        runner = threading.Thread(target=lambda: outcome.extend(_run_paid_image_jobs(
            state, provider, [
                {"job_id": "slow-job", "prompt": "slow", "output_path": slow_target,
                 "references": [], "size": "32x32"},
                {"job_id": "fast-job", "prompt": "fast", "output_path": fast_target,
                 "references": [], "size": "32x32"},
            ], 2, grant_id,
        )))
        runner.start()
        try:
            self.assertTrue(fast_returned.wait(timeout=5))
            deadline = time.time() + 15
            fast_entry = {}
            while time.time() < deadline:
                matches = [
                    item for item in _load_paid_ledger(state)["jobs"].values()
                    if item["logical_job_id"] == "fast-job"
                ]
                fast_entry = matches[0] if matches else {}
                if os.path.isfile(fast_target) and fast_entry.get("status") == "succeeded":
                    break
                time.sleep(0.01)
            self.assertTrue(runner.is_alive())
            self.assertTrue(os.path.isfile(fast_target))
            self.assertEqual(fast_entry["status"], "succeeded")
        finally:
            slow_release.set()
            runner.join(timeout=10)
        self.assertEqual(len(outcome), 2)

    def test_exhausted_foundation_grant_transitions_to_one_call_retry(self):
        state = {
            "run_id": "exhausted-transition", "output_dir": self.output_dir,
            "status": "needs_review", "stop_reason": "supervisor_stopped:grant_exhausted",
            "stage": "foundation_generate", "foundation_phase_index": 5,
            "foundation_budget_approved": True, "foundation_grant_id": "grant_main",
            "foundation_primary_grant_id": "grant_main", "foundation_retry_grant_id": "",
            "foundation_assets": [{
                "asset_id": "prop_generic", "asset_type": "prop", "phase": "prop",
                "dependencies": [],
            }],
            "budgets": {"foundation_candidates": 3}, "candidates": {"prop_generic": []},
            "locked_assets": {}, "group_states": {}, "pending_approval": {},
            "counters": {"image_calls": 0},
        }
        ledger = _load_paid_ledger(state)
        ledger["grants"] = {"grant_main": {
            "grant_id": "grant_main", "stage": "foundation_cost",
            "state_fingerprint": "fp", "maximum_paid_calls": 3, "used_calls": 3,
        }}
        for number in (1, 2):
            candidate_id = f"prop_generic_r01_c{number:02d}"
            path = os.path.join(self.output_dir, f"candidate-{number}.png")
            Image.new("RGB", (32, 32), (number, 2, 3)).save(path)
            fingerprint = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            key = f"job-{number}"
            ledger["jobs"][key] = {
                "ledger_job_id": key, "logical_job_id": f"foundation:{candidate_id}",
                "grant_id": "grant_main", "status": "succeeded", "artifact_binding_version": 2,
                "output_path": os.path.relpath(path, self.output_dir),
                "published_output_path": os.path.relpath(path, self.output_dir),
                "published_file_sha256": fingerprint,
            }
            state["candidates"]["prop_generic"].append({
                "candidate_id": candidate_id, "asset_id": "prop_generic",
                "round": 1, "number": number,
                "path": os.path.relpath(path, self.output_dir), "ledger_job_id": key,
            })
        _save_paid_ledger(state, ledger)

        transition = _normalize_recoverable_paid_state(state)
        self.assertEqual(transition["missing_paid_calls"], 1)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["stage"], "foundation_retry_approval")
        approval = _tool_request_foundation_approval(state)
        self.assertEqual(approval["maximum_paid_calls"], 1)

    def test_superseded_uncertain_jobs_are_historical_not_actionable(self):
        state = {
            "run_id": "uncertain-classification", "output_dir": self.output_dir,
            "foundation_assets": [], "group_states": {}, "counters": {"image_calls": 0},
        }
        ledger = _load_paid_ledger(state)
        ledger["jobs"] = {
            "historical": {
                "ledger_job_id": "historical", "logical_job_id": "foundation:old_r01_c01",
                "status": "uncertain", "lifecycle_status": "superseded",
            },
            "current": {
                "ledger_job_id": "current", "logical_job_id": "foundation:new_r01_c01",
                "status": "uncertain",
            },
        }
        _save_paid_ledger(state, ledger)
        result = _reconcile_durable_progress(state)
        self.assertEqual(result["actionable_uncertain_paid_jobs"], [
            "foundation:new_r01_c01",
        ])
        self.assertEqual(result["superseded_uncertain_paid_jobs"], [
            "foundation:old_r01_c01",
        ])

    def test_paid_failure_never_exceeds_current_approval_grant(self):
        class FailingProvider:
            def __init__(self):
                self.calls = 0

            def __call__(self, prompt, output_path, references, size):
                self.calls += 1
                raise OSError("provider unavailable")

        provider = FailingProvider()
        first = self._run(provider, MockVisionProvider(), foundation_candidates=3)
        self._approve(first)
        failed = self._run(provider, MockVisionProvider(), foundation_candidates=3)
        self.assertEqual(failed["status"], "awaiting_approval")
        cost = self._read_json(os.path.join(self.output_dir, "cost_plan.json"))
        self.assertLessEqual(cost["used_paid_calls"], cost["approved_paid_calls"])
        self.assertEqual(cost["used_paid_calls"], provider.calls)
        self.assertEqual(provider.calls, 3)

    def test_cover_aspect_policy_produces_no_padding_bands(self):
        path = os.path.join(self.output_dir, "square.png")
        Image.new("RGB", (100, 100), (20, 80, 140)).save(path)
        details = _normalize_shot_canvas(path, 9 / 16, "cover")
        self.assertEqual(details["method"], "cover_center_crop")
        self.assertEqual(details["padding_fraction"], 0.0)
        with Image.open(path) as image:
            self.assertAlmostEqual(image.width / image.height, 9 / 16, places=2)
            self.assertEqual(image.getpixel((0, 0)), (20, 80, 140))
            self.assertEqual(image.getpixel((image.width - 1, image.height - 1)), (20, 80, 140))

    def test_three_invalid_tool_choices_stop_safely(self):
        def invalid_supervisor(_snapshot):
            return {"action": "finalize", "params": {}, "summary": "too early"}

        manifest = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            supervisor_provider=invalid_supervisor,
            image_provider=MockImageProvider(), vision_provider=MockVisionProvider(),
        )
        self.assertEqual(manifest["status"], "needs_review")
        self.assertEqual(manifest["stop_reason"], "three_invalid_tool_actions")

    def test_trace_and_sqlite_do_not_contain_api_keys(self):
        secret = "super-secret-visual-key"
        with patch.dict(os.environ, {
            "LLM_API_KEY": secret, "LLM_API_BASE": "https://example.invalid/v1", "LLM_MODEL": "mock",
            "MANJU_IMAGE_API_KEY": secret, "MANJU_IMAGE_API_BASE": "https://example.invalid/v1",
            "MANJU_IMAGE_MODEL": "mock-image", "MANJU_VISION_API_KEY": secret,
            "MANJU_VISION_API_BASE": "https://example.invalid/v1", "MANJU_VISION_MODEL": "mock-vision",
        }, clear=False):
            self._run(MockImageProvider(), MockVisionProvider())
        for root, _dirs, files in os.walk(self.output_dir):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    self.assertNotIn(secret.encode(), handle.read(), os.path.join(root, name))

    def test_no_character_story_does_not_invent_character_assets(self):
        storyboard = normalize_storyboard({
            "title": "空景", "creative_bible": {
                "style_anchor": "ink wash", "aspect_ratio": "9:16", "characters": [],
            },
            "scenes": [{
                "scene_id": "1", "heading": "EXT. 山谷 - 黎明", "purpose": "建立环境",
                "visual_mood": "安静", "continuity": {}, "shots": [{
                    "shot_id": "1.1", "duration_seconds": 3,
                    "visual": {"shot_type": "远景", "composition": "纵深", "composition_emotion": "",
                               "camera_movement": "固定", "description": "雾从山谷升起",
                               "color_tone": "青灰", "visible_character_ids": []},
                    "audio": {}, "prompts": {"image_cn": "山谷晨雾", "image_en": "misty valley", "video": ""},
                }],
            }],
        })
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(storyboard, handle, ensure_ascii=False)
        manifest = self._run(MockImageProvider(), MockVisionProvider())
        self.assertEqual(manifest["stop_reason"], "foundation_cost")
        plan = self._read_json(os.path.join(self.output_dir, "visual_plan.json"))
        types = {item["asset_type"] for item in plan["foundation_assets"]}
        self.assertNotIn("character_identity", types)
        self.assertEqual(types, {"style_board", "location_master"})

    def test_pipeline_awaiting_visual_approval_stops_before_media(self):
        manifest = {
            "status": "awaiting_approval", "stop_reason": "foundation_cost",
            "pending_approval": {},
        }
        with patch("manju.pipeline.visual_agent.run_image_agent", return_value=manifest), patch(
            "manju.cli.run_voice"
        ) as voice, patch("manju.cli.run_video") as video:
            result = CliRunner().invoke(cli, [
                "pipeline", "--storyboard-json", self.storyboard_path,
                "--image-engine", "agent", "--no-voice", "--no-video",
            ])
        self.assertEqual(result.exit_code, 3, result.output)
        voice.assert_not_called()
        video.assert_not_called()

    def test_scale_contract_extraction_is_generic_and_source_bound(self):
        chinese = _declared_scale_contract({"physical_spec": "装置只有硬币大小，边缘有缺口"})
        english = _declared_scale_contract({"description": "a coin-sized sealed module"})
        metric = _declared_scale_contract({"dimensions": ["12 mm wide", "4 mm thick"]})
        unspecified = _declared_scale_contract({"physical_spec": "dark metal with a side notch"})

        self.assertTrue(chinese["required"])
        self.assertTrue(any("大小" in cue for cue in chinese["source_cues"]))
        self.assertTrue(english["required"])
        self.assertTrue(any("coin-sized" in cue for cue in english["source_cues"]))
        self.assertTrue(metric["required"])
        self.assertTrue(any("12 mm" in cue for cue in metric["source_cues"]))
        self.assertFalse(unspecified["required"])

    def test_scale_bound_prop_prompt_and_lock_require_visible_scale_evidence(self):
        scale_contract = _declared_scale_contract({"physical_spec": "coin-sized module"})
        asset = {
            "asset_id": "prop_generic", "asset_type": "key_prop", "phase": "prop",
            "label": "generic key prop", "spec": {"physical_spec": "coin-sized module"},
            "reference_contract": {
                "role": "canonical_geometry_anchor", "scale_contract": scale_contract,
            },
        }
        prompt = _asset_prompt(asset, _storyboard(), 1, generation_round=2)
        self.assertIn("SOURCE SCALE CONTRACT", prompt)
        self.assertIn("complete, identifiable natural comparator", prompt)
        self.assertIn("exactly one instance of the key prop", prompt)
        self.assertIn("FOUNDATION RESET ROUND 2", prompt)
        self.assertIn("partial fingertip fragment", prompt)
        self.assertIn("same focal plane", prompt)

        pending = {
            "stage": "foundation_lock_prop", "item_ids": ["prop_generic"],
            "reference_contracts": {"prop_generic": asset["reference_contract"]},
        }
        decision = {
            "reviewer": "Human Scale Reviewer", "reviewed_item_ids": ["prop_generic"],
            "selections": {"prop_generic": "candidate_1"},
            "reference_contract_checks": {"prop_generic": {
                "candidate_id": "candidate_1", "single_object": True, "single_view": True,
                "clean_background": True, "no_grid_or_state_sequence": True,
            }},
        }
        with self.assertRaisesRegex(ValueError, "scale evidence"):
            _validate_human_decision(pending, decision, "approve")
        decision["reference_contract_checks"]["prop_generic"].update({
            "scale_evidence_present": True, "scale_relation_matches": True,
        })
        with self.assertRaisesRegex(ValueError, "complete, in-focus source scale evidence"):
            _validate_human_decision(pending, decision, "approve")
        decision["reference_contract_checks"]["prop_generic"].update({
            "scale_comparator_complete": True,
            "scale_comparator_in_focus": True,
            "scale_comparator_contact_or_shared_plane": True,
        })
        _validate_human_decision(pending, decision, "approve")

    def test_scale_evidence_prompt_is_generic_for_non_handheld_and_unscaled_props(self):
        large = {
            "asset_id": "prop_large", "asset_type": "key_prop", "phase": "prop",
            "label": "large measured prop", "spec": {"dimensions": "100 m wide"},
        }
        unscaled = {
            "asset_id": "prop_unscaled", "asset_type": "key_prop", "phase": "prop",
            "label": "unscaled prop", "spec": {"material": "dark metal"},
        }

        large_prompt = _asset_prompt(large, _storyboard(), 1, generation_round=2)
        unscaled_prompt = _asset_prompt(unscaled, _storyboard(), 1, generation_round=2)

        self.assertIn("For a larger or non-hand-held prop", large_prompt)
        self.assertIn("do not force the prop into a hand", large_prompt)
        self.assertNotIn("SOURCE SCALE CONTRACT", unscaled_prompt)
        self.assertNotIn("FOUNDATION RESET ROUND", unscaled_prompt)

    def test_shot_prompt_enforces_one_continuous_frame_and_source_scale(self):
        scale_contract = _declared_scale_contract({"physical_spec": "12 mm wide module"})
        state = {
            "locked_assets": {"prop_generic": {"version": 2}},
            "foundation_assets": [{
                "asset_id": "prop_generic", "asset_type": "key_prop",
                "spec": {"physical_spec": "12 mm wide module"},
                "reference_contract": {"scale_contract": scale_contract},
            }],
        }
        group = {"reference_asset_ids": ["prop_generic"]}
        shot = {
            "prompt": "show the module changing color", "description": "the module changes color",
            "visible_character_ids": [], "visible_prop_ids": ["generic"],
            "reference_asset_ids": ["prop_generic"],
        }
        prompt = _shot_prompt(state, group, shot)
        self.assertIn("SINGLE CONTINUOUS CAMERA FRAME", prompt)
        self.assertIn("triptych", prompt)
        self.assertIn("stacked panels", prompt)
        self.assertIn("12 mm", prompt)
        self.assertNotIn("distinct before/transition/after zones", prompt)

    def test_shared_exhausted_scale_asset_requires_foundation_reference_reset(self):
        def issue(shot_id):
            return {
                "issue_id": f"scale_{shot_id}", "shot_id": shot_id, "blocking": True,
                "category": "storyboard_execution", "correction_target": "prop_scale",
                "storyboard_path": f"$.scenes[0].shots[{shot_id}]",
                "reference_asset_ids": ["prop_generic"], "focus_asset_ids": ["prop_generic"],
                "problem": "declared scale is not visible", "instruction": "restore declared scale",
            }

        deferred = {
            "issue_id": "geometry_1.4", "shot_id": "1.4", "blocking": True,
            "category": "storyboard_execution", "correction_target": "prop_geometry",
            "storyboard_path": "$.scenes[0].shots[3]",
            "reference_asset_ids": ["prop_other"], "focus_asset_ids": ["prop_other"],
            "problem": "an unrelated locked geometry differs",
            "instruction": "restore the other locked geometry",
        }
        issues = [issue("1.1"), issue("1.2"), issue("1.3"), deferred]
        histories = []
        for current in (issues[0], issues[1], deferred):
            contract = _correction_contract("scene_1", current["shot_id"], [current])
            histories.append({
                "attempt_id": f"clean-{current['shot_id']}", "shot_id": current["shot_id"],
                "correction_contract_id": contract["correction_contract_id"],
                "strategy": "clean_regeneration", "provider_outcome": "succeeded",
                "artifact_path": f"shot_{current['shot_id']}.png",
            })
        group = {"group_id": "scene_1"}
        group_state = {"issues": issues, "revision_attempt_history": histories}
        state = {
            "run_id": "run", "output_dir": self.output_dir, "counters": {"image_calls": 20},
            "budgets": {"foundation_candidates": 3}, "scene_groups": [group],
            "group_states": {"scene_1": group_state}, "pending_approval": {"x": 1},
            "foundation_assets": [{
                "asset_id": "prop_generic", "asset_type": "key_prop", "phase": "prop",
                "reference_contract": {"scale_contract": _declared_scale_contract({
                    "physical_spec": "coin-sized module",
                })},
            }],
        }
        self.assertTrue(_apply_scene_convergence_gate(state, group, group_state, issues))
        self.assertEqual(state["stop_reason"], "foundation_reference_reset_required")
        reset = state["repair_plan"]["convergence"]["reference_reset"]
        self.assertEqual(reset["asset_ids"], ["prop_generic"])
        self.assertEqual(reset["exhausted_shot_ids"], ["1.1", "1.2", "1.4"])
        self.assertEqual(reset["affected_shot_ids"], ["1.1", "1.2", "1.3"])
        plan = state["repair_plan"]
        self.assertEqual(plan["deferred_blocking_shot_ids"], ["1.4"])
        self.assertEqual([item["issue_id"] for item in plan["deferred_issues"]], ["geometry_1.4"])
        self.assertTrue(plan["separate_repair_required"])
        self.assertEqual(plan["convergence"]["post_reset_review"]["scope"], "entire_scene_group")

    def test_prepare_foundation_reset_archives_only_target_scope(self):
        state = {
            "run_id": "run", "output_dir": self.output_dir,
            "foundation_assets": [
                {"asset_id": "prop_target", "asset_type": "key_prop", "phase": "prop"},
                {"asset_id": "prop_other", "asset_type": "key_prop", "phase": "prop"},
            ],
            "candidates": {
                "prop_target": [{"candidate_id": "old_target", "round": 1}],
                "prop_other": [{"candidate_id": "old_other", "round": 1}],
            },
            "rankings": {"prop_target": {"ranking": ["old_target"]}, "prop_other": {"ranking": ["old_other"]}},
            "locked_assets": {"prop_target": {"version": 1}, "prop_other": {"version": 1}},
            "scene_groups": [{
                "group_id": "scene_1", "shot_ids": ["1.1", "1.4"],
            }],
            "group_states": {"scene_1": {
                "retry_count": 4,
                "issues": [
                    {"issue_id": "target", "shot_id": "1.1", "blocking": True},
                    {"issue_id": "other", "shot_id": "1.4", "blocking": True},
                ],
                "revision_attempt_history": [
                    {"shot_id": "1.1", "attempt_id": "target",
                     "logical_job_id": "retry:scene_1:r07:1.1"},
                    {"shot_id": "1.4", "attempt_id": "other",
                     "logical_job_id": "retry:scene_1:r03:1.4"},
                ],
            }},
            "repair_plan": {
                "status": "foundation_reference_reset_required", "group_ids": ["scene_1"],
                "convergence": {"reference_reset": {
                    "asset_ids": ["prop_target"], "affected_shot_ids": ["1.1"],
                }},
            },
        }
        reset = _prepare_foundation_reference_reset(state, "test")
        self.assertEqual(reset["round_by_asset"], {"prop_target": 2})
        self.assertEqual(reset["shot_revision_round"], 8)
        self.assertEqual(state["candidates"]["prop_target"], [])
        self.assertEqual(state["candidates"]["prop_other"][0]["candidate_id"], "old_other")
        self.assertEqual(state["locked_assets"]["prop_other"]["version"], 1)
        self.assertEqual(state["foundation_candidate_history"]["prop_target"][0]["candidate_id"], "old_target")
        self.assertEqual(state["group_states"]["scene_1"]["retry_count"], 0)
        self.assertEqual(reset["deferred_blocking_shot_ids"], ["1.4"])
        self.assertEqual(state["repair_plan"]["deferred_issues"][0]["issue_id"], "other")
        self.assertTrue(state["repair_plan"]["separate_repair_required"])
        self.assertEqual(
            state["repair_plan"]["convergence"]["post_reset_review"]["shot_ids"],
            ["1.1", "1.4"],
        )
        self.assertEqual(
            [item["shot_id"] for item in state["group_states"]["scene_1"]["revision_attempt_history"]],
            ["1.4"],
        )

    def test_post_reset_repair_uses_monotonic_artifact_round_and_local_retry_budget(self):
        previous = os.path.join(self.output_dir, "previous_round_4.png")
        Image.new("RGB", (32, 48), "green").save(previous)
        previous_relative = os.path.relpath(previous, self.output_dir)
        issue = {
            "issue_id": "generic_blocker", "shot_id": "1.1", "blocking": True,
            "category": "prop_consistency", "correction_target": "prop_geometry",
            "storyboard_path": "$.scenes[0].shots[0]", "reference_asset_ids": [],
            "focus_asset_ids": [], "problem": "geometry differs",
            "instruction": "restore the locked geometry",
        }
        contract = _correction_contract("scene_1", "1.1", [issue])
        shot = {
            "shot_id": "1.1", "storyboard_path": "$.scenes[0].shots[0]",
            "visible_character_ids": [], "visible_prop_ids": [],
            "reference_asset_ids": [], "prompt": "generic scene",
            "description": "a referenced object in a scene",
        }
        group = {
            "group_id": "scene_1", "shot_ids": ["1.1"], "shots": [shot],
            "reference_asset_ids": [],
        }
        group_state = {
            "group_id": "scene_1", "status": "approved", "approved": True,
            "retry_count": 1, "generated": {"1.1": previous_relative},
            "issues": [issue], "pending_paid_operation": "retry",
            "grant_id": "post_reset_grant", "revision_attempt_history": [{
                "attempt_id": "round-4", "logical_job_id": "retry:scene_1:r04:1.1",
                "shot_id": "1.1", "correction_contract_id": contract["correction_contract_id"],
                "strategy": "canonical_prop_geometry", "provider_outcome": "succeeded",
                "artifact_path": previous_relative, "recorded_at": "2026-08-02T10:05:00+08:00",
            }],
        }
        state = {
            "run_id": "post-reset-round", "output_dir": self.output_dir,
            "storyboard_path": self.storyboard_path, "paid_authorized": True,
            "storyboard": _storyboard(), "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_1": group_state}, "foundation_assets": [],
            "locked_assets": {}, "provider_capabilities": {
                "reference_mode": "multi", "max_references": 4,
            },
            "foundation_reset": {
                "group_id": "scene_1", "affected_shot_ids": ["1.1"],
                "shot_revision_round": 4, "shot_retry_count": 1,
                "prepared_at": "2026-08-02T10:00:00+08:00",
            },
            "size": "1024x1536", "target_aspect_ratio": 9 / 16,
            "aspect_mode": "cover", "budgets": {
                "image_parallelism": 1, "max_auto_retries": 3,
            },
            "counters": {"image_calls": 0}, "approval_grants": {},
            "pending_approval": {}, "quality_gate": {}, "status": "running",
            "stage": "group_retry", "stop_reason": "",
        }
        _register_approval_grant(state, {
            "request_id": "post_reset_grant", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "post-reset", "maximum_paid_calls": 1,
        })
        provider = MockImageProvider()

        result = _tool_revise_scene_group(state, provider)

        self.assertEqual(result["retry_number"], 5)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("retry05", group_state["generated"]["1.1"])
        self.assertEqual(group_state["retry_count"], 2)
        self.assertEqual(state["foundation_reset"]["shot_retry_count"], 2)
        ledger = _load_paid_ledger(state)
        self.assertEqual(ledger["grants"]["post_reset_grant"]["used_calls"], 1)
        self.assertIn(
            "retry:scene_1:r05:1.1",
            {item["logical_job_id"] for item in ledger["jobs"].values()},
        )

    def test_reconcile_repairs_post_reset_revision_round_collision_without_calls(self):
        historical = os.path.join(self.output_dir, "historical_round_2.png")
        current = os.path.join(self.output_dir, "current_round_4.png")
        Image.new("RGB", (32, 48), "red").save(historical)
        Image.new("RGB", (32, 48), "blue").save(current)
        historical_relative = os.path.relpath(historical, self.output_dir)
        current_relative = os.path.relpath(current, self.output_dir)
        current_sha = hashlib.sha256(Path(current).read_bytes()).hexdigest()
        restored_issue = {
            "issue_id": "reviewed_round_4", "shot_id": "1.1", "blocking": True,
            "category": "prop_consistency", "problem": "geometry differs",
            "instruction": "restore geometry", "storyboard_path": "$.scenes[0].shots[0]",
        }
        state = {
            "run_id": "collision-reconcile", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "status": "awaiting_approval", "stage": "manual_review",
            "stop_reason": "manual_review_scene_1", "pending_approval": {"request_id": "stale"},
            "foundation_assets": [], "foundation_reset": {
                "group_id": "scene_1", "affected_shot_ids": ["1.1"],
                "shot_revision_round": 4, "prepared_at": "2026-08-02T10:00:00+08:00",
            },
            "group_states": {"scene_1": {
                "status": "revised", "approved": True, "grant_id": "unused_grant",
                "pending_paid_operation": "", "issues": [{"issue_id": "stale_round_2"}],
                "retry_count": 2, "generated": {"1.1": historical_relative},
                "review_history": [{
                    "reviewed_at": "2026-08-02T10:06:00+08:00",
                    "vision_available": True, "image_fingerprints": {"1.1": current_sha},
                    "issues": [restored_issue],
                }],
                "revision_attempt_history": [{
                    "attempt_id": "valid-round-4", "ledger_job_id": "new04",
                    "logical_job_id": "retry:scene_1:r04:1.1", "shot_id": "1.1",
                    "provider_attempted": True, "provider_outcome": "succeeded",
                    "artifact_path": current_relative, "artifact_sha256": current_sha,
                    "recorded_at": "2026-08-02T10:05:00+08:00",
                }, {
                    "attempt_id": "colliding-round-2", "ledger_job_id": "old02",
                    "logical_job_id": "retry:scene_1:r02:1.1", "shot_id": "1.1",
                    "provider_attempted": False, "provider_outcome": "recovered",
                    "artifact_path": historical_relative,
                    "recorded_at": "2026-08-02T10:10:00+08:00",
                }],
            }},
        }
        _save_paid_ledger(state, {
            "run_id": state["run_id"], "grants": {"unused_grant": {
                "grant_id": "unused_grant", "stage": "scene_group_cost_scene_1",
                "state_fingerprint": "approved", "maximum_paid_calls": 1,
                "used_calls": 0,
            }}, "jobs": {
                "old02": {
                    "ledger_job_id": "old02", "logical_job_id": "retry:scene_1:r02:1.1",
                    "group_id": "scene_1", "shot_id": "1.1", "status": "succeeded",
                    "started_at": "2026-08-02T09:00:00+08:00",
                    "output_path": historical_relative, "operation_kind": "shot_revision",
                },
                "new04": {
                    "ledger_job_id": "new04", "logical_job_id": "retry:scene_1:r04:1.1",
                    "group_id": "scene_1", "shot_id": "1.1", "status": "succeeded",
                    "started_at": "2026-08-02T10:01:00+08:00",
                    "output_path": current_relative, "operation_kind": "shot_revision",
                },
            },
        })
        approval_dir = os.path.join(
            self.output_dir, "approvals", state["run_id"]
        )
        os.makedirs(approval_dir, exist_ok=True)
        request = {
            "request_id": "unused_grant", "stage": "scene_group_cost_scene_1",
            "state_fingerprint": "approved", "item_ids": ["1.1"],
            "maximum_paid_calls": 1,
        }
        with open(
            os.path.join(approval_dir, "unused_grant.request.json"),
            "w", encoding="utf-8",
        ) as handle:
            json.dump(request, handle)

        result = _reconcile_durable_progress(state)

        repair = result["post_reset_revision_collision_repair"]
        self.assertEqual(repair["invalidated_attempt_count"], 1)
        group_state = state["group_states"]["scene_1"]
        self.assertEqual(group_state["generated"]["1.1"], current_relative)
        self.assertEqual(group_state["retry_count"], 1)
        self.assertEqual(state["foundation_reset"]["shot_retry_count"], 1)
        self.assertEqual(group_state["issues"], [restored_issue])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["stage"], "group_retry")
        self.assertEqual(state["pending_approval"], {})
        self.assertEqual(group_state["pending_paid_operation"], "retry")
        self.assertEqual(repair["resumed_unused_grant_id"], "unused_grant")
        self.assertTrue(repair["restored_approval_pointer"])
        self.assertEqual(
            self._read_json(os.path.join(self.output_dir, "approvals", "current.json")),
            request,
        )
        self.assertEqual(
            [item["logical_job_id"] for item in group_state["revision_attempt_history"]],
            ["retry:scene_1:r04:1.1"],
        )
        self.assertEqual(
            group_state["revision_attempt_history_archive"][-1]["invalidated_reason"],
            "post_foundation_reset_revision_round_collision",
        )
        self.assertEqual(state["counters"]["image_calls"], 2)

    def test_reconcile_recovers_only_active_foundation_reset_round(self):
        asset_id = "prop_generic"
        state = {
            "run_id": "round-reconcile", "output_dir": self.output_dir,
            "counters": {"image_calls": 0}, "approval_grants": {},
            "foundation_assets": [{
                "asset_id": asset_id, "asset_type": "key_prop", "phase": "prop",
                "dependencies": [], "spec": {"dimensions": "3.2 cm wide"},
            }],
            "candidates": {}, "locked_assets": {}, "group_states": {},
            "foundation_reset": {
                "asset_ids": [asset_id], "round_by_asset": {asset_id: 2},
                "status": "candidate_generation",
            },
        }
        ledger = _load_paid_ledger(state)
        for round_number in (1, 2):
            candidate_id = f"{asset_id}_r{round_number:02d}_c01"
            path = os.path.join(self.output_dir, f"{candidate_id}.png")
            Image.new("RGB", (32, 32), (20 * round_number, 40, 60)).save(path)
            relative = os.path.relpath(path, self.output_dir)
            ledger["jobs"][f"job-r{round_number}"] = {
                "ledger_job_id": f"job-r{round_number}",
                "logical_job_id": f"foundation:{candidate_id}",
                "grant_id": f"grant-r{round_number}", "status": "succeeded",
                "artifact_binding_version": 2,
                "output_path": relative, "published_output_path": relative,
                "published_file_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "prompt_fingerprint": f"prompt-r{round_number}",
            }
        _save_paid_ledger(state, ledger)

        result = _reconcile_durable_progress(state)

        self.assertEqual(result["recovered_candidates"], 1)
        self.assertEqual(
            [item["candidate_id"] for item in state["candidates"][asset_id]],
            ["prop_generic_r02_c01"],
        )
        persisted = _load_paid_ledger(state)
        self.assertEqual(persisted["jobs"]["job-r1"]["status"], "succeeded")
        self.assertNotEqual(
            persisted["jobs"]["job-r1"].get("lifecycle_status"), "superseded",
        )

    def test_reset_generation_ignores_published_prior_round_candidates(self):
        asset_id = "prop_generic"
        scale_contract = _declared_scale_contract({"dimensions": "3.2 cm wide"})
        asset = {
            "asset_id": asset_id, "asset_type": "key_prop", "phase": "prop",
            "label": "generic measured prop", "dependencies": [],
            "spec": {"dimensions": "3.2 cm wide"},
            "reference_contract": {
                "role": "canonical_geometry_anchor", "scale_contract": scale_contract,
            },
        }
        state = {
            "run_id": "round-generation", "output_dir": self.output_dir,
            "storyboard": _storyboard(), "foundation_assets": [asset],
            "foundation_phase_index": 0, "candidates": {asset_id: []},
            "locked_assets": {}, "group_states": {}, "approval_grants": {},
            "counters": {"image_calls": 0}, "paid_ledger": {},
            "budgets": {"foundation_candidates": 3, "image_parallelism": 1},
            "foundation_budget_approved": True, "paid_authorized": True,
            "size": "1024x1536",
            "foundation_reset": {
                "asset_ids": [asset_id], "round_by_asset": {asset_id: 2},
                "status": "candidate_approval",
            },
        }
        ledger = _load_paid_ledger(state)
        ledger["grants"] = {
            "primary": {
                "grant_id": "primary", "stage": "foundation_cost",
                "maximum_paid_calls": 3, "used_calls": 3,
            },
            "retry": {
                "grant_id": "retry", "stage": "foundation_retry_cost",
                "maximum_paid_calls": 3, "used_calls": 0,
            },
        }
        for number in range(1, 4):
            candidate_id = f"{asset_id}_r01_c{number:02d}"
            path = os.path.join(self.output_dir, f"{candidate_id}.png")
            Image.new("RGB", (32, 32), (number * 20, 40, 60)).save(path)
            relative = os.path.relpath(path, self.output_dir)
            ledger_job_id = f"old-{number}"
            ledger["jobs"][ledger_job_id] = {
                "ledger_job_id": ledger_job_id,
                "logical_job_id": f"foundation:{candidate_id}",
                "grant_id": "primary", "status": "succeeded",
                "artifact_binding_version": 2,
                "output_path": relative, "published_output_path": relative,
                "published_file_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "prompt_fingerprint": f"old-{number}",
            }
            state["candidates"][asset_id].append({
                "candidate_id": candidate_id, "asset_id": asset_id,
                "round": 1, "number": number, "path": relative,
                "ledger_job_id": ledger_job_id,
            })
        _save_paid_ledger(state, ledger)
        state.update({
            "foundation_grant_id": "retry",
            "foundation_primary_grant_id": "primary",
            "foundation_retry_grant_id": "retry",
        })
        provider = MockImageProvider()

        result = _tool_generate_foundation_candidates(state, provider)

        self.assertEqual(result["generated"], 3)
        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(all("FOUNDATION RESET ROUND 2" in call["prompt"] for call in provider.calls))
        self.assertTrue(all("partial fingertip fragment" in call["prompt"] for call in provider.calls))
        self.assertEqual(
            [item["candidate_id"] for item in state["candidates"][asset_id]],
            [
                "prop_generic_r02_c01",
                "prop_generic_r02_c02",
                "prop_generic_r02_c03",
            ],
        )
        persisted = _load_paid_ledger(state)
        self.assertEqual(persisted["grants"]["retry"]["used_calls"], 3)
        self.assertEqual(persisted["jobs"]["old-1"]["status"], "succeeded")
        self.assertNotEqual(
            persisted["jobs"]["old-1"].get("lifecycle_status"), "superseded",
        )

    def test_migrated_scale_contract_is_persisted_and_enforced_at_lock(self):
        asset = {
            "asset_id": "prop_measured", "asset_type": "key_prop", "phase": "prop",
            "label": "measured prop", "dependencies": [],
            "spec": {"dimensions": "3.2 cm wide"},
            "reference_contract": {"role": "canonical_geometry_anchor"},
        }
        state = {
            "run_id": "contract-backfill", "output_dir": self.output_dir,
            "foundation_assets": [asset],
            "visual_bible": {"asset_specs": [json.loads(json.dumps(asset))]},
            "foundation_phase_index": 0,
            "candidates": {"prop_measured": [{
                "candidate_id": "prop_measured_r02_c01", "path": "missing.png",
            }]},
            "rankings": {"prop_measured": {"ranking": ["prop_measured_r02_c01"]}},
            "counters": {"image_calls": 0}, "approval_grants": {},
        }

        updated = _backfill_foundation_reference_contracts(state)
        _tool_request_foundation_lock(state)

        self.assertEqual(updated, ["prop_measured"])
        contract = state["pending_approval"]["reference_contracts"]["prop_measured"]
        self.assertTrue(contract["scale_contract"]["required"])
        self.assertTrue(
            state["visual_bible"]["asset_specs"][0]["reference_contract"]
            ["scale_contract"]["required"]
        )
        decision = {
            "reviewer": "Human Scale Reviewer",
            "reviewed_item_ids": ["prop_measured"],
            "selections": {"prop_measured": "prop_measured_r02_c01"},
            "reference_contract_checks": {"prop_measured": {
                "candidate_id": "prop_measured_r02_c01",
                "single_object": True, "single_view": True,
                "clean_background": True, "no_grid_or_state_sequence": True,
            }},
        }
        with self.assertRaisesRegex(ValueError, "scale evidence"):
            _validate_human_decision(state["pending_approval"], decision, "approve")

    def test_paid_trace_accounting_stays_on_retry_grant_during_handoff(self):
        state = {
            "run_id": "grant-trace", "output_dir": self.output_dir,
            "counters": {"image_calls": 45}, "approval_grants": {},
        }
        _save_paid_ledger(state, {
            "run_id": "grant-trace",
            "grants": {
                "primary": {"maximum_paid_calls": 42, "used_calls": 42},
                "retry": {"maximum_paid_calls": 3, "used_calls": 3},
            },
            "jobs": {},
        })
        before = {"active_grant_id": "retry", "used_calls": 0}
        after = {
            "active_grant_id": "primary", "used_calls": 42,
            "remaining_calls": 0, "approval_state": "exhausted",
        }

        accounting = _paid_tool_trace_accounting(state, before, after)

        self.assertTrue(accounting["paid_action"])
        self.assertEqual(accounting["paid_grant_id"], "retry")
        self.assertEqual(accounting["paid_calls_before"], 0)
        self.assertEqual(accounting["paid_calls_after"], 3)
        self.assertEqual(accounting["active_grant_after"], "primary")

    def test_reset_prepare_and_approval_request_make_no_agent_or_media_calls(self):
        state = {
            "run_id": "zero-api-reset", "output_dir": self.output_dir,
            "foundation_assets": [{
                "asset_id": "prop_target", "asset_type": "key_prop", "phase": "prop",
                "label": "generic measured prop", "dependencies": [],
                "spec": {"dimensions": "3.2 cm wide"},
                "reference_contract": {"role": "canonical_geometry_anchor"},
            }],
            "candidates": {"prop_target": [{"candidate_id": "prop_target_r01_c01", "round": 1}]},
            "rankings": {}, "locked_assets": {"prop_target": {"version": 1}},
            "group_states": {"scene_1": {"retry_count": 2, "revision_attempt_history": []}},
            "repair_plan": {
                "status": "foundation_reference_reset_required", "group_ids": ["scene_1"],
                "convergence": {"reference_reset": {
                    "asset_ids": ["prop_target"], "affected_shot_ids": ["1.1", "1.2"],
                }},
            },
            "budgets": {"foundation_candidates": 3},
            "counters": {"model_calls": 9, "vision_calls": 4, "image_calls": 12},
            "approval_grants": {},
        }
        before = dict(state["counters"])

        _prepare_foundation_reference_reset(state, "test")
        result = _tool_request_foundation_approval(state)

        self.assertEqual(state["counters"], before)
        self.assertEqual(result["approval_stage"], "foundation_retry_cost")
        self.assertEqual(result["maximum_paid_calls"], 3)
        self.assertEqual(state["pending_approval"]["item_ids"], ["prop_target"])
        self.assertTrue(
            state["foundation_assets"][0]["reference_contract"]
            ["scale_contract"]["required"]
        )

    def test_cli_help_exposes_explicit_foundation_reference_reset(self):
        result = CliRunner().invoke(cli, ["image-agent", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--reset-foundation-references", result.output)
        self.assertIn("--prepare-provider-escalation", result.output)
        self.assertIn("--vision-calibration-file", result.output)

    def test_uncalibrated_vision_profile_is_reported_without_api_calls(self):
        calibration_path = os.path.join(self.output_dir, "vision_calibration.json")
        with open(calibration_path, "w", encoding="utf-8") as handle:
            json.dump({
                "samples": [
                    {"confidence": 0.9, "correct": index % 2 == 0}
                    for index in range(49)
                ]
            }, handle)
        image = MockImageProvider()
        manifest = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            foundation_candidates=1, image_provider=image,
            vision_provider=MockVisionProvider(),
            vision_calibration_file=calibration_path,
        )
        self.assertEqual(manifest["status"], "awaiting_approval")
        self.assertEqual(manifest["vision_confidence_calibration"]["status"], "uncalibrated")
        self.assertFalse(
            manifest["vision_confidence_calibration"]["calibration_applied"]
        )
        self.assertEqual(image.calls, [])

    def test_reset_lock_replaces_only_target_and_approves_only_affected_shots(self):
        old_target = os.path.join(self.output_dir, "old_target.png")
        other_locked = os.path.join(self.output_dir, "other_locked.png")
        candidate_path = os.path.join(self.output_dir, "new_target.png")
        for path, color in ((old_target, "red"), (other_locked, "blue"), (candidate_path, "green")):
            Image.new("RGB", (32, 32), color).save(path)
        contract = {
            "role": "canonical_geometry_anchor",
            "scale_contract": _declared_scale_contract({"physical_spec": "12 mm wide module"}),
        }
        decision_relative = os.path.join("approvals", "reset_lock", "decision.json")
        os.makedirs(os.path.dirname(os.path.join(self.output_dir, decision_relative)), exist_ok=True)
        decision = {
            "request_id": "reset_lock", "state_fingerprint": "fingerprint",
            "decision": "approve", "reviewer": "Human Scale Reviewer",
            "reviewed_item_ids": ["prop_target"], "reviewed_image_fingerprints": {},
            "change_note": "Reviewed the replacement geometry and visible scale comparator.",
            "selections": {"prop_target": "prop_target_r02_c01"},
            "reference_contract_checks": {"prop_target": {
                "candidate_id": "prop_target_r02_c01", "single_object": True,
                "single_view": True, "clean_background": True,
                "no_grid_or_state_sequence": True, "scale_evidence_present": True,
                "scale_relation_matches": True, "scale_comparator_complete": True,
                "scale_comparator_in_focus": True,
                "scale_comparator_contact_or_shared_plane": True,
            }},
        }
        with open(os.path.join(self.output_dir, decision_relative), "w", encoding="utf-8") as handle:
            json.dump(decision, handle)
        state = {
            "run_id": "run", "output_dir": self.output_dir, "status": "awaiting_approval",
            "stop_reason": "foundation_lock_prop", "stage": "foundation_lock",
            "pending_approval": {
                "request_id": "reset_lock", "state_fingerprint": "fingerprint",
                "stage": "foundation_lock_prop", "item_ids": ["prop_target"],
                "decision_path": decision_relative, "reference_contracts": {"prop_target": contract},
            },
            "foundation_assets": [
                {"asset_id": "prop_target", "asset_type": "key_prop", "phase": "prop",
                 "dependencies": [], "reference_contract": contract},
                {"asset_id": "prop_other", "asset_type": "key_prop", "phase": "prop",
                 "dependencies": []},
            ],
            "candidates": {"prop_target": [{
                "candidate_id": "prop_target_r02_c01", "path": os.path.relpath(candidate_path, self.output_dir),
                "ledger_job_id": "ledger_target_r02_c01",
            }]},
            "locked_assets": {
                "prop_target": {"asset_id": "prop_target", "version": 1,
                                "path": os.path.relpath(old_target, self.output_dir)},
                "prop_other": {"asset_id": "prop_other", "version": 1,
                               "path": os.path.relpath(other_locked, self.output_dir)},
            },
            "foundation_reset": {
                "status": "candidate_lock", "asset_ids": ["prop_target"], "group_id": "scene_1",
                "affected_shot_ids": ["1.2", "1.3", "1.4", "1.5"],
            },
            "repair_plan": {
                "status": "foundation_reference_reset_required",
                "deferred_blocking_shot_ids": ["1.1"],
                "deferred_issues": [{"issue_id": "other", "shot_id": "1.1", "blocking": True}],
                "separate_repair_required": True,
            },
            "scene_groups": [{
                "group_id": "scene_1", "shot_ids": ["1.1", "1.2", "1.3", "1.4", "1.5"],
                "shots": [], "reference_asset_ids": ["prop_target", "prop_other"],
            }],
            "current_group_index": 0,
            "group_states": {"scene_1": {"generated": {"1.1": "unchanged.png"}}},
            "budgets": {"max_auto_retries": 1}, "approval_grants": {}, "paid_ledger": {},
            "foundation_primary_grant_id": "", "foundation_retry_grant_id": "",
        }
        candidate_relative = os.path.relpath(candidate_path, self.output_dir)
        _save_paid_ledger(state, {
            "run_id": "run", "grants": {}, "jobs": {"ledger_target_r02_c01": {
                "status": "succeeded", "logical_job_id": "foundation:prop_target_r02_c01",
                "artifact_binding_version": 2, "published_output_path": candidate_relative,
                "published_file_sha256": hashlib.sha256(Path(candidate_path).read_bytes()).hexdigest(),
            }},
        })
        self.assertTrue(_apply_pending_decision(state))
        self.assertEqual(state["locked_assets"]["prop_target"]["version"], 2)
        self.assertEqual(state["locked_assets"]["prop_other"]["version"], 1)
        self.assertEqual(state["group_states"]["scene_1"]["generated"]["1.1"], "unchanged.png")
        self.assertEqual(state["stage"], "group_approval")
        self.assertEqual(state["repair_plan"]["deferred_blocking_shot_ids"], ["1.1"])
        self.assertTrue(state["repair_plan"]["separate_repair_required"])

        approval = _tool_request_scene_group_approval(state)
        self.assertEqual(approval["maximum_paid_calls"], 4)
        self.assertEqual(state["pending_approval"]["item_ids"], ["1.2", "1.3", "1.4", "1.5"])

    def test_post_reset_reference_transfer_excludes_all_shot_images_for_single_and_multi_providers(self):
        paths = {}
        for index, name in enumerate((
            "failed", "adjacent", "style", "character", "location", "prop"
        ), 1):
            path = os.path.join(self.output_dir, f"{name}.png")
            Image.new("RGB", (32, 32), (index * 20, 30, 40)).save(path)
            paths[name] = path
        asset_ids = ["style_generic", "character_generic", "location_generic", "prop_generic"]
        state = {
            "output_dir": self.output_dir, "run_id": "post-reset-references",
            "provider_capabilities": {"reference_mode": "single", "max_references": 1},
            "foundation_assets": [
                {"asset_id": "style_generic", "asset_type": "style_board", "spec": {}},
                {"asset_id": "character_generic", "asset_type": "character_identity", "spec": {}},
                {"asset_id": "location_generic", "asset_type": "location_master", "spec": {}},
                {"asset_id": "prop_generic", "asset_type": "key_prop", "spec": {"prop_id": "p"}},
            ],
            "locked_assets": {
                asset_id: {"path": f"{name}.png", "version": 1}
                for asset_id, name in zip(asset_ids, ("style", "character", "location", "prop"))
            },
        }
        issue = {
            "category": "prop_consistency", "correction_target": "prop_geometry",
            "focus_asset_ids": ["prop_generic"], "reference_asset_ids": ["prop_generic"],
        }
        temporal = [{"group_id": "scene_generic", "shot_id": "neighbor", "path": paths["adjacent"]}]

        single, single_metadata = _revision_provider_references(
            state, asset_ids, "post_reset_single", paths["failed"], temporal,
            shot={"visible_prop_ids": ["p"]}, issues=[issue], revision_attempt_number=1,
            post_foundation_reset_transfer=True,
        )

        self.assertEqual(len(single), 1)
        board = self._read_json(single[0] + ".manju.json")
        included = {board["primary_reference"], *board["supporting_sources"]}
        self.assertEqual(included, {f"{name}.png" for name in ("style", "character", "location", "prop")})
        self.assertFalse(board["failed_shot_reference_included"])
        self.assertTrue(board["temporal_image_references_excluded"])
        self.assertEqual(board["temporal_context"], [])
        self.assertIn("failed.png", board["excluded_image_reference_paths"])
        self.assertIn("adjacent.png", board["excluded_image_reference_paths"])
        self.assertEqual(
            single_metadata["provider_reference_mode"], "post_reset_locked_assets_only_board"
        )
        self.assertEqual(
            single_metadata["reference_strategy"]["primary_role"],
            "post_reset_locked_assets_transfer",
        )
        self.assertTrue(single_metadata["reference_strategy"]["locked_assets_only_reference"])

        state["provider_capabilities"] = {"reference_mode": "multi", "max_references": 8}
        multi, multi_metadata = _revision_provider_references(
            state, asset_ids, "post_reset_multi", paths["failed"], temporal,
            shot={"visible_prop_ids": ["p"]}, issues=[issue], revision_attempt_number=1,
            post_foundation_reset_transfer=True,
        )
        self.assertEqual(set(multi), {paths[name] for name in ("style", "character", "location", "prop")})
        self.assertNotIn(paths["failed"], multi)
        self.assertNotIn(paths["adjacent"], multi)
        self.assertEqual(
            multi_metadata["provider_reference_mode"], "post_reset_locked_assets_only_multi"
        )
        self.assertFalse(multi_metadata["failed_shot_reference_included"])
        self.assertTrue(multi_metadata["temporal_image_references_excluded"])

        prompt = _shot_prompt(
            state,
            {"reference_asset_ids": asset_ids},
            {
                "prompt": "generic staged action", "description": "a referenced item changes appearance",
                "visible_character_ids": ["c"], "visible_prop_ids": ["p"],
                "reference_asset_ids": asset_ids,
            },
            ["restore the declared geometry"],
            multi_metadata["reference_strategy"],
        )
        self.assertIn("POST-FOUNDATION-RESET LOCKED-ASSETS-ONLY TRANSFER", prompt)
        self.assertIn("APPEARANCE-ONLY STATE CONTRACT", prompt)
        self.assertIn("must not change the locked silhouette, topology", prompt)
        self.assertIn("no failed or adjacent shot image", prompt)

    def test_post_reset_plan_migration_preserves_state_and_requires_regenerate(self):
        group = {
            "group_id": "scene_generic", "shot_ids": ["s0", "s1", "s2", "s3"],
            "shots": [], "reference_asset_ids": ["prop_generic", "location_generic"],
        }
        issues = [{
            "issue_id": "reset_related", "shot_id": "s1", "blocking": True,
            "focus_asset_ids": ["prop_generic"], "reference_asset_ids": ["prop_generic"],
        }, {
            "issue_id": "independent_location", "shot_id": "s2", "blocking": True,
            "focus_asset_ids": ["location_generic"], "reference_asset_ids": ["location_generic"],
        }, {
            "issue_id": "independent_identity", "shot_id": "s3", "blocking": True,
            "focus_asset_ids": ["character_generic"], "reference_asset_ids": ["character_generic"],
        }]
        group_state = {
            "status": "revised", "approved": False, "retry_count": 1,
            "generated": {}, "issues": issues, "pending_paid_operation": "",
            "revision_attempt_history": [],
        }
        decision_relative = os.path.join("approvals", "existing", "decision.json")
        decision_path = os.path.join(self.output_dir, decision_relative)
        os.makedirs(os.path.dirname(decision_path), exist_ok=True)
        state = {
            "run_id": "post-reset-migration", "output_dir": self.output_dir,
            "status": "awaiting_approval", "stage": "manual_review",
            "stop_reason": "manual_review_scene_generic",
            "pending_approval": {
                "request_id": "existing", "state_fingerprint": "stable",
                "stage": "manual_review_scene_generic", "item_ids": group["shot_ids"],
                "decision_path": decision_relative,
            },
            "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_generic": group_state},
            "foundation_reset": {
                "status": "shot_review", "group_id": "scene_generic",
                "asset_ids": ["prop_generic"], "affected_shot_ids": ["s1"],
                "locked_assets": {"prop_generic": {
                    "path": "locked_prop.png", "candidate_id": "candidate_new", "version": 2,
                }},
            },
            "locked_assets": {"prop_generic": {
                "path": "locked_prop.png", "candidate_id": "candidate_new", "version": 2,
            }},
            "repair_plan": {"status": "foundation_reference_reset_locked"},
            "budgets": {"max_auto_retries": 1},
            "counters": {"model_calls": 11, "vision_calls": 7, "image_calls": 13},
            "approval_grants": {},
        }
        unchanged = {
            key: json.loads(json.dumps(state[key]))
            for key in ("status", "stage", "stop_reason", "pending_approval", "counters")
        }

        transition = _prepare_post_foundation_reset_transfer(
            state, group, group_state, issues, "resume_migration"
        )

        self.assertEqual(transition["operation"], "post_foundation_reset_transfer")
        for key, value in unchanged.items():
            self.assertEqual(state[key], value)
        self.assertEqual(state["repair_plan"]["status"], "post_foundation_reset_transfer_required")
        self.assertEqual(state["repair_plan"]["shot_ids"], ["s1", "s2", "s3"])
        self.assertEqual(state["repair_plan"]["reset_related_shot_ids"], ["s1"])
        self.assertFalse(state["repair_plan"]["requires_new_paid_grant"])
        self.assertEqual(
            state["repair_plan"]["reference_policy"]["mode"], "locked_assets_only"
        )

        decision = {
            "request_id": "existing", "state_fingerprint": "stable",
            "decision": "approve", "reviewer": "Human Reviewer",
            "reviewed_item_ids": group["shot_ids"], "reviewed_image_fingerprints": {},
            "override_reason": "A detailed but forbidden blocking override.",
        }
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle)
        with self.assertRaisesRegex(ValueError, "cannot be overridden"):
            _apply_pending_decision(state)

        decision.update({
            "decision": "regenerate",
            "change_note": "Regenerate from the current locked Foundation assets only.",
        })
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle)
        self.assertTrue(_apply_pending_decision(state))
        self.assertEqual(group_state["pending_paid_operation"], "post_foundation_reset_transfer")
        self.assertEqual(
            state["repair_plan"]["status"], "post_foundation_reset_transfer_pending_approval"
        )

        approval = _tool_request_scene_group_approval(state)
        self.assertEqual(approval["maximum_paid_calls"], 3)
        self.assertEqual(state["pending_approval"]["item_ids"], ["s1", "s2", "s3"])
        self.assertEqual(
            state["pending_approval"]["operation"], "post_foundation_reset_transfer"
        )

    def test_post_reset_scene_review_classifies_transfer_before_convergence_reset(self):
        for name, color in (
            ("first", "red"), ("second", "green"), ("prop", "blue"), ("location", "yellow")
        ):
            Image.new("RGB", (32, 48), color).save(os.path.join(self.output_dir, f"{name}.png"))
        shots = [{
            "shot_id": "first", "storyboard_path": "$.scenes[0].shots[0]",
            "visible_character_ids": [], "visible_prop_ids": ["p"],
            "reference_asset_ids": ["prop_generic"], "prompt": "generic first action",
            "description": "first generic action",
        }, {
            "shot_id": "second", "storyboard_path": "$.scenes[0].shots[1]",
            "visible_character_ids": [], "visible_prop_ids": [],
            "reference_asset_ids": ["location_generic"], "prompt": "generic second action",
            "description": "second generic action",
        }]
        group = {
            "group_id": "scene_generic", "shot_ids": ["first", "second"],
            "shots": shots, "reference_asset_ids": ["prop_generic", "location_generic"],
        }
        group_state = {
            "status": "revised", "approved": False, "retry_count": 1,
            "generated": {"first": "first.png", "second": "second.png"},
            "issues": [], "revision_attempt_history": [], "review_history": [],
            "pending_paid_operation": "",
        }
        state = {
            "run_id": "post-reset-review", "output_dir": self.output_dir,
            "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_generic": group_state},
            "foundation_assets": [{
                "asset_id": "prop_generic", "asset_type": "key_prop", "spec": {"prop_id": "p"},
            }, {
                "asset_id": "location_generic", "asset_type": "location_master", "spec": {},
            }],
            "locked_assets": {"prop_generic": {
                "path": "prop.png", "candidate_id": "new_candidate", "version": 2,
            }, "location_generic": {"path": "location.png", "version": 1}},
            "foundation_reset": {
                "status": "shot_review", "group_id": "scene_generic",
                "asset_ids": ["prop_generic"], "affected_shot_ids": ["first"],
                "locked_assets": {"prop_generic": {
                    "path": "prop.png", "candidate_id": "new_candidate", "version": 2,
                }},
            },
            "repair_plan": {"status": "foundation_reference_reset_locked"},
            "target_aspect_ratio": 2 / 3, "budgets": {"max_auto_retries": 1},
            "counters": {"vision_calls": 0, "vision_attempts": 0, "vision_failures": 0},
            "quality_gate": {}, "issues": [], "status": "running", "stage": "group_review",
            "vision_recheck_only": False, "vision_repair_mode": False,
        }

        def blocking_review(_task, _paths, context):
            return {"issues": [{
                "issue_id": "provider_reset", "shot_id": "first",
                "category": "prop_consistency", "severity": "major", "blocking": True,
                "problem": "locked geometry is not preserved", "instruction": "restore locked geometry",
                "storyboard_path": shots[0]["storyboard_path"],
                "reference_asset_ids": ["prop_generic"], "focus_asset_ids": ["prop_generic"],
                "correction_target": "prop_geometry", "image_path": context["generated_paths"]["first"],
            }, {
                "issue_id": "provider_other", "shot_id": "second",
                "category": "shot_composition", "severity": "major", "blocking": True,
                "problem": "composition is not preserved", "instruction": "restore composition",
                "storyboard_path": shots[1]["storyboard_path"],
                "reference_asset_ids": ["location_generic"], "focus_asset_ids": ["location_generic"],
                "correction_target": "location_structure", "image_path": context["generated_paths"]["second"],
            }]}

        result = _tool_inspect_scene_group(state, blocking_review)

        self.assertTrue(result["post_foundation_reset_transfer_required"])
        self.assertEqual(state["stage"], "manual_review")
        self.assertEqual(state["repair_plan"]["status"], "post_foundation_reset_transfer_required")
        self.assertEqual(state["repair_plan"]["shot_ids"], ["first", "second"])
        self.assertNotEqual(
            state["repair_plan"]["convergence"]["status"],
            "foundation_reference_reset_required",
        )

    def test_post_reset_revision_persists_locked_only_reference_evidence(self):
        for name, color in (
            ("adjacent", "red"), ("failed", "green"), ("style", "blue"),
            ("location", "yellow"), ("prop", "purple"),
        ):
            Image.new("RGB", (32, 48), color).save(os.path.join(self.output_dir, f"{name}.png"))
        asset_ids = ["style_generic", "location_generic", "prop_generic"]
        target_issue = {
            "issue_id": "geometry_block", "shot_id": "target", "blocking": True,
            "category": "prop_consistency", "correction_target": "prop_geometry",
            "focus_asset_ids": ["prop_generic"], "reference_asset_ids": ["prop_generic"],
            "storyboard_path": "$.scenes[0].shots[1]", "problem": "geometry differs",
            "instruction": "restore the locked geometry",
        }
        shots = [{
            "shot_id": "adjacent", "storyboard_path": "$.scenes[0].shots[0]",
            "visible_character_ids": [], "visible_prop_ids": [],
            "reference_asset_ids": ["style_generic", "location_generic"],
            "prompt": "generic establishing view", "description": "a generic location",
        }, {
            "shot_id": "target", "storyboard_path": "$.scenes[0].shots[1]",
            "visible_character_ids": [], "visible_prop_ids": ["p"],
            "reference_asset_ids": asset_ids,
            "prompt": "generic object action", "description": "an object changes optical state",
        }]
        group = {
            "group_id": "scene_generic", "shot_ids": ["adjacent", "target"],
            "shots": shots, "reference_asset_ids": asset_ids,
        }
        group_state = {
            "status": "approved", "approved": True, "retry_count": 1,
            "generated": {"adjacent": "adjacent.png", "target": "failed.png"},
            "issues": [target_issue], "pending_paid_operation": "post_foundation_reset_transfer",
            "grant_id": "post-reset-grant", "revision_attempt_history": [],
            "post_foundation_reset_transfer": {
                "status": "approved", "shot_ids": ["target"],
                "reset_related_shot_ids": ["target"], "reset_asset_ids": ["prop_generic"],
            },
        }
        state = {
            "run_id": "post-reset-generation", "output_dir": self.output_dir,
            "storyboard_path": self.storyboard_path, "storyboard": _storyboard(),
            "paid_authorized": True, "status": "running", "stage": "group_retry",
            "stop_reason": "", "pending_approval": {}, "quality_gate": {},
            "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_generic": group_state},
            "foundation_assets": [
                {"asset_id": "style_generic", "asset_type": "style_board", "spec": {}},
                {"asset_id": "location_generic", "asset_type": "location_master", "spec": {}},
                {"asset_id": "prop_generic", "asset_type": "key_prop", "spec": {"prop_id": "p"}},
            ],
            "locked_assets": {
                "style_generic": {"path": "style.png", "version": 1},
                "location_generic": {"path": "location.png", "version": 1},
                "prop_generic": {"path": "prop.png", "version": 2},
            },
            "provider_capabilities": {"reference_mode": "single", "max_references": 1},
            "foundation_reset": {
                "status": "shot_review", "group_id": "scene_generic",
                "asset_ids": ["prop_generic"], "affected_shot_ids": ["target"],
                "shot_revision_round": 3, "shot_retry_count": 1,
            },
            "repair_plan": {"status": "post_foundation_reset_transfer_approved"},
            "size": "1024x1536", "target_aspect_ratio": 9 / 16, "aspect_mode": "cover",
            "budgets": {"image_parallelism": 1, "max_auto_retries": 1},
            "counters": {"image_calls": 0}, "approval_grants": {},
        }
        _register_approval_grant(state, {
            "request_id": "post-reset-grant", "stage": "scene_group_cost_scene_generic",
            "state_fingerprint": "stable", "maximum_paid_calls": 1,
        })
        provider = MockImageProvider()

        result = _tool_revise_scene_group(state, provider)

        self.assertEqual(result["revised"], 1)
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertIn("POST-FOUNDATION-RESET LOCKED-ASSETS-ONLY TRANSFER", call["prompt"])
        self.assertIn("APPEARANCE-ONLY STATE CONTRACT", call["prompt"])
        board = self._read_json(call["references"][0] + ".manju.json")
        self.assertFalse(board["failed_shot_reference_included"])
        self.assertTrue(board["temporal_image_references_excluded"])
        self.assertIn("failed.png", board["excluded_image_reference_paths"])
        self.assertIn("adjacent.png", board["excluded_image_reference_paths"])

        revised_relative = group_state["generated"]["target"]
        production = self._read_json(
            os.path.join(self.output_dir, revised_relative + ".manju.json")
        )["production"]
        self.assertEqual(
            production["provider_reference_mode"], "post_reset_locked_assets_only_board"
        )
        self.assertFalse(production["failed_shot_reference_included"])
        self.assertTrue(production["temporal_image_references_excluded"])
        self.assertIn("failed.png", production["excluded_image_reference_paths"])
        self.assertIn("adjacent.png", production["excluded_image_reference_paths"])
        ledger_job = next(iter(_load_paid_ledger(state)["jobs"].values()))
        self.assertEqual(
            ledger_job["finalization_payload"]["provider_reference_mode"],
            "post_reset_locked_assets_only_board",
        )
        self.assertFalse(
            ledger_job["finalization_payload"]["failed_shot_reference_included"]
        )
        self.assertEqual(
            group_state["post_foundation_reset_transfer"]["status"], "shot_review"
        )
        self.assertEqual(
            state["repair_plan"]["status"], "post_foundation_reset_transfer_review"
        )

        def still_blocked_review(_task, _paths, context):
            return {"issues": [{
                **target_issue,
                "image_path": context["generated_paths"]["target"],
            }]}

        review_result = _tool_inspect_scene_group(state, still_blocked_review)
        self.assertTrue(review_result["post_foundation_reset_transfer_blocked"])
        self.assertEqual(
            state["repair_plan"]["status"], "post_foundation_reset_transfer_blocked"
        )
        self.assertEqual(
            group_state["post_foundation_reset_transfer"]["status"], "blocked"
        )
        current_blocking = [
            issue for issue in group_state["issues"] if issue.get("blocking") is True
        ]
        self.assertEqual(
            state["quality_gate"]["observed_blocking_issue_count"], len(current_blocking)
        )
        self.assertEqual(state["quality_gate"]["blocking_issue_count"], len(current_blocking))
        self.assertEqual(
            set(state["quality_gate"]["blocking_issue_ids"]),
            {issue["issue_id"] for issue in current_blocking},
        )
        _tool_request_manual_review(state)
        self.assertEqual(state["pending_approval"]["allowed_decisions"], ["reject"])
        with self.assertRaisesRegex(ValueError, "not allowed"):
            _validate_human_decision(state["pending_approval"], {
                "decision": "approve", "reviewer": "Human Reviewer",
                "reviewed_item_ids": group["shot_ids"],
                "reviewed_image_fingerprints": state["pending_approval"][
                    "reviewed_image_fingerprints"
                ],
            }, "approve")

    def test_post_transfer_scale_evidence_plan_reopens_once_and_stays_non_overridable(self):
        for name, size, color in (
            ("scale", (1536, 1024), "blue"),
            ("location", (512, 512), "gray"),
            ("near_failed", (1024, 1536), "red"),
            ("wide_failed", (1024, 1536), "orange"),
        ):
            Image.new("RGB", size, color).save(os.path.join(self.output_dir, f"{name}.png"))
        scale_contract = _declared_scale_contract({"physical_spec": "12 mm wide and 2 mm thick"})
        locked = {
            "path": "scale.png", "candidate_id": "scale_candidate", "version": 3,
        }
        issues = [{
            "issue_id": "near_scale", "shot_id": "near", "blocking": True,
            "category": "prop_consistency", "correction_target": "prop_geometry",
            "focus_asset_ids": ["prop_scale"], "reference_asset_ids": ["prop_scale"],
            "problem": "the object violates its declared dimensions",
            "instruction": "restore its declared dimensions against scene comparators",
        }, {
            "issue_id": "wide_scale", "shot_id": "wide", "blocking": True,
            "category": "prop_consistency", "correction_target": "prop_geometry",
            "focus_asset_ids": ["prop_scale"], "reference_asset_ids": ["prop_scale"],
            "problem": "the object-to-furniture ratio is incorrect",
            "instruction": "restore the locked object-to-comparator ratio",
        }]
        shots = [{
            "shot_id": shot_id, "reference_asset_ids": ["location", "prop_scale"],
            "visible_prop_ids": ["p"], "visible_character_ids": [],
        } for shot_id in ("near", "wide")]
        group = {
            "group_id": "scene_generic", "shot_ids": ["near", "wide"],
            "shots": shots, "reference_asset_ids": ["location", "prop_scale"],
        }
        group_state = {
            "status": "blocked", "approved": False, "grant_id": "",
            "generated": {"near": "near_failed.png", "wide": "wide_failed.png"},
            "issues": issues, "review_history": [{"vision_available": True}],
            "pending_paid_operation": "",
            "post_foundation_reset_transfer": {
                "status": "blocked", "group_id": "scene_generic",
                "shot_ids": ["near", "wide"], "reset_asset_ids": ["prop_scale"],
                "reference_policy": {"mode": "locked_assets_only"},
            },
        }
        state = {
            "run_id": "scale-reconstruction", "output_dir": self.output_dir,
            "status": "needs_review", "stage": "manual_review",
            "stop_reason": "approval_rejected:manual_review_scene_generic",
            "pending_approval": {}, "scene_groups": [group], "current_group_index": 0,
            "group_states": {"scene_generic": group_state},
            "foundation_assets": [{
                "asset_id": "prop_scale", "asset_type": "key_prop",
                "spec": {"prop_id": "p"},
                "reference_contract": {"scale_contract": scale_contract},
            }, {
                "asset_id": "location", "asset_type": "location_master", "spec": {},
            }],
            "locked_assets": {"prop_scale": locked, "location": {"path": "location.png", "version": 1}},
            "foundation_reset": {
                "status": "shot_review", "group_id": "scene_generic",
                "asset_ids": ["prop_scale"], "locked_assets": {"prop_scale": dict(locked)},
            },
            "repair_plan": {"status": "post_foundation_reset_transfer_blocked"},
            "quality_gate": {"observed_blocking_issue_count": 7},
            "counters": {"model_calls": 9, "tool_steps": 9, "image_calls": 14,
                         "vision_calls": 5, "vision_attempts": 5, "vision_failures": 0},
            "budgets": {"max_auto_retries": 1}, "approval_grants": {},
        }
        unchanged = {
            key: json.loads(json.dumps(state[key]))
            for key in ("status", "stage", "stop_reason", "counters")
        }

        transition = _prepare_post_transfer_scale_evidence_reconstruction(
            state, group, group_state, issues, "resume_migration"
        )

        self.assertEqual(transition["operation"], "scale_evidence_reconstruction")
        for key, value in unchanged.items():
            self.assertEqual(state[key], value)
        self.assertEqual(
            state["repair_plan"]["status"],
            "post_transfer_scale_evidence_reconstruction_required",
        )
        self.assertEqual(state["repair_plan"]["shot_ids"], ["near", "wide"])
        self.assertFalse(state["repair_plan"]["requires_new_paid_grant"])
        self.assertEqual(state["quality_gate"]["observed_blocking_issue_count"], 2)

        _tool_request_manual_review(state)
        self.assertEqual(state["pending_approval"]["allowed_decisions"], ["regenerate", "reject"])
        pending = state["pending_approval"]
        decision_path = os.path.join(self.output_dir, pending["decision_path"])
        with open(decision_path, "w", encoding="utf-8") as handle:
            json.dump({
                "request_id": pending["request_id"],
                "state_fingerprint": pending["state_fingerprint"],
                "decision": "regenerate", "reviewer": "Human Reviewer",
                "change_note": "Use the distinct locked scale-evidence strategy once.",
                "reviewed_item_ids": group["shot_ids"],
                "reviewed_image_fingerprints": pending["reviewed_image_fingerprints"],
            }, handle)
        self.assertTrue(_apply_pending_decision(state))
        self.assertEqual(group_state["pending_paid_operation"], "scale_evidence_reconstruction")
        approval = _tool_request_scene_group_approval(state)
        self.assertEqual(approval["maximum_paid_calls"], 2)
        self.assertEqual(state["pending_approval"]["item_ids"], ["near", "wide"])

        state["pending_approval"] = {}
        group_state["post_transfer_scale_evidence_reconstruction"]["status"] = "shot_review"
        group_state["issues"] = [issues[0]]
        terminal = _close_blocked_post_transfer_scale_evidence_reconstruction(
            state, group, group_state, [issues[0]], "scene_group_review"
        )
        self.assertEqual(terminal["operation"], "scale_evidence_reconstruction_blocked")
        self.assertEqual(
            state["repair_plan"]["status"],
            "post_transfer_scale_evidence_reconstruction_blocked",
        )
        self.assertEqual(state["quality_gate"]["observed_blocking_issue_count"], 1)
        _tool_request_manual_review(state)
        self.assertEqual(state["pending_approval"]["allowed_decisions"], ["reject"])

    def test_scale_evidence_priority_board_is_dominant_and_locked_only(self):
        paths = {}
        for name, size, color in (
            ("failed", (1024, 1536), "red"),
            ("adjacent", (1024, 1536), "orange"),
            ("scale", (1536, 1024), "blue"),
            ("location", (1024, 1024), "gray"),
            ("character", (1024, 1536), "green"),
        ):
            path = os.path.join(self.output_dir, f"{name}.png")
            Image.new("RGB", size, color).save(path)
            paths[name] = path
        scale_contract = _declared_scale_contract({"dimensions": "12 mm wide, 2 mm thick"})
        asset_ids = ["location", "character", "prop_scale"]
        state = {
            "output_dir": self.output_dir, "run_id": "scale-board",
            "provider_capabilities": {"reference_mode": "single", "max_references": 1},
            "foundation_assets": [{
                "asset_id": "location", "asset_type": "location_master", "spec": {},
            }, {
                "asset_id": "character", "asset_type": "character_identity", "spec": {},
            }, {
                "asset_id": "prop_scale", "asset_type": "key_prop", "spec": {"prop_id": "p"},
                "reference_contract": {"scale_contract": scale_contract},
            }],
            "locked_assets": {
                "location": {"path": "location.png", "version": 1},
                "character": {"path": "character.png", "version": 1},
                "prop_scale": {"path": "scale.png", "version": 3},
            },
        }
        issue = {
            "category": "prop_consistency", "correction_target": "prop_geometry",
            "focus_asset_ids": ["prop_scale"], "reference_asset_ids": ["prop_scale"],
        }
        temporal = [{"group_id": "scene_generic", "shot_id": "neighbor",
                     "path": paths["adjacent"]}]
        shot = {"visible_prop_ids": ["p"], "visible_character_ids": [],
                "reference_asset_ids": asset_ids, "prompt": "a restrained object scene",
                "description": "the object remains visible at its declared physical scale"}

        references, metadata = _revision_provider_references(
            state, asset_ids, "scale_priority_single", paths["failed"], temporal,
            shot=shot, issues=[issue], revision_attempt_number=2,
            scale_evidence_reconstruction=True,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(
            metadata["provider_reference_mode"],
            "scale_evidence_priority_locked_assets_board",
        )
        board_manifest = self._read_json(references[0] + ".manju.json")
        self.assertEqual(board_manifest["primary_reference"], "scale.png")
        self.assertEqual(
            board_manifest["reference_layout"]["mode"],
            "dominant_full_comparator_scale_evidence",
        )
        self.assertEqual(board_manifest["reference_layout"]["primary_region"], [0, 0, 1536, 1024])
        self.assertFalse(board_manifest["failed_shot_reference_included"])
        self.assertTrue(board_manifest["temporal_image_references_excluded"])
        with Image.open(references[0]) as board_image:
            self.assertEqual(board_image.size, (1536, 1536))
        prompt = _shot_prompt(
            state, {"reference_asset_ids": asset_ids}, shot,
            ["restore the declared physical ratio"], metadata["reference_strategy"],
        )
        self.assertIn("SCALE-EVIDENCE-PRIORITY RECONSTRUCTION", prompt)
        self.assertIn("dominant full-comparator reference panel", prompt)
        self.assertIn("never by increasing its physical dimensions", prompt)

        state["provider_capabilities"] = {"reference_mode": "multi", "max_references": 8}
        multi, multi_metadata = _revision_provider_references(
            state, asset_ids, "scale_priority_multi", paths["failed"], temporal,
            shot=shot, issues=[issue], revision_attempt_number=2,
            scale_evidence_reconstruction=True,
        )
        self.assertEqual(multi[0], paths["scale"])
        self.assertNotIn(paths["failed"], multi)
        self.assertNotIn(paths["adjacent"], multi)
        self.assertEqual(
            multi_metadata["provider_reference_mode"],
            "scale_evidence_priority_locked_assets_multi",
        )

    def test_provider_escalation_is_zero_api_exact_and_uses_new_run(self):
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(_storyboard(two_shots=True), handle, ensure_ascii=False)
        image = MockImageProvider()
        completed = self._drive(
            image, MockVisionProvider(), foundation_candidates=1,
        )
        source_run_id = completed["run_id"]
        source_state_path = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", source_run_id,
            "state.json",
        )
        state = self._read_json(source_state_path)
        group = state["scene_groups"][0]
        group_state = state["group_states"][group["group_id"]]
        target_shot = group["shots"][0]
        target_id = target_shot["shot_id"]
        other_id = group["shots"][1]["shot_id"]
        target_image = group_state["generated"][target_id]
        other_path = os.path.join(self.output_dir, group_state["generated"][other_id])
        other_sha = hashlib.sha256(open(other_path, "rb").read()).hexdigest()
        focus_asset = target_shot["reference_asset_ids"][0]

        def reviewed_issue(issue_id, target, problem, instruction, confidence):
            return {
                "issue_id": issue_id, "shot_id": target_id, "group_id": group["group_id"],
                "blocking": True, "severity": "major", "category": "visual_quality",
                "correction_target": target, "problem": problem, "instruction": instruction,
                "storyboard_path": target_shot["storyboard_path"],
                "reference_asset_ids": [focus_asset], "focus_asset_ids": [focus_asset],
                "image_path": target_image, "evidence_valid": True,
                "constraint_verdict": {
                    "verdict": "fail", "confidence": confidence,
                    "evidence": [{"image_path": target_image, "problem": problem}],
                },
            }

        active = reviewed_issue(
            "identity-active", "character_identity", "face identity visibly differs",
            "restore the locked face identity", 0.91,
        )
        deferred = reviewed_issue(
            "effect-deferred", "effect_alignment", "secondary glow is detached",
            "repair the deferred secondary glow", 0.99,
        )
        group_state.update({
            "status": "blocked", "issues": [active, deferred], "approved": False,
            "pending_paid_operation": "", "grant_id": "",
        })
        state.update({
            "status": "needs_review", "stop_reason": "scene_group_non_converging",
            "stage": "scene_group_non_converging", "pending_approval": {},
            "repair_plan": {"status": "non_converging", "strategy_exhausted": True},
            "issues": [active, deferred],
        })
        _persist_artifacts(state)
        calls_before = len(image.calls)

        prepared = prepare_provider_escalation(self.storyboard_path, self.output_dir)

        self.assertEqual(prepared["model_calls_made"], 0)
        self.assertEqual(prepared["vision_calls_made"], 0)
        self.assertEqual(prepared["image_calls_made"], 0)
        self.assertEqual(prepared["maximum_paid_calls"], 1)
        self.assertEqual(len(image.calls), calls_before)
        plan = self._read_json(os.path.join(self.output_dir, "visual_repair_plan.json"))
        task = plan["groups"][0]["tasks"][0]
        self.assertEqual(task["active_issue_id"], "identity-active")
        self.assertEqual(task["deferred_issue_ids"], ["effect-deferred"])
        source_events = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", source_run_id,
            "events.jsonl",
        )
        with open(source_events, "rb") as handle:
            source_events_after_prepare = handle.read()

        approval = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            repair_vision_blockers=True, foundation_candidates=1,
            image_provider=image, vision_provider=MockVisionProvider(),
        )
        self.assertNotEqual(approval["run_id"], source_run_id)
        self.assertEqual(approval["pending_approval"]["maximum_paid_calls"], 1)
        self.assertEqual(approval["pending_approval"]["item_ids"], [target_id])
        self.assertEqual(
            approval["pending_approval"]["provider_escalation"]["tasks"][0]["task_id"],
            task["task_id"],
        )
        with open(source_events, "rb") as handle:
            self.assertEqual(handle.read(), source_events_after_prepare)

        self._approve(approval)
        repaired = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=True,
            repair_vision_blockers=True, foundation_candidates=1,
            image_provider=image, vision_provider=MockVisionProvider(),
        )

        self.assertEqual(repaired["status"], "completed")
        self.assertEqual(len(image.calls), calls_before + 1)
        escalation_call = image.calls[-1]
        self.assertIn("CONSTRAINT-ISOLATED EDIT CONTRACT", escalation_call["prompt"])
        self.assertLess(
            escalation_call["prompt"].index("CONSTRAINT-ISOLATED EDIT CONTRACT"),
            escalation_call["prompt"].index("NARRATIVE PROMPT: "),
        )
        self.assertNotIn("repair the deferred secondary glow", escalation_call["prompt"])
        board = self._read_json(escalation_call["references"][0] + ".manju.json")
        self.assertEqual(board["primary_reference_role"], "constraint_isolated_edit")
        self.assertTrue(board["temporal_image_references_excluded"])
        self.assertEqual(board["supporting_asset_ids"], [focus_asset])
        self.assertEqual(board["temporal_context"], [])
        self.assertIn(other_id, {
            item["shot_id"] for item in board["excluded_temporal_context"]
        })
        self.assertEqual(
            hashlib.sha256(open(other_path, "rb").read()).hexdigest(), other_sha
        )
        with open(source_events, "rb") as handle:
            self.assertEqual(handle.read(), source_events_after_prepare)

        repeated_state_path = os.path.join(
            self.output_dir, "stages", "visual_agent", "runs", repaired["run_id"],
            "state.json",
        )
        repeated_state = self._read_json(repeated_state_path)
        repeated_state.update({
            "status": "needs_review", "stop_reason": "scene_group_non_converging",
            "stage": "scene_group_non_converging", "provider_escalation_mode": True,
            "repair_plan": {
                **repeated_state.get("repair_plan", {}),
                "status": "provider_escalation_blocked",
            },
        })
        _persist_artifacts(repeated_state)
        with self.assertRaisesRegex(ValueError, "distinct provider capability or strategy"):
            prepare_provider_escalation(self.storyboard_path, self.output_dir)

    def test_partial_vision_outage_continues_and_resume_reviews_only_missing_group(self):
        storyboard = _storyboard()
        second_scene = json.loads(json.dumps(storyboard["scenes"][0]))
        second_scene["scene_id"] = "2"
        second_scene["heading"] = "EXT. COURTYARD - DAWN"
        second_scene["shots"][0]["shot_id"] = "2.1"
        second_scene["shots"][0]["visual"]["description"] = "the figure enters the courtyard"
        second_scene["shots"][0]["prompts"]["image_en"] = "the figure enters the courtyard"
        storyboard["scenes"].append(second_scene)
        storyboard = normalize_storyboard(storyboard)
        with open(self.storyboard_path, "w", encoding="utf-8") as handle:
            json.dump(storyboard, handle, ensure_ascii=False)
        image = MockImageProvider()
        completed = self._drive(
            image, MockVisionProvider(), foundation_candidates=1,
        )
        original_review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        image_hashes = {
            shot_id: hashlib.sha256(open(os.path.join(self.output_dir, relative), "rb").read()).hexdigest()
            for group_state in original_review["scene_groups"].values()
            for shot_id, relative in group_state["generated"].items()
        }
        image_calls = completed["counters"]["image_calls"]

        class OneGroupUnavailable:
            def __init__(self, unavailable=True):
                self.unavailable = unavailable
                self.reviewed = []

            def __call__(self, task, paths, context):
                if task == "rank_foundation_candidates":
                    return {"ranking": context["candidate_ids"], "summary": "ranked"}
                group_id = context["group"]["group_id"]
                self.reviewed.append(group_id)
                if self.unavailable and group_id == "scene_1":
                    return None
                return {"issues": []}

        first_review = OneGroupUnavailable()
        interrupted = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            recheck_vision=True, foundation_candidates=1,
            image_provider=image, vision_provider=first_review,
        )
        self.assertEqual(first_review.reviewed, ["scene_1", "scene_2"])
        self.assertEqual(interrupted["stop_reason"], "vision_recheck_unavailable")
        self.assertEqual(interrupted["repair_plan"]["status"], "verification_incomplete")
        self.assertFalse(interrupted["repair_plan"]["requires_new_paid_grant"])
        self.assertEqual(interrupted["repair_plan"]["maximum_paid_calls"], 0)
        self.assertEqual(interrupted["pending_approval"], {})
        self.assertEqual(interrupted["counters"]["image_calls"], image_calls)

        resumed_review = OneGroupUnavailable(unavailable=False)
        resumed = run_image_agent(
            self.storyboard_path, self.output_dir, execute_paid_calls=False,
            recheck_vision=True, foundation_candidates=1,
            image_provider=image, vision_provider=resumed_review,
        )
        self.assertEqual(resumed_review.reviewed, ["scene_1"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["counters"]["image_calls"], image_calls)
        final_review = self._read_json(os.path.join(self.output_dir, "visual_review.json"))
        final_hashes = {
            shot_id: hashlib.sha256(open(os.path.join(self.output_dir, relative), "rb").read()).hexdigest()
            for group_state in final_review["scene_groups"].values()
            for shot_id, relative in group_state["generated"].items()
        }
        self.assertEqual(final_hashes, image_hashes)


if __name__ == "__main__":
    unittest.main()
