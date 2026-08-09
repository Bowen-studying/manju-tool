import json
import os
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from manju.cli import cli
from manju.pipeline.storyboard_schema import normalize_storyboard
from manju.pipeline.storyboard_schema import validate_storyboard
from manju.pipeline.storyboard_agent import _deterministic_quality_issues, _visible_characters
from manju.pipeline.storyboard_supervisor import (
    ALLOWED_ACTIONS,
    _build_source_model,
    _enrich_source_model_from_plan,
    _is_model_schema_claim,
    _normalize_model_issues,
    _prepare_scene_semantics,
    _review_audit_payload,
    _scene_shots_contract_errors,
    _semantic_source_issues,
    _source_sentences,
    _supervisor_node,
    _validate_action_args,
    calculate_agent_call_budget,
    generate_storyboard_agent,
)
from test_storyboard_agent import SOURCE, plan_response, shots_response


def action(name, args=None, summary="mock decision"):
    return json.dumps({
        "action": name,
        "args": args or {},
        "decision_summary": summary,
    })


def source_analysis_response(text=SOURCE):
    return json.dumps({
        "summary": text[:200],
        "entities": [{"name": "Lin", "kind": "character"}] if "Lin" in text else [],
        "beats": [{
            "source_quote": text,
            "must_preserve_facts": [text],
            "relation_to_previous": "before",
        }],
    })


CLEAN_REVIEW = json.dumps({
    "summary": "No objective blockers.",
    "issues": [],
})


def blocking_review(
    issue_id="model_mock",
    scene_id="1",
    problem="objective blocker",
    value="Lin turns toward the rooftop door",
):
    return json.dumps({
        "summary": "One evidenced blocker.",
        "issues": [{
            "issue_id": issue_id,
            "scene_id": scene_id,
            "shot_id": f"{scene_id}.1",
            "severity": "high",
            "category": "continuity",
            "blocking": True,
            "defect": problem,
            "suggestion": "Repair only this scene.",
            "source_evidence": [{"beat_id": "beat_0001"}],
            "storyboard_evidence": [{
                "path": f"$.scenes[{int(scene_id) - 1}].shots[0].visual.description",
                "value": value,
            }],
        }],
    })


def multi_scene_plan(count):
    payload = json.loads(plan_response())
    base = payload["scenes"][0]
    payload["scenes"] = [
        {
            **base,
            "scene_id": str(index),
            "heading": f"EXT. LOCATION {index} - DAY",
            "purpose": f"story beat {index}",
            "source_chunk_ids": [1],
        }
        for index in range(1, count + 1)
    ]
    return json.dumps(payload)


def scene_shots(scene_id, *, camera="slow push", description=None):
    payload = json.loads(shots_response(description or f"Lin performs story beat {scene_id}"))
    payload["shots"][0]["shot_id"] = f"{scene_id}.1"
    payload["shots"][0]["visual"]["camera_movement"] = camera
    return json.dumps(payload)


def revision_shots_response(description="repaired scene"):
    payload = json.loads(shots_response(description))
    for shot in payload["shots"]:
        shot["source_beat_ids"] = ["beat_0001"]
    return json.dumps(payload)


def clean_actions():
    return [
        action("analyze_source"),
        action("create_plan"),
        plan_response(),
        action("generate_scenes"),
        shots_response(),
        action("assemble_storyboard"),
        action("validate_schema"),
        action("compare_source"),
        action("inspect_shootability"),
        action("review_storyboard"),
        CLEAN_REVIEW,
        action("finalize"),
    ]


class StoryboardSupervisorTests(unittest.TestCase):
    def _run(self, root, responses, *, text=SOURCE, scenes=1, **kwargs):
        iterator = iter(responses)

        def provider(system, user, **call_kwargs):
            if system.startswith("SOURCE_MODEL_EXTRACTION_V2"):
                chunk = user.split("\n", 1)[1] if "\n" in user else text
                return source_analysis_response(chunk)
            return next(iterator)

        with patch(
            "manju.pipeline.storyboard_supervisor.call_llm",
            side_effect=provider,
        ) as mocked:
            result = generate_storyboard_agent(
                text,
                "Mock Story",
                len(text),
                scenes,
                os.path.join(root, "stages", "agent"),
                root,
                **kwargs,
            )
        return result, mocked.call_count

    def _manifest(self, root):
        with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def _trace(self, root):
        with open(os.path.join(root, "agent_trace.jsonl"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_clean_supervisor_run_uses_dynamic_tools_and_writes_safe_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, clean_actions())

            self.assertIsNotNone(result)
            self.assertEqual(validate_storyboard(result), [])
            manifest = self._manifest(root)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["model_calls"], calls)
            self.assertEqual(manifest["revision_count"], 0)
            self.assertEqual(manifest["supervisor_agent_version"], "3.5")
            self.assertEqual(manifest["budgets"]["requested_max_calls"], "auto")
            self.assertEqual(manifest["budgets"]["effective_max_calls"], 22)
            trace = self._trace(root)
            actions = [event["action"] for event in trace]
            self.assertEqual(actions, [
                "analyze_source", "create_plan", "generate_scenes",
                "assemble_storyboard", "validate_schema", "compare_source",
                "inspect_shootability", "review_storyboard", "finalize",
            ])
            self.assertNotIn("revise_plan", actions)
            self.assertNotIn("revise_scenes", actions)
            self.assertTrue(set(actions).issubset(ALLOWED_ACTIONS))
            self.assertFalse(any("image_api" in name or "video_api" in name for name in ALLOWED_ACTIONS))

    def test_cli_routes_agent_budgets_to_supervisor(self):
        plan = json.loads(plan_response())
        generated = normalize_storyboard({
            "title": "Mock Story",
            "creative_bible": plan["creative_bible"],
            "scenes": [{**plan["scenes"][0], "shots": json.loads(shots_response())["shots"]}],
        })
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "story.txt")
            output = os.path.join(root, "output")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(SOURCE)
            with patch(
                "manju.pipeline.storyboard_supervisor.generate_storyboard_agent",
                return_value=generated,
            ) as mocked, patch("manju.pipeline.storyboard.write_xlsx"):
                result = runner.invoke(cli, [
                    "storyboard", source, "--engine", "agent", "--max-scenes", "1",
                    "--agent-max-steps", "31", "--agent-max-calls", "17",
                    "--agent-max-revisions", "1", "-o", output,
                ])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(mocked.call_args.kwargs["max_steps"], 31)
            self.assertEqual(mocked.call_args.kwargs["max_calls"], 17)
            self.assertEqual(mocked.call_args.kwargs["max_revisions"], 1)

    def test_auto_budget_formula_and_explicit_override(self):
        cases = [
            ((1, 1, 2, None), 22),
            ((3, 1, 2, None), 34),
            ((8, 1, 2, None), 36),
            ((8, 5, 2, None), 36),
            ((8, 5, 2, 52), 52),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                effective, factors = calculate_agent_call_budget(*arguments)
                self.assertEqual(effective, expected)
                self.assertEqual(factors["mode"], "explicit" if arguments[-1] else "auto")

    def test_cli_auto_budget_routes_none(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "story.txt")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(SOURCE)
            generated = normalize_storyboard(json.loads(plan_response()) | {
                "scenes": [{
                    **json.loads(plan_response())["scenes"][0],
                    "shots": json.loads(shots_response())["shots"],
                }],
            })
            with patch(
                "manju.pipeline.storyboard_supervisor.generate_storyboard_agent",
                return_value=generated,
            ) as mocked, patch("manju.pipeline.storyboard.write_xlsx"):
                result = runner.invoke(cli, [
                    "storyboard", source, "--engine", "agent", "--max-scenes", "1",
                    "--agent-max-calls", "auto", "-o", os.path.join(root, "out"),
                ])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIsNone(mocked.call_args.kwargs["max_calls"])

    def test_review_aliases_are_canonical_and_invalid_evidence_is_advisory(self):
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{"shot_id": "1.1", "visual": {"description": "current"}}]}]}
        source_model = {"beats": [{"beat_id": "beat_0001", "text": "source fact"}]}
        review = _normalize_model_issues({"issues": [{
            "issue_id": "alias",
            "shot_id": "1.1",
            "defect": "mismatch",
            "suggestion": "repair it",
            "category": "continuity",
            "severity": "high",
            "blocking": True,
            "source_beat_id": "beat_0001",
            "storyboard_evidence": [{
                "path": "$.scenes[0].shots[0].visual.description", "value": "current",
            }],
        }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
        self.assertEqual(review["blocking_issues"][0]["problem"], "mismatch")
        self.assertEqual(review["blocking_issues"][0]["instruction"], "repair it")
        invalid = _normalize_model_issues({"issues": [{
            "problem": "unsupported claim", "category": "continuity",
            "severity": "high", "blocking": True,
            "source_beat_id": "missing", "json_path": "$.missing",
        }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
        self.assertEqual(invalid["blocking_issues"], [])
        self.assertEqual(invalid["advisory_issues"][0]["downgrade_reason"], "evidence_invalid")

    def test_source_review_aliases_remain_blocking_and_unknown_objective_stops_for_classification(self):
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "source_beat_ids": ["beat_0001"],
            "visual": {"description": "current"},
        }]}]}
        source_model = {"beats": [{"beat_id": "beat_0001", "text": "required fact"}]}
        for category in (
            "source_alignment", "required_fact_omission", "source_traceability",
            "beat_traceability", "source_mapping", "beat_mapping",
            "source_beat_alignment", "beat-source-alignment",
        ):
            review = _normalize_model_issues({"issues": [{
                "category": category, "severity": "high", "blocking": True,
                "problem": "source mismatch", "source_beat_id": "beat_0001",
                "storyboard_evidence": [{
                    "path": "$.scenes[0].shots[0].visual.description", "value": "current",
                }],
            }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
            self.assertEqual(review["blocking_issues"][0]["category"], "source_fidelity")
            self.assertEqual(review["blocking_issues"][0]["original_category"], category)
        unknown = _normalize_model_issues({"issues": [{
            "category": "provider_new_objective_check", "severity": "critical", "blocking": True,
            "problem": "objective mismatch", "source_beat_id": "beat_0001",
            "storyboard_evidence": [{
                "path": "$.scenes[0].shots[0].visual.description", "value": "current",
            }],
        }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
        issue = unknown["blocking_issues"][0]
        self.assertTrue(issue["needs_classification"])
        self.assertFalse(issue["auto_revisable"])

    def test_subjective_unknown_category_is_advisory_even_when_model_requests_blocking(self):
        storyboard = {"scenes": [{"shots": [{"visual": {"description": "current"}}]}]}
        source_model = {"beats": [{"beat_id": "beat_0001", "text": "source"}]}
        review = _normalize_model_issues({"issues": [{
            "category": "cinematography", "severity": "critical", "blocking": True,
            "problem": "prefer a different lens", "source_beat_id": "beat_0001",
            "storyboard_evidence": [{
                "path": "$.scenes[0].shots[0].visual.description", "value": "current",
            }],
        }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
        self.assertEqual(review["blocking_issues"], [])

    def test_evidenced_production_metadata_is_promoted_even_when_provider_calls_it_advisory(self):
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "visual": {
                "description": "A looks toward off-screen B",
                "visible_character_ids": ["a", "b"],
            },
        }]}]}
        source_model = {"beats": [{"beat_id": "beat_0001", "text": "The frame holds on A."}]}
        review = _normalize_model_issues({"issues": [{
            "category": "visible_character_metadata", "severity": "medium", "blocking": False,
            "problem": "B is off-screen but declared visible", "instruction": "remove b",
            "source_evidence": [{"beat_id": "beat_0001"}],
            "storyboard_evidence": [{
                "path": "$.scenes[0].shots[0].visual.visible_character_ids",
                "value": ["a", "b"],
            }],
        }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
        issue = review["blocking_issues"][0]
        self.assertEqual(issue["category"], "visible_entity_consistency")
        self.assertTrue(issue["auto_revisable"])
        self.assertEqual(issue["promotion_reason"], "evidence_backed_production_metadata")

    def test_wardrobe_binding_opinion_is_advisory_not_a_visible_prop_blocker(self):
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "visible_prop_ids": [],
            "visual": {"description": "A wears a red coat", "key_props": []},
        }]}]}
        source_model = {
            "beats": [{"beat_id": "beat_0001", "text": "A wears a red coat."}],
            "props": [{"prop_id": "prop_001", "name": "red coat", "asset_kind": "wardrobe"}],
        }
        review = _normalize_model_issues({"issues": [{
            "category": "visible_prop_metadata", "severity": "medium", "blocking": False,
            "problem": "prop_001 wardrobe is missing from visible_prop_ids",
            "instruction": "add prop_001",
            "source_evidence": [{"beat_id": "beat_0001"}],
            "storyboard_evidence": [{
                "path": "$.scenes[0].shots[0].visible_prop_ids", "value": [],
            }],
        }]}, default_category="shootability", source_model=source_model, storyboard=storyboard)
        self.assertEqual(review["blocking_issues"], [])
        self.assertEqual(
            review["advisory_issues"][0]["downgrade_reason"],
            "wardrobe_managed_by_character_identity",
        )

    def test_explicit_character_visibility_survives_body_detail_shot(self):
        characters = [{"character_id": "c1", "name": "A"}]
        shot = {"visual": {
            "shot_type": "close-up", "description": "A's hand and sleeve fill the frame",
            "visible_character_ids": ["c1"],
        }}
        self.assertEqual(_visible_characters(shot, characters), characters)

    def test_unknown_action_argument_is_recoverable_protocol_error(self):
        responses = [
            action("revise_scenes", {"scene_ids": ["1"], "revisions": ["free text"]}),
            action("stop_needs_review", {"reason": "protocol was rejected"}),
        ]
        with tempfile.TemporaryDirectory() as root:
            self._run(root, responses)
            first = self._trace(root)[0]
            self.assertEqual(first["result"]["error"], "invalid_action_args")
            self.assertEqual(first["result"]["unknown_args"], ["revisions"])
            self.assertNotIn("revisions", first["args"])

    def test_common_revision_argument_aliases_are_normalized(self):
        for alias in ("blocking_issue_ids", "current_blocking_issue_ids"):
            args, error = _validate_action_args("revise_scenes", {
                "scene_ids": ["1"], alias: ["issue_1"],
            })
            self.assertIsNone(error)
            self.assertEqual(args["issue_ids"], ["issue_1"])

    def test_revised_candidate_forces_combined_audit_without_another_supervisor_call(self):
        storyboard = {"scenes": [{"scene_id": "1", "shots": []}]}
        state = {
            "storyboard": storyboard, "pending_revision": {"issue_ids": ["issue_1"]},
            "audited_fingerprint": "", "run_dir": tempfile.gettempdir(),
            "max_steps": 40, "max_calls": 20,
        }
        update = _supervisor_node(state)
        self.assertEqual(update["pending_action"]["action"], "review_storyboard")
        self.assertIn("Code-required", update["pending_action"]["decision_summary"])

    def test_empty_source_response_is_retried_but_never_cached_as_success(self):
        responses = iter([action("analyze_source"), action("stop_needs_review", {
            "reason": "source extraction unavailable",
        })])

        def provider(system, user, **kwargs):
            if system.startswith("SOURCE_MODEL_EXTRACTION_V2"):
                return None
            return next(responses)

        with tempfile.TemporaryDirectory() as root, patch(
            "manju.pipeline.storyboard_supervisor.call_llm", side_effect=provider,
        ):
            result = generate_storyboard_agent(
                SOURCE, "Mock Story", len(SOURCE), 1,
                os.path.join(root, "stages", "agent"), root,
            )
            self.assertIsNone(result)
            manifest = self._manifest(root)
            self.assertEqual(manifest["status"], "needs_review")
            self.assertEqual(manifest["model_calls"], 4)
            call_dir = os.path.join(
                root, "stages", "agent",
                next(name for name in os.listdir(os.path.join(root, "stages", "agent"))
                     if name.startswith("run_")), "model_calls",
            )
            source_records = []
            for name in os.listdir(call_dir):
                if name.startswith("analyze_source_chunk_"):
                    with open(os.path.join(call_dir, name), encoding="utf-8") as handle:
                        source_records.append(json.load(handle))
            self.assertEqual(len(source_records), 2)
            self.assertTrue(all(item["status"] == "invalid_response" for item in source_records))
            self.assertTrue(all("response" not in item for item in source_records))

    def test_revision_does_not_start_without_audit_reserve(self):
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), shots_response(), action("assemble_storyboard"),
            action("review_storyboard"), blocking_review(),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            action("stop_needs_review", {"reason": "insufficient reserve"}),
        ]
        with tempfile.TemporaryDirectory() as root:
            self._run(root, responses, max_calls=12)
            revision = [event for event in self._trace(root) if event["action"] == "revise_scenes"][0]
            self.assertEqual(revision["result"]["error"], "insufficient_revision_reserve")
            self.assertEqual(self._manifest(root)["revision_attempt_counts"], {})

    def test_revisable_subset_runs_when_another_issue_needs_human_classification(self):
        mixed_review = json.dumps({
            "summary": "One repairable and one unclassified issue.",
            "issues": [
                {
                    "issue_id": "model_repairable", "scene_id": "1", "shot_id": "1.1",
                    "category": "continuity", "severity": "high", "blocking": True,
                    "problem": "repair this scene", "instruction": "repair only this scene",
                    "source_evidence": [{"beat_id": "beat_0001"}],
                    "storyboard_evidence": [{
                        "path": "$.scenes[0].shots[0].visual.description",
                        "value": "Lin turns toward the rooftop door",
                    }],
                },
                {
                    "issue_id": "model_unclassified", "scene_id": "1", "shot_id": "1.1",
                    "category": "provider_new_objective_check", "severity": "critical", "blocking": True,
                    "problem": "requires classification", "instruction": "wait for a human",
                    "source_evidence": [{"beat_id": "beat_0001"}],
                    "storyboard_evidence": [{
                        "path": "$.scenes[0].shots[0].visual.description",
                        "value": "Lin turns toward the rooftop door",
                    }],
                },
            ],
        })
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), shots_response(), action("assemble_storyboard"),
            action("review_storyboard"), mixed_review,
            action("revise_scenes", {
                "scene_ids": ["1"],
                "issue_ids": ["model_repairable", "model_unclassified"],
            }),
            revision_shots_response("repaired candidate"), CLEAN_REVIEW, action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses)
            self.assertIsNotNone(result)
            revision = [event for event in self._trace(root) if event["action"] == "revise_scenes"][0]
            self.assertEqual(revision["result"]["deferred_issue_ids"], ["model_unclassified"])
            self.assertEqual(revision["result"]["revised_scene_ids"], ["1"])

    def test_storyboard_cli_returns_two_for_needs_review(self):
        runner = CliRunner()
        candidate = normalize_storyboard({
            "title": "pending", "creative_bible": {"style_anchor": "cinematic", "characters": []},
            "metadata": {"agent_status": "needs_review"},
            "scenes": [{"scene_id": "1", "heading": "INT. ROOM", "shots": json.loads(shots_response())["shots"]}],
        })
        candidate["metadata"]["agent_status"] = "needs_review"
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "story.txt")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(SOURCE)
            with patch("manju.cli.run_storyboard", return_value=candidate):
                result = runner.invoke(cli, ["storyboard", source, "--engine", "agent"])
        self.assertEqual(result.exit_code, 2, result.output)

    def test_pipeline_needs_review_stops_every_media_stage(self):
        runner = CliRunner()
        candidate = normalize_storyboard({
            "title": "pending", "creative_bible": {"style_anchor": "cinematic", "characters": []},
            "scenes": [{"scene_id": "1", "heading": "INT. ROOM", "shots": json.loads(shots_response())["shots"]}],
        })
        candidate["metadata"]["agent_status"] = "needs_review"
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "script.json")
            with open(source, "w", encoding="utf-8") as handle:
                json.dump({"title": "pending"}, handle)
            with patch("manju.cli.run_storyboard", return_value=candidate), patch(
                "manju.cli.run_voice"
            ) as voice, patch("manju.cli.run_video") as video, patch(
                "manju.cli.run_batch_speak"
            ) as speak, patch("manju.cli.run_generate") as render:
                result = runner.invoke(cli, [
                    "pipeline", "--script", source, "--engine", "agent",
                    "-o", os.path.join(root, "out"),
                ])
            self.assertEqual(result.exit_code, 2, result.output)
            voice.assert_not_called()
            video.assert_not_called()
            speak.assert_not_called()
            render.assert_not_called()

    def test_source_relations_detect_reorder_and_simultaneous_mismatch(self):
        source_model = _build_source_model(["First event happens. Meanwhile second event happens."])
        first, second = [beat["beat_id"] for beat in source_model["beats"]]
        state = {"source_model": source_model}
        reversed_storyboard = {"scenes": [{"scene_id": "1", "shots": [
            {"shot_id": "1.1", "source_beat_ids": [second]},
            {"shot_id": "1.2", "source_beat_ids": [first]},
        ]}]}
        # The declared relation is simultaneous, so order alone must not be
        # treated as a sequential reversal.
        self.assertFalse(any(
            "reversed" in issue["problem"]
            for issue in _semantic_source_issues(state, reversed_storyboard)
        ))
        sequential_model = _build_source_model(["First event happens. Second event happens."])
        first, second = [beat["beat_id"] for beat in sequential_model["beats"]]
        reordered = {"scenes": [{"scene_id": "1", "shots": [
            {"shot_id": "1.1", "source_beat_ids": [second]},
            {"shot_id": "1.2", "source_beat_ids": [first]},
        ]}]}
        self.assertTrue(any(
            "reversed" in issue["problem"]
            for issue in _semantic_source_issues({"source_model": sequential_model}, reordered)
        ))
        simultaneous = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "source_beat_ids": ["beat_0001", "beat_0002"],
            "temporal_relations": [{
                "type": "before", "from_beat_id": "beat_0001", "to_beat_id": "beat_0002",
            }],
        }]}]}
        self.assertTrue(any(
            "simultaneous" in issue["problem"].lower()
            for issue in _semantic_source_issues(state, simultaneous)
        ))

    def test_model_relation_upgrade_requires_exact_explicit_relation_evidence(self):
        source = "First event happens. Second event happens."
        unsupported = _build_source_model([source], [{"beats": [{
            "source_quote": "Second event happens.",
            "relation_to_previous": "simultaneous",
        }]}])
        self.assertEqual(unsupported["relations"][0]["type"], "before")
        supported_source = "First event happens. Meanwhile second event happens."
        supported = _build_source_model([supported_source], [{"beats": [{
            "source_quote": "Meanwhile second event happens.",
            "relation_to_previous": "simultaneous",
            "relation_evidence": "Meanwhile second event happens.",
        }]}])
        self.assertEqual(supported["relations"][0]["type"], "simultaneous")

    def test_chinese_dialogue_segmentation_keeps_quotes_and_removes_phantom_cast(self):
        source = (
            "雨后的天台上，林夏握着一封旧信。"
            "哥哥低声说：“不要相信最后一句话。”"
            "林夏追问：“那我该相信谁？”画面停在哥哥沉默的表情上。"
        )
        sentences = _source_sentences(source)
        self.assertEqual(len(sentences), 4)
        self.assertFalse(any(value in {"”", "\""} for value in sentences))
        analysis = {
            "entities": [
                {"name": "哥哥低声说", "kind": "character", "source_quote": "哥哥低声说"},
                {"name": "信", "kind": "prop", "source_quote": "一封旧信",
                 "description": "没有署名的旧信纸", "required_visual_consistency": True},
            ],
            "beats": [],
        }
        model = _build_source_model([source], [analysis])
        dialogues = [line for beat in model["beats"] for line in beat.get("dialogue", [])]
        self.assertEqual(dialogues, [
            {"speaker": "哥哥", "line": "不要相信最后一句话。"},
            {"speaker": "林夏", "line": "那我该相信谁？"},
        ])
        self.assertEqual(model["integrity_errors"], [])
        enriched = _enrich_source_model_from_plan(model, {
            "creative_bible": {"characters": [
                {"character_id": "character_001", "name": "林夏"},
                {"character_id": "character_002", "name": "哥哥"},
            ]}
        })
        names = {item["name"] for item in enriched["entities"] if item.get("kind") == "character"}
        self.assertNotIn("哥哥低声说", names)
        self.assertEqual(enriched["props"][0]["name"], "信")

    def test_unattributed_dialogue_is_an_explicit_source_integrity_blocker(self):
        model = _build_source_model(["门后传来一句：“别进来。”"], [{"beats": []}])
        self.assertEqual(model["beats"][0]["dialogue"], [
            {"speaker": "", "line": "别进来。"},
        ])
        self.assertEqual(model["integrity_errors"], [
            "unresolved_dialogue_speaker:beat_0001",
        ])

    def test_reported_speech_recovers_explicit_character_subject(self):
        source = "方知禾抬头看了陈屿一眼，将晶片放进口袋，说实验数据需要他签字。"
        model = _build_source_model([source], [{
            "entities": [
                {"name": "方知禾", "kind": "character", "source_quote": "方知禾"},
                {"name": "陈屿", "kind": "character", "source_quote": "陈屿"},
            ],
            "beats": [{
                "source_quote": source,
                "dialogue": [{"speaker": "", "line": "实验数据需要他签字"}],
            }],
        }])
        self.assertEqual(model["beats"][0]["dialogue"], [
            {"speaker": "方知禾", "line": "实验数据需要他签字"},
        ])
        self.assertEqual(model["integrity_errors"], [])

    def test_reported_speech_does_not_mistake_object_for_speaker(self):
        source = "陈屿看向方知禾，说需要马上离开。"
        model = _build_source_model([source], [{
            "entities": [
                {"name": "陈屿", "kind": "character", "source_quote": "陈屿"},
                {"name": "方知禾", "kind": "character", "source_quote": "方知禾"},
            ],
            "beats": [{
                "source_quote": source,
                "dialogue": [{"speaker": "", "line": "需要马上离开"}],
            }],
        }])
        self.assertEqual(model["beats"][0]["dialogue"][0]["speaker"], "陈屿")
        self.assertEqual(model["integrity_errors"], [])

    def test_review_projection_is_valid_json_and_keeps_the_final_shot(self):
        final_prompt = "最终镜头" + "细节" * 30000
        storyboard = {
            "schema_version": "2.0",
            "scenes": [{
                "scene_id": "2",
                "shots": [{
                    "shot_id": "2.5",
                    "source_beat_ids": ["beat_0012"],
                    "visual": {"description": "晨光落在晶片上"},
                    "prompts": {"image_cn": final_prompt, "video": "omitted duplicate"},
                }],
            }],
        }
        payload = _review_audit_payload({
            "beats": [{"beat_id": "beat_0012", "text": "晨光落在晶片上"}],
        }, storyboard)
        serialized = json.dumps(payload, ensure_ascii=False)
        reparsed = json.loads(serialized)
        projected_shot = reparsed["storyboard"]["scenes"][0]["shots"][0]
        self.assertEqual(projected_shot["shot_id"], "2.5")
        self.assertTrue(projected_shot["prompts"]["image_cn"].startswith("最终镜头"))
        self.assertIn("[FIELD_EXCERPT_CLIPPED original_chars=", projected_shot["prompts"]["image_cn"])
        self.assertNotIn("video", projected_shot["prompts"])
        self.assertTrue(reparsed["payload_contract"]["complete_scene_and_shot_list"])
        self.assertTrue(_is_model_schema_claim({"category": "schema_integrity"}))
        self.assertFalse(_is_model_schema_claim({"category": "source_fidelity"}))

    def test_common_chinese_entity_kind_aliases_preserve_key_props(self):
        source = "她把一封旧信锁进抽屉。"
        model = _build_source_model([source], [{
            "entities": [
                {"name": "信", "kind": "关键道具", "source_quote": "一封旧信"},
                {"name": "抽屉", "kind": "物件", "source_quote": "抽屉"},
            ]
        }])
        self.assertEqual([item["name"] for item in model["props"]], ["信", "抽屉"])

    def test_source_model_preserves_asset_kind_physical_spec_and_lifecycle(self):
        source = "她拿起旧信。她一直拿着旧信走到门边。"
        model = _build_source_model([source], [{
            "entities": [{
                "name": "旧信", "kind": "prop", "source_quote": "旧信",
                "asset_kind": "portable_prop", "aliases": ["旧信"],
                "physical_spec": {"object_class": "paper letter", "opacity": "opaque"},
                "lifecycle": [{
                    "source_quote": "她拿起旧信。", "visible": True,
                    "persists": True, "holder": "她", "state": "held",
                }],
            }], "beats": [],
        }])
        prop = model["props"][0]
        self.assertEqual(prop["asset_kind"], "portable_prop")
        self.assertEqual(prop["physical_spec"]["object_class"], "paper letter")
        self.assertTrue(prop["lifecycle"][0]["persists"])
        scene = _prepare_scene_semantics({"source_model": model}, {
            "scene_id": "1", "source_beat_ids": ["beat_0001", "beat_0002"],
            "shots": [{
                "shot_id": "1.1", "source_beat_ids": ["beat_0002"],
                "visual": {"description": "She reaches the door", "key_props": []},
                "prompts": {},
            }],
        })
        self.assertEqual(scene["shots"][0]["visible_prop_ids"], [prop["prop_id"]])

    def test_non_persistent_lifecycle_event_stops_future_visibility_propagation(self):
        source_model = {
            "beats": [
                {"beat_id": "beat_0001", "text": "A courier picks up a parcel."},
                {"beat_id": "beat_0002", "text": "The parcel is shown once more."},
                {"beat_id": "beat_0003", "text": "A different person fills the frame."},
            ],
            "relations": [],
            "entities": [{
                "entity_id": "entity_001", "kind": "character", "name": "Courier",
                "character_id": "character_001", "aliases": [],
            }],
            "props": [{
                "prop_id": "prop_001", "name": "parcel", "asset_kind": "portable_prop",
                "beat_ids": ["beat_0001"],
                "lifecycle": [
                    {"beat_id": "beat_0001", "visible": True, "persists": True, "holder": "Courier"},
                    {"beat_id": "beat_0002", "visible": True, "persists": False, "holder": "Courier"},
                ],
            }],
        }
        scene = _prepare_scene_semantics({"source_model": source_model}, {
            "scene_id": "1", "source_beat_ids": ["beat_0001", "beat_0002", "beat_0003"],
            "shots": [{
                "shot_id": "1.1", "source_beat_ids": ["beat_0003"],
                "visual": {"visible_character_ids": ["character_002"], "key_props": []},
                "prompts": {},
            }],
        })
        self.assertEqual(scene["shots"][0]["visible_prop_ids"], [])
        self.assertEqual(scene["shots"][0]["visual"]["key_props"], [])

    def test_persistent_handheld_prop_is_not_forced_when_holder_is_offscreen(self):
        source_model = {
            "beats": [
                {"beat_id": "beat_0001", "text": "A courier holds a parcel."},
                {"beat_id": "beat_0002", "text": "Another face fills the frame."},
            ],
            "relations": [],
            "entities": [{
                "entity_id": "entity_001", "kind": "character", "name": "Courier",
                "character_id": "character_001", "aliases": [],
            }],
            "props": [{
                "prop_id": "prop_001", "name": "parcel", "asset_kind": "portable_prop",
                "beat_ids": ["beat_0001"],
                "lifecycle": [{
                    "beat_id": "beat_0001", "visible": True, "persists": True, "holder": "Courier",
                }],
            }],
        }
        scene = _prepare_scene_semantics({"source_model": source_model}, {
            "scene_id": "1", "source_beat_ids": ["beat_0001", "beat_0002"],
            "shots": [{
                "shot_id": "1.1", "source_beat_ids": ["beat_0002"],
                "visual": {"visible_character_ids": ["character_002"], "key_props": []},
                "prompts": {},
            }],
        })
        self.assertEqual(scene["shots"][0]["visible_prop_ids"], [])

    def test_source_quote_grounds_a_normalized_entity_name_and_registry_prop(self):
        source = "她收到一封没有署名的信。"
        model = _build_source_model([source], [{
            "entities": [{
                "name": "无署名的信",
                "kind": "prop",
                "source_quote": "一封没有署名的信",
                "asset_kind": "story_key_prop",
                "lifecycle": [{
                    "source_quote": "她收到一封没有署名的信。",
                    "state": "received",
                    "visible": True,
                }],
            }],
            "beats": [],
        }])

        self.assertEqual(len(model["props"]), 1)
        prop = model["props"][0]
        self.assertEqual(prop["name"], "无署名的信")
        self.assertEqual(prop["source_quote"], "一封没有署名的信")
        self.assertEqual(prop["beat_ids"], ["beat_0001"])
        self.assertEqual(prop["lifecycle"][0]["beat_id"], "beat_0001")

    def test_asset_binding_without_canonical_registry_anchor_is_not_scene_revisable(self):
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "visible_prop_ids": [],
        }]}]}
        review = _normalize_model_issues({"issues": [{
            "issue_id": "missing_registry_asset",
            "scene_id": "1",
            "shot_ids": ["1.1"],
            "category": "asset_binding",
            "severity": "medium",
            "blocking": False,
            "problem": "A source-owned visible asset has no canonical binding.",
            "source_evidence": [{"beat_id": "beat_0001"}],
            "storyboard_evidence": [{
                "path": "$.scenes[0].shots[0].visible_prop_ids", "value": [],
            }],
        }]}, default_category="shootability", source_model={
            "beats": [{"beat_id": "beat_0001", "text": "A source-owned asset appears."}],
            "props": [],
        }, storyboard=storyboard)

        issue = review["blocking_issues"][0]
        self.assertFalse(issue["auto_revisable"])
        self.assertTrue(issue["registry_repair_required"])

    def test_source_model_rejects_fields_from_a_quote_spanning_adjacent_beats(self):
        source = "林夏打开信。哥哥说：\u201c别看结尾。\u201d画面停在门外的影子上。"
        analysis = {"beats": [{
            "source_quote": source,
            "must_preserve_facts": ["画面停在门外的影子上"],
            "dialogue": [{"speaker": "哥哥", "line": "别看结尾。"}],
        }]}
        model = _build_source_model([source], [analysis])
        self.assertFalse(any("must_preserve_facts" in beat for beat in model["beats"]))
        ending = model["ending_constraint"]
        self.assertEqual(ending["beat_id"], model["beats"][-1]["beat_id"])
        self.assertEqual(model["integrity_errors"], [])

    def test_focus_transfer_is_not_camera_motion_but_real_locked_camera_motion_is(self):
        def board(movement):
            return {"creative_bible": {"characters": []}, "scenes": [{
                "scene_id": "1", "shots": [{
                    "shot_id": "1.1", "duration_seconds": 3,
                    "visual": {"camera_movement": movement, "shot_type": "近景"},
                    "audio": {}, "prompts": {},
                }],
            }]}
        focus = _deterministic_quality_issues(
            board("固定机位，焦点从林夏转移到哥哥，不发生推拉摇移")
        )
        self.assertFalse(any(item["code"] == "camera_conflict" for item in focus))
        conflict = _deterministic_quality_issues(board("固定机位，同时镜头推进到人物面部"))
        self.assertTrue(any(item["code"] == "camera_conflict" for item in conflict))

    def test_explicit_low_budget_records_closure_warning(self):
        effective, factors = calculate_agent_call_budget(1, 1, 2, 10)
        self.assertEqual(effective, 10)
        self.assertEqual(factors["warning"], "explicit_budget_below_automatic_safe_default")
        self.assertEqual(factors["recommended_minimum_calls"], 22)

    def test_key_props_propagate_to_scene_and_shot_semantics(self):
        source_model = {
            "beats": [{"beat_id": "beat_0001", "text": "A courier carries a sealed package."}],
            "relations": [],
            "props": [{
                "prop_id": "prop_001", "name": "sealed package",
                "description": "brown paper parcel", "beat_ids": ["beat_0001"],
                "continuity_required": True,
            }],
        }
        scene = _prepare_scene_semantics({"source_model": source_model}, {
            "scene_id": "1", "source_beat_ids": ["beat_0001"],
            "shots": [{"shot_id": "1.1", "source_beat_ids": ["beat_0001"], "visual": {}}],
        })
        self.assertEqual(scene["key_props"][0]["prop_id"], "prop_001")
        self.assertEqual(scene["shots"][0]["visual"]["key_props"][0]["prop_id"], "prop_001")
        self.assertEqual(scene["shots"][0]["visible_prop_ids"], ["prop_001"])
        normalized = normalize_storyboard({
            "creative_bible": {"style_anchor": "cinematic", "characters": []},
            "scenes": [{**scene, "heading": "EXT. STREET", "shots": [{
                **scene["shots"][0], "visual": {
                    **scene["shots"][0]["visual"], "description": "Courier holds package",
                    "shot_type": "medium", "composition": "center", "camera_movement": "fixed",
                    "color_tone": "neutral",
                },
                "prompts": {"image_cn": "快递员拿着包裹", "image_en": "courier with parcel"},
            }]}],
        })
        self.assertEqual(normalized["scenes"][0]["shots"][0]["visual"]["key_props"][0]["prop_id"], "prop_001")

    def test_invented_prop_ids_map_to_canonical_registry_and_wardrobe_stays_on_character(self):
        source_model = {
            "beats": [
                {"beat_id": "beat_0001", "text": "A wears a coat and holds a letter."},
                {"beat_id": "beat_0002", "text": "A door opens."},
            ],
            "relations": [],
            "props": [
                {"prop_id": "coat", "name": "coat", "asset_kind": "wardrobe",
                 "beat_ids": ["beat_0001"]},
                {"prop_id": "letter", "name": "letter", "asset_kind": "portable_prop",
                 "beat_ids": ["beat_0001"]},
                {"prop_id": "door", "name": "door", "asset_kind": "set_piece",
                 "beat_ids": ["beat_0002"]},
            ],
        }
        scene = _prepare_scene_semantics({"source_model": source_model}, {
            "scene_id": "1", "source_beat_ids": ["beat_0001", "beat_0002"],
            "shots": [
                {"shot_id": "1.1", "source_beat_ids": ["beat_0001"],
                 "visible_prop_ids": ["invented_letter"],
                 "visual": {"description": "A holds a letter while wearing a coat", "key_props": [
                     {"prop_id": "invented_letter", "name": "letter"},
                 ]}, "prompts": {}},
                {"shot_id": "1.2", "source_beat_ids": ["beat_0002"],
                 "visible_prop_ids": ["invented_door"],
                 "visual": {"description": "The door opens", "key_props": []}, "prompts": {}},
            ],
        })
        self.assertEqual(scene["shots"][0]["visible_prop_ids"], ["letter"])
        self.assertEqual(scene["shots"][1]["visible_prop_ids"], ["door"])
        self.assertNotIn("coat", scene["shots"][0]["visible_prop_ids"])

    def test_shot_props_are_scoped_by_beat_and_explicit_empty_is_preserved(self):
        source_model = {
            "beats": [
                {"beat_id": "beat_0001", "text": "A sealed package arrives."},
                {"beat_id": "beat_0002", "text": "A gate closes."},
            ],
            "relations": [],
            "props": [
                {"prop_id": "package", "name": "sealed package", "beat_ids": ["beat_0001"],
                 "continuity_required": True},
                {"prop_id": "gate", "name": "gate", "beat_ids": ["beat_0002"],
                 "continuity_required": True},
            ],
        }
        scene = _prepare_scene_semantics({"source_model": source_model}, {
            "scene_id": "1", "source_beat_ids": ["beat_0001", "beat_0002"],
            "shots": [
                {"shot_id": "1.1", "source_beat_ids": ["beat_0001"], "visual": {}},
                {"shot_id": "1.2", "source_beat_ids": ["beat_0002"],
                 "visual": {"key_props": []}},
            ],
        })
        self.assertEqual(
            [item["prop_id"] for item in scene["shots"][0]["visual"]["key_props"]],
            ["package"],
        )
        self.assertEqual(scene["shots"][1]["visual"]["key_props"], [])

    def test_exact_dialogue_requires_matching_beat_id_on_same_shot(self):
        source_model = {"beats": [
            {"beat_id": "beat_0001", "text": "A says: keep moving.",
             "dialogue": [{"speaker": "A", "line": "keep moving"}], "required": True},
            {"beat_id": "beat_0002", "text": "B closes the door.", "required": True},
        ], "relations": []}
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "source_beat_ids": ["beat_0002"],
            "audio": {"dialogue": "Keep moving!"}, "visual": {}, "prompts": {},
        }]}]}
        issues = _semantic_source_issues({"source_model": source_model}, storyboard)
        alignment = [item for item in issues if "contains dialogue" in item["problem"]]
        self.assertEqual(len(alignment), 1)
        self.assertEqual(alignment[0]["scene_id"], "1")
        self.assertEqual(alignment[0]["shot_ids"], ["1.1"])

    def test_exact_source_sound_requires_matching_beat_id_on_same_shot(self):
        source_model = {"beats": [
            {"beat_id": "beat_0001", "text": "A distant bell rings.",
             "sounds": ["distant bell"], "required": True},
            {"beat_id": "beat_0002", "text": "A looks down.", "required": True},
        ], "relations": []}
        storyboard = {"scenes": [{"scene_id": "1", "shots": [{
            "shot_id": "1.1", "source_beat_ids": ["beat_0002"],
            "audio": {"sound_music": "A distant bell echoes"}, "visual": {}, "prompts": {},
        }]}]}
        issues = _semantic_source_issues({"source_model": source_model}, storyboard)
        alignment = [item for item in issues if "contains source sound" in item["problem"]]
        self.assertEqual(len(alignment), 1)
        self.assertEqual(alignment[0]["shot_ids"], ["1.1"])

    def test_optional_semantic_fields_survive_v2_normalization(self):
        payload = normalize_storyboard({
            "creative_bible": {"style_anchor": "cinematic", "characters": [{
                "character_id": "character_001", "name": "A",
            }]},
            "scenes": [{
                "scene_id": "1", "heading": "INT. ROOM", "source_beat_ids": ["beat_0001"],
                "shots": [{
                    **json.loads(shots_response())["shots"][0],
                    "source_beat_ids": ["beat_0001"],
                    "visible_prop_ids": ["prop_001"],
                    "temporal_relations": [{"type": "simultaneous"}],
                    "visual": {
                        **json.loads(shots_response())["shots"][0]["visual"],
                        "visible_character_ids": ["character_001"],
                    },
                }],
            }],
        })
        self.assertEqual(payload["creative_bible"]["characters"][0]["character_id"], "character_001")
        self.assertEqual(payload["scenes"][0]["source_beat_ids"], ["beat_0001"])
        self.assertEqual(payload["scenes"][0]["shots"][0]["source_beat_ids"], ["beat_0001"])
        self.assertEqual(payload["scenes"][0]["shots"][0]["visible_prop_ids"], ["prop_001"])
        self.assertEqual(
            payload["scenes"][0]["shots"][0]["visual"]["visible_character_ids"],
            ["character_001"],
        )

    def test_planning_defect_is_revised_at_plan_level(self):
        broken = json.loads(plan_response())
        broken["scenes"] = []
        responses = [
            action("analyze_source"),
            action("create_plan"),
            json.dumps(broken),
            action("revise_plan"),
            plan_response(),
            action("generate_scenes"),
            shots_response(),
            action("assemble_storyboard"),
            action("validate_schema"),
            action("compare_source"),
            action("inspect_shootability"),
            action("review_storyboard"),
            CLEAN_REVIEW,
            action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses)
            actions = [event["action"] for event in self._trace(root)]

            self.assertIsNotNone(result)
            self.assertIn("revise_plan", actions)
            self.assertNotIn("revise_scenes", actions)

    def test_different_stories_can_take_different_tool_paths(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            self._run(first_root, [
                action("analyze_source"),
                action("stop_needs_review", {"reason": "mystery needs a human clue check"}),
            ], text="A mystery depends on an ambiguous handwritten clue.")
            self._run(second_root, [
                action("stop_needs_review", {"reason": "simple source is not ready"}),
            ], text="A simple visual vignette.")

            first_path = [event["action"] for event in self._trace(first_root)]
            second_path = [event["action"] for event in self._trace(second_root)]
            self.assertNotEqual(first_path, second_path)

    def test_revision_limit_is_enforced_per_scene(self):
        responses = [
            action("analyze_source"),
            action("create_plan"),
            plan_response(),
            action("generate_scenes"),
            shots_response(),
            action("assemble_storyboard"),
            action("review_storyboard"),
            blocking_review(),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            revision_shots_response("first targeted revision"),
            action("review_storyboard"),
            blocking_review(value="first targeted revision"),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            revision_shots_response("second targeted revision"),
            action("review_storyboard"),
            blocking_review(value="second targeted revision"),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            action("stop_needs_review", {"reason": "revision limit reached"}),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses)

            self.assertIsNotNone(result)
            manifest = self._manifest(root)
            self.assertEqual(manifest["revision_counts"], {})
            self.assertEqual(manifest["revision_attempt_counts"], {"1": 2})
            third = [event for event in self._trace(root) if event["action"] == "revise_scenes"][-1]
            self.assertEqual(third["result"]["revision_limit_scene_ids"], ["1"])

    def test_improving_revision_gets_one_bounded_convergence_extension(self):
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), shots_response(), action("assemble_storyboard"),
            action("review_storyboard"), blocking_review(issue_id="model_first"),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_first"]}),
            revision_shots_response("first repaired candidate"),
            blocking_review(
                issue_id="model_residual", value="first repaired candidate", problem="smaller residual blocker",
            ),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_residual"]}),
            revision_shots_response("extension repaired candidate"),
            CLEAN_REVIEW,
            action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses, max_revisions=1)
            self.assertIsNotNone(result)
            manifest = self._manifest(root)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["revision_attempt_counts"], {"1": 2})
            self.assertEqual(manifest["revision_extension_counts"], {"1": 1})

    def test_storyboard_stop_requires_specific_reason(self):
        args, error = _validate_action_args("stop_needs_review", {})
        self.assertEqual(args, {})
        self.assertEqual(error["error"], "invalid_action_args")
        args, error = _validate_action_args(
            "stop_needs_review", {"reason": "A human must resolve the remaining objective blocker."}
        )
        self.assertIsNone(error)

    def test_two_targeted_revisions_leave_budget_for_audit_and_finalize(self):
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), shots_response(), action("assemble_storyboard"),
            action("review_storyboard"), blocking_review(issue_id="model_first"),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_first"]}),
            revision_shots_response("first repaired candidate"),
            action("review_storyboard"), blocking_review(
                issue_id="model_second", value="first repaired candidate", problem="second blocker",
            ),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_second"]}),
            revision_shots_response("second repaired candidate"),
            action("review_storyboard"), CLEAN_REVIEW,
            action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, responses)
            manifest = self._manifest(root)
            self.assertIsNotNone(result)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["revision_attempt_counts"], {"1": 2})
            self.assertEqual(manifest["revision_counts"], {"1": 2})
            self.assertLessEqual(calls, manifest["budgets"]["effective_max_calls"])
            self.assertGreaterEqual(manifest["budgets"]["remaining_calls"], 0)

    def test_empty_revision_contract_retries_with_a_new_cache_record(self):
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), shots_response(), action("assemble_storyboard"),
            action("review_storyboard"), blocking_review(),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            json.dumps({"shots": []}),
            revision_shots_response("contract retry repaired the scene"),
            action("review_storyboard"), CLEAN_REVIEW, action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses)
            self.assertIsNotNone(result)
            manifest = self._manifest(root)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["revision_attempt_counts"], {"1": 1})
            self.assertEqual(manifest["failed_revision_contract_count"], 0)
            call_dir = os.path.join(root, "stages", "agent")
            invalid_records = []
            for current, _, files in os.walk(call_dir):
                for name in files:
                    if not name.endswith(".json"):
                        continue
                    path = os.path.join(current, name)
                    with open(path, encoding="utf-8") as handle:
                        record = json.load(handle)
                    if record.get("status") == "invalid_contract":
                        invalid_records.append(record)
            self.assertEqual(len(invalid_records), 1)
            self.assertIn("shots_must_be_a_non_empty_array", invalid_records[0]["contract_errors"])

    def test_revision_contract_rejects_multi_beat_scene_collapse(self):
        beat_ids = {f"beat_{index:04d}" for index in range(1, 8)}
        collapsed = json.loads(shots_response("only the targeted beat remains"))
        collapsed["shots"][0]["source_beat_ids"] = ["beat_0001"]

        errors = _scene_shots_contract_errors(
            collapsed,
            scene_id="1",
            allowed_beat_ids=beat_ids,
            require_source_beat_ids=True,
            required_beat_ids=beat_ids,
            required_shot_ids={"1.2", "1.3"},
        )

        self.assertIn(
            "scene.source_beat_ids_missing_required:"
            "beat_0002,beat_0003,beat_0004,beat_0005,beat_0006,beat_0007",
            errors,
        )
        self.assertIn("scene.untargeted_shot_ids_missing:1.2,1.3", errors)

    def test_partial_scene_revision_retries_before_replacing_original_shots(self):
        original = json.loads(shots_response("targeted shot before repair"))
        protected = json.loads(revision_shots_response("untargeted shot must survive"))["shots"][0]
        protected["shot_id"] = "1.2"
        original["shots"].append(protected)

        collapsed = json.loads(revision_shots_response("targeted shot repaired but scene collapsed"))
        repaired = json.loads(revision_shots_response("targeted shot repaired"))
        repaired["shots"].append(protected)
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), json.dumps(original), action("assemble_storyboard"),
            action("review_storyboard"), blocking_review(value="targeted shot before repair"),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            json.dumps(collapsed), json.dumps(repaired),
            action("review_storyboard"), CLEAN_REVIEW, action("finalize"),
        ]

        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses, max_revisions=1)

            self.assertIsNotNone(result)
            self.assertEqual(self._manifest(root)["status"], "completed")
            shots = result["scenes"][0]["shots"]
            self.assertEqual([shot["shot_id"] for shot in shots], ["1.1", "1.2"])
            self.assertEqual(shots[1]["visual"]["description"], "untargeted shot must survive")
            call_dir = os.path.join(root, "stages", "agent")
            invalid_records = []
            for current, _, files in os.walk(call_dir):
                for name in files:
                    if name.endswith(".json"):
                        with open(os.path.join(current, name), encoding="utf-8") as handle:
                            record = json.load(handle)
                        if record.get("status") == "invalid_contract":
                            invalid_records.append(record)
            self.assertTrue(any(
                "scene.untargeted_shot_ids_missing:1.2" in record.get("contract_errors", [])
                for record in invalid_records
            ))

    def test_repeated_invalid_revision_is_counted_and_reported_explicitly(self):
        responses = [
            action("analyze_source"), action("create_plan"), plan_response(),
            action("generate_scenes"), shots_response(), action("assemble_storyboard"),
            action("review_storyboard"), blocking_review(),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            json.dumps({"shots": []}), json.dumps({"shots": []}),
            action("stop_needs_review", {"reason": "revision contract failed"}),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses)
            self.assertIsNotNone(result)
            manifest = self._manifest(root)
            self.assertEqual(manifest["status"], "needs_review")
            self.assertEqual(manifest["revision_attempt_counts"], {"1": 1})
            self.assertEqual(manifest["failed_revision_contract_count"], 1)
            revision = [event for event in self._trace(root) if event["action"] == "revise_scenes"][0]
            self.assertEqual(revision["result"]["error"], "revision_contract_failed")

    def test_scene_revision_only_changes_the_target_scene(self):
        responses = [
            action("analyze_source"),
            action("create_plan"),
            multi_scene_plan(2),
            action("generate_scenes"),
            scene_shots("1", camera="static locked camera with pan"),
            scene_shots("2", description="Lin keeps the second scene intact"),
            action("assemble_storyboard"),
            action("review_storyboard"),
            blocking_review(value="Lin performs story beat 1"),
            action("revise_scenes", {"scene_ids": ["1"], "issue_ids": ["model_mock"]}),
            revision_shots_response("Lin turns once"),
            action("review_storyboard"),
            CLEAN_REVIEW,
            action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, responses, scenes=2)

            self.assertIsNotNone(result)
            by_id = {scene["scene_id"]: scene for scene in result["scenes"]}
            self.assertEqual(by_id["1"]["shots"][0]["visual"]["description"], "Lin turns once")
            self.assertEqual(
                by_id["2"]["shots"][0]["visual"]["description"],
                "Lin keeps the second scene intact",
            )
            manifest = self._manifest(root)
            self.assertEqual(manifest["revision_counts"], {"1": 1})
            self.assertEqual(manifest["revision_attempt_counts"], {"1": 1})

    def test_three_invalid_actions_stop_and_media_action_is_rejected(self):
        secret = "mock-secret-never-persist"
        responses = [
            action("render_videos", {"api_key": secret}),
            action("render_videos", {"api_key": secret}),
            action("render_videos", {"api_key": secret}),
        ]
        with tempfile.TemporaryDirectory() as root, patch(
            "manju.pipeline.storyboard_supervisor.get_ai_config",
            return_value=("https://example.invalid/v1/chat/completions", "mock", secret),
        ):
            result, _ = self._run(root, responses)

            self.assertIsNone(result)
            self.assertEqual(self._manifest(root)["stop_reason"], "invalid_action_limit")
            self.assertTrue(all(event["result"]["error"] == "invalid_action" for event in self._trace(root)))
            with open(os.path.join(root, "agent_trace.jsonl"), "rb") as handle:
                self.assertNotIn(secret.encode(), handle.read())
            with open(os.path.join(root, "stages", "agent", "checkpoints.sqlite"), "rb") as handle:
                self.assertNotIn(secret.encode(), handle.read())

    def test_same_no_progress_error_stops_after_three_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, [
                action("assemble_storyboard"),
                action("assemble_storyboard"),
                action("assemble_storyboard"),
            ])

            self.assertIsNone(result)
            self.assertEqual(self._manifest(root)["stop_reason"], "no_progress_limit")

    def test_unresolved_source_integrity_stops_without_reanalysis_loop(self):
        text = "门后传来一句：“别进来。”"
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, [action("analyze_source")], text=text)

            self.assertIsNone(result)
            manifest = self._manifest(root)
            self.assertEqual(manifest["stop_reason"], "source_model_integrity_failed")
            self.assertEqual(manifest["tool_steps"], 1)
            self.assertEqual(manifest["source_model_integrity_errors"], [
                "unresolved_dialogue_speaker:beat_0001",
            ])
            event = self._trace(root)[0]
            self.assertEqual(event["result"]["error"], "source_model_integrity_failed")

    def test_premature_finalize_is_denied_by_code_owned_gates(self):
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, [
                action("finalize"), action("finalize"), action("finalize"),
            ])

            self.assertIsNone(result)
            events = self._trace(root)
            self.assertEqual(events[0]["result"]["error"], "completion_gates_failed")
            self.assertIn("valid scene plan", " ".join(events[0]["result"]["issues"]))

    def test_step_and_call_budgets_stop_with_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as root:
            result, _ = self._run(root, [action("analyze_source")], max_steps=1)
            self.assertIsNone(result)
            self.assertEqual(self._manifest(root)["stop_reason"], "budget_exhausted")
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [action("analyze_source")], max_calls=1)
            self.assertIsNone(result)
            self.assertEqual(calls, 1)
            self.assertEqual(self._manifest(root)["stop_reason"], "budget_exhausted")

    def test_budget_change_creates_a_new_run_fingerprint(self):
        with tempfile.TemporaryDirectory() as root:
            self._run(root, [action("analyze_source")], max_steps=1)
            first = self._manifest(root)["run_id"]
            self._run(root, [action("analyze_source"), action("stop_needs_review")], max_steps=2)
            second = self._manifest(root)["run_id"]

            self.assertNotEqual(first, second)
            run_dirs = [name for name in os.listdir(os.path.join(root, "stages", "agent")) if name.startswith("run_")]
            self.assertEqual(len(run_dirs), 2)

    def test_eight_scene_story_completes_with_default_call_budget(self):
        responses = [
            action("analyze_source"),
            action("create_plan"),
            multi_scene_plan(8),
            action("generate_scenes"),
            *[scene_shots(str(index)) for index in range(1, 9)],
            action("assemble_storyboard"),
            action("validate_schema"),
            action("compare_source"),
            action("inspect_shootability"),
            action("review_storyboard"),
            CLEAN_REVIEW,
            action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, responses, scenes=8)

            self.assertIsNotNone(result)
            self.assertEqual(len(result["scenes"]), 8)
            self.assertLessEqual(calls, 20)

    def test_long_source_extracts_each_chunk_independently(self):
        long_source = ("A story beat continues. " * 2100) + "The story ends safely."
        responses = [
            action("analyze_source"),
            action("create_plan"),
            plan_response(),
            action("generate_scenes"),
            shots_response(),
            action("assemble_storyboard"),
            action("validate_schema"),
            action("compare_source"),
            action("inspect_shootability"),
            action("review_storyboard"),
            CLEAN_REVIEW,
            action("finalize"),
        ]
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, responses, text=long_source)

            self.assertIsNotNone(result)
            self.assertEqual(calls, 14)

    def test_resume_reuses_cached_scene_call_after_node_failure(self):
        first_responses = [
            action("analyze_source"),
            source_analysis_response(),
            action("create_plan"),
            plan_response(),
            action("generate_scenes"),
            shots_response(),
        ]
        second_responses = [
            action("assemble_storyboard"),
            action("validate_schema"),
            action("compare_source"),
            action("inspect_shootability"),
            action("review_storyboard"),
            CLEAN_REVIEW,
            action("finalize"),
        ]
        from manju.pipeline.storyboard_agent import _prepare_generated_scene as real_prepare

        with tempfile.TemporaryDirectory() as root:
            with patch(
                "manju.pipeline.storyboard_supervisor.call_llm",
                side_effect=first_responses,
            ) as first_mock, patch(
                "manju.pipeline.storyboard_supervisor._prepare_generated_scene",
                side_effect=OSError("injected node failure"),
            ):
                failed = generate_storyboard_agent(
                    SOURCE, "Mock Story", len(SOURCE), 1,
                    os.path.join(root, "stages", "agent"), root,
                )
            self.assertIsNone(failed)
            self.assertEqual(first_mock.call_count, 6)

            with patch(
                "manju.pipeline.storyboard_supervisor.call_llm",
                side_effect=second_responses,
            ) as second_mock, patch(
                "manju.pipeline.storyboard_supervisor._prepare_generated_scene",
                side_effect=real_prepare,
            ):
                resumed = generate_storyboard_agent(
                    SOURCE, "Mock Story", len(SOURCE), 1,
                    os.path.join(root, "stages", "agent"), root,
                )

            self.assertIsNotNone(resumed)
            self.assertEqual(second_mock.call_count, len(second_responses))
            self.assertEqual(self._manifest(root)["model_calls"], 13)


if __name__ == "__main__":
    unittest.main()
