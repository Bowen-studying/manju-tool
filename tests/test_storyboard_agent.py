import json
import os
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from manju.cli import cli
from manju.pipeline.storyboard_agent import generate_storyboard_agent
from manju.pipeline.storyboard_schema import normalize_storyboard, validate_storyboard


SOURCE = "Lin runs to the rooftop and hears the door open behind her."


def plan_response(title="Mock Story"):
    return json.dumps({
        "title": title,
        "creative_bible": {
            "style_anchor": "cinematic anime, grounded lighting",
            "aspect_ratio": "9:16",
            "characters": [{
                "name": "Lin",
                "name_en": "Lin Xia",
                "role": "lead",
                "anchor_description": "女性，短黑发，鹅蛋脸，深色眼睛，穿红色风衣",
                "anchor_description_en": "woman, short black hair, oval face, dark eyes, wearing a red trench coat",
            }],
        },
        "scenes": [{
            "scene_id": "1",
            "heading": "EXT. ROOFTOP - SUNSET",
            "purpose": "escape",
            "visual_mood": "tense",
            "scene_template": "orange sunset, concrete rooftop",
            "source_chunk_ids": [1],
            "continuity": {"from_previous": "", "to_next": ""},
        }],
    })


def shots_response(description="Lin turns toward the rooftop door"):
    return json.dumps({"shots": [{
        "shot_id": "1.1",
        "duration_seconds": 3,
        "visual": {
            "shot_type": "medium",
            "composition": "rule of thirds",
            "composition_emotion": "trapped",
            "camera_movement": "slow push",
            "description": description,
            "color_tone": "orange and blue",
        },
        "audio": {
            "speaker": "Lin",
            "dialogue": "Who is there?",
            "narration": "",
            "sound_music": "wind",
        },
        "prompts": {
            "image_cn": "夕阳天台上的短黑发红衣女孩",
            "image_en": "short black-haired woman in a red coat on a rooftop at sunset",
            "video": "hair and coat moving in the wind",
            "video_cn": "头发和风衣在风中摆动，镜头缓慢推近",
            "video_en": "hair and red coat moving in the wind as the camera slowly pushes in",
        },
        "assets": {"image": "", "voice": "", "video": ""},
        "status": {"image": "pending", "voice": "pending", "video": "pending"},
    }]})


def review_response(decision="accept", targets=None):
    targets = targets or []
    return json.dumps({
        "decision": decision,
        "summary": "A concrete continuity issue exists." if decision == "revise" else "Valid.",
        "issues": ([{
            "scene_id": "1",
            "shot_ids": ["1.1"],
            "severity": "high",
            "category": "continuity",
            "blocking": True,
            "problem": "The action is ambiguous.",
            "instruction": "Make the turn and door movement explicit.",
        }] if decision == "revise" else []),
        "target_scene_ids": targets,
    })


class LangGraphStoryboardTests(unittest.TestCase):
    def _run(self, root, responses, *, resume=True, text=SOURCE):
        stage_dir = os.path.join(root, "stages", "agent")
        with patch(
            "manju.pipeline.storyboard_agent.call_llm",
            side_effect=responses,
        ) as mocked:
            result = generate_storyboard_agent(
                text,
                "Mock Story",
                len(text),
                1,
                stage_dir,
                root,
                resume=resume,
            )
        return result, mocked.call_count

    def test_graph_writes_outputs_and_resume_avoids_model_calls(self):
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(
                root,
                [plan_response(), shots_response(), review_response()],
            )
            resumed, resumed_calls = self._run(root, [])

            self.assertEqual(calls, 3)
            self.assertEqual(resumed_calls, 0)
            self.assertEqual(validate_storyboard(result), [])
            self.assertEqual(resumed, result)
            self.assertTrue(os.path.isfile(os.path.join(root, "review.json")))
            self.assertTrue(os.path.isfile(os.path.join(root, "agent_run.json")))
            self.assertTrue(os.path.isfile(
                os.path.join(root, "stages", "agent", "checkpoints.sqlite")
            ))
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["agent_version"], "6")
            self.assertEqual(manifest["status"], "completed")
            self.assertNotIn("api_key", json.dumps(manifest).lower())

    def test_revisions_stop_after_two_rounds(self):
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                plan_response(),
                shots_response(),
                review_response("revise", ["1"]),
                shots_response("Lin turns as the door visibly swings open"),
                review_response("revise", ["1"]),
                shots_response("Lin turns; the rooftop door swings open behind her"),
                review_response("revise", ["1"]),
            ])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 7)
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["revision_count"], 2)
            self.assertEqual(manifest["status"], "needs_review")
            self.assertEqual(manifest["review_decision"], "revise")

    def test_deterministic_camera_gate_triggers_revision(self):
        bad_shots = json.loads(shots_response())
        bad_shots["shots"][0]["visual"]["camera_movement"] = "static camera with a slow dolly push"
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                plan_response(),
                json.dumps(bad_shots),
                shots_response("Lin turns once toward the opening door"),
                review_response(),
            ])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 4)
            self.assertEqual(result["scenes"][0]["shots"][0]["visual"]["camera_movement"], "slow push")
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["revision_count"], 1)
            with open(
                os.path.join(root, "stages", "agent", "latest.json"),
                encoding="utf-8",
            ) as handle:
                latest = json.load(handle)
            with open(
                os.path.join(
                    root,
                    "stages",
                    "agent",
                    latest["run_dir"],
                    "04_review_00.json",
                ),
                encoding="utf-8",
            ) as handle:
                first_review = json.load(handle)
            self.assertEqual(first_review["decision"], "revise")
            self.assertIn(
                "camera_conflict",
                [issue.get("code") for issue in first_review["issues"]],
            )

    def test_english_explicit_ending_is_detected(self):
        from manju.pipeline.storyboard_agent import _explicit_ending_target

        source = "The final shot holds on Brother's hesitant expression."
        self.assertEqual(
            _explicit_ending_target(source),
            "Brother's hesitant expression",
        )

    def test_tight_face_closeup_rejects_full_body_shadow(self):
        from manju.pipeline.storyboard_agent import (
            _deterministic_quality_issues,
            _prepare_generated_scene,
        )

        bible = json.loads(plan_response())["creative_bible"]
        bible["characters"][0].update({"name": "哥哥", "name_en": "Brother"})
        shot = json.loads(shots_response())["shots"][0]
        shot["visual"].update({
            "shot_type": "close-up",
            "composition": "Brother's face fills the frame, background completely blurred",
            "description": "Brother hesitates while his full-body shadow extends in the opposite direction",
        })
        shot["prompts"].update({
            "image_cn": "哥哥面部特写，背景完全虚化，影子朝反方向延伸",
            "image_en": "Brother face close-up, background completely blurred, full-body shadow extends in the opposite direction",
        })
        scene = _prepare_generated_scene({
            "scene_id": "1",
            "heading": "EXT. ROOFTOP",
            "shots": [shot],
        }, bible)
        storyboard = normalize_storyboard({
            "title": "Spatial check",
            "creative_bible": bible,
            "scenes": [scene],
        })

        codes = [issue["code"] for issue in _deterministic_quality_issues(storyboard)]
        self.assertIn("closeup_shadow_conflict", codes)

    def test_repeated_motif_and_valid_ending_closeup_opinions_are_ignored(self):
        overreach = json.dumps({
            "decision": "revise",
            "summary": "The motif must be repeated in every shot.",
            "issues": [
                {
                    "scene_id": "1",
                    "shot_ids": ["1.1", "1.2", "1.3"],
                    "severity": "high",
                    "category": "source_fidelity",
                    "blocking": True,
                    "problem": "The source motif is not shown in every shot.",
                    "instruction": "Repeat it consistently in all later shots.",
                },
                {
                    "scene_id": "1",
                    "shot_ids": ["1.5"],
                    "severity": "low",
                    "category": "advisory_art_direction",
                    "blocking": False,
                    "problem": "The added close-up is not explicitly required by the source ending.",
                    "instruction": "Consider removing the close-up.",
                },
            ],
            "target_scene_ids": ["1"],
        })
        source = "画面停在哥哥欲言又止的表情上。"
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                plan_response(), shots_response(), overreach,
            ], text=source)

            self.assertIsNotNone(result)
            self.assertEqual(calls, 3)
            with open(os.path.join(root, "review.json"), encoding="utf-8") as handle:
                review = json.load(handle)
            self.assertEqual(review["decision"], "accept")
            self.assertEqual(review["issues"], [])
            self.assertEqual(
                review["summary"],
                "Automated review found no blocking or advisory issues.",
            )

    def test_incomplete_character_plan_is_retried(self):
        incomplete = json.loads(plan_response())
        incomplete["creative_bible"]["characters"][0]["anchor_description"] = "mysterious lead"
        incomplete["creative_bible"]["characters"][0]["anchor_description_en"] = ""
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                json.dumps(incomplete),
                plan_response(),
                shots_response(),
                review_response(),
            ])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 4)
            self.assertTrue(result["creative_bible"]["characters"][0]["anchor_description_en"])

    def test_character_anchor_without_source_age_is_valid(self):
        from manju.pipeline.storyboard_agent import _plan_quality_errors

        plan = json.loads(plan_response())
        errors = _plan_quality_errors(plan, 1)

        self.assertEqual(errors, [])
        character = plan["creative_bible"]["characters"][0]
        self.assertNotRegex(character["anchor_description"], r"\d+\s*岁")
        self.assertNotRegex(character["anchor_description_en"], r"\d+[- ]year[- ]old")

    def test_meaningless_anchor_filler_is_removed_without_retry(self):
        plan = json.loads(plan_response())
        character = plan["creative_bible"]["characters"][0]
        character["anchor_description"] += "，无其他永久标记"
        character["anchor_description_en"] += ", no other permanent marks"
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                json.dumps(plan, ensure_ascii=False),
                shots_response(),
                review_response(),
            ])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 3)
            serialized = json.dumps(result, ensure_ascii=False).lower()
            self.assertNotIn("无其他永久标记", serialized)
            self.assertNotIn("no other permanent marks", serialized)

    def test_named_creator_style_is_replaced_and_anchors_are_injected(self):
        named_plan = json.loads(plan_response())
        named_plan["creative_bible"]["style_anchor"] = "新海诚式细腻光影"
        named_shots = json.loads(shots_response())
        named_shots["shots"][0]["prompts"]["image_cn"] += "，新海诚风格"
        named_shots["shots"][0]["prompts"]["image_en"] += ", Makoto Shinkai style"
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                json.dumps(named_plan, ensure_ascii=False),
                json.dumps(named_shots, ensure_ascii=False),
                review_response(),
            ])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 3)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("新海诚", serialized)
            self.assertNotIn("Makoto Shinkai", serialized)
            character = result["creative_bible"]["characters"][0]
            prompt = result["scenes"][0]["shots"][0]["prompts"]
            self.assertIn(character["anchor_description"], prompt["image_cn"])
            self.assertIn(character["anchor_description_en"], prompt["image_en"])
            self.assertTrue(prompt["video_cn"])
            self.assertTrue(prompt["video_en"])

    def test_anchor_injection_is_idempotent_and_english_only(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        bible["characters"][0]["name"] = "林夏"
        scene = {
            "scene_id": "1",
            "shots": json.loads(shots_response())["shots"],
        }
        scene["shots"][0]["prompts"]["image_en"] += "; fixed character anchor for 林夏: " + (
            bible["characters"][0]["anchor_description_en"]
        )
        once = _prepare_generated_scene(scene, bible)
        twice = _prepare_generated_scene(once, bible)
        prompt = twice["shots"][0]["prompts"]

        self.assertNotRegex(prompt["image_en"], r"[\u4e00-\u9fff]")
        self.assertLessEqual(prompt["image_cn"].count("角色固定锚点"), 1)
        self.assertLessEqual(prompt["image_en"].lower().count("fixed character anchor"), 1)

    def test_semicolon_rich_anchor_is_preserved_as_one_structured_suffix(self):
        from manju.pipeline.storyboard_agent import _deterministic_quality_issues, _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        character = bible["characters"][0]
        character["anchor_description"] = "短黑发；左眉细疤；红色长外套；银色圆扣"
        character["anchor_description_en"] = (
            "short black hair; thin scar over left eyebrow; red long coat; silver round buttons"
        )
        scene = json.loads(plan_response())["scenes"][0]
        scene["shots"] = json.loads(shots_response())["shots"]
        once = _prepare_generated_scene(scene, bible)
        twice = _prepare_generated_scene(once, bible)
        prompts = twice["shots"][0]["prompts"]
        self.assertIn(character["anchor_description"], prompts["image_cn"])
        self.assertIn(character["anchor_description_en"], prompts["image_en"])
        self.assertEqual(prompts["image_cn"].count("角色固定锚点"), 1)
        self.assertEqual(prompts["image_en"].lower().count("fixed character anchor"), 1)
        storyboard = normalize_storyboard({
            "creative_bible": bible, "scenes": [twice],
        })
        self.assertFalse(any(
            issue.get("code") == "missing_character_anchor"
            for issue in _deterministic_quality_issues(storyboard)
        ))

    def test_anchor_cleanup_keeps_only_characters_framed_in_the_still(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        bible["characters"] = [
            {**bible["characters"][0], "name": "林夏"},
            {
                "name": "哥哥",
                "name_en": "Brother",
                "role": "supporting",
                "anchor_description": "28岁男性，黑色短发，方脸，肤色苍白，身材偏瘦，穿深灰夹克",
                "anchor_description_en": "28-year-old man, short black hair, square face, pale skin, slim build, dark gray jacket",
            },
        ]
        lin_cn = bible["characters"][0]["anchor_description"]
        lin_en = bible["characters"][0]["anchor_description_en"]
        brother_cn = bible["characters"][1]["anchor_description"]
        brother_en = bible["characters"][1]["anchor_description_en"]
        first = json.loads(shots_response())["shots"][0]
        first["shot_id"] = "1.1"
        first["visual"].update({
            "composition": "林夏独自站在画面右侧",
            "description": "林夏低头看信，哥哥稍后才会出现",
            "color_tone": "",
        })
        first["prompts"].update({
            "image_cn": f"林夏独自站在天台；角色固定锚点：哥哥，{brother_cn}",
            "image_en": f"Lin Xia stands alone; fixed character anchor for Brother: {brother_en}",
        })
        second = json.loads(shots_response())["shots"][0]
        second["shot_id"] = "1.2"
        second["visual"].update({
            "composition": "哥哥站在铁门边，占据画面中心",
            "description": "哥哥出现并看向画外的林夏",
            "color_tone": "",
        })
        second["prompts"].update({
            "image_cn": f"哥哥站在铁门边；角色固定锚点：林夏，{lin_cn}",
            "image_en": f"Brother stands by the door; fixed character anchor for Lin Xia: {lin_en}",
        })

        prepared = _prepare_generated_scene({
            "scene_id": "1",
            "visual_mood": "低饱和青灰色",
            "shots": [first, second],
        }, bible)

        first, second = prepared["shots"]
        self.assertIn(lin_cn, first["prompts"]["image_cn"])
        self.assertIn(lin_en, first["prompts"]["image_en"])
        self.assertNotIn(brother_cn, first["prompts"]["image_cn"])
        self.assertNotIn(brother_en, first["prompts"]["image_en"])
        self.assertIn(brother_cn, second["prompts"]["image_cn"])
        self.assertIn(brother_en, second["prompts"]["image_en"])
        self.assertNotIn(lin_cn, second["prompts"]["image_cn"])
        self.assertNotIn(lin_en, second["prompts"]["image_en"])
        self.assertEqual(first["visual"]["color_tone"], "低饱和青灰色")
        self.assertEqual(second["visual"]["color_tone"], "低饱和青灰色")

    def test_object_detail_does_not_receive_full_body_anchor(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        detail = json.loads(shots_response())
        shot = detail["shots"][0]
        shot["visual"]["shot_type"] = "特写"
        shot["visual"]["description"] = "林夏的手指紧握信纸边缘，墨迹逐渐消失"
        shot["prompts"]["image_cn"] = "林夏的手指与信纸特写"
        shot["prompts"]["image_en"] = "close-up of Lin Xia's fingers and letter"
        prepared = _prepare_generated_scene({"scene_id": "1", "shots": [shot]}, bible)
        prompt = prepared["shots"][0]["prompts"]

        self.assertNotIn("角色固定锚点", prompt["image_cn"])
        self.assertNotIn("fixed character anchor", prompt["image_en"].lower())

    def test_short_sequential_shot_is_extended_without_revision(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        payload = json.loads(shots_response())
        shot = payload["shots"][0]
        shot["duration_seconds"] = 3
        shot["visual"]["description"] = "铁门被风吹开，林夏回头看向门边，随后向前迈出一步"
        shot["audio"]["dialogue"] = ""
        prepared = _prepare_generated_scene({"scene_id": "1", "shots": [shot]}, bible)

        self.assertEqual(prepared["shots"][0]["duration_seconds"], 4.0)

    def test_four_beat_shot_is_extended_to_five_seconds(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        payload = json.loads(shots_response())
        shot = payload["shots"][0]
        shot["duration_seconds"] = 4
        shot["visual"]["description"] = "铁门被风吹开，哥哥出现，林夏回头看向他"
        prepared = _prepare_generated_scene({"scene_id": "1", "shots": [shot]}, bible)

        self.assertEqual(prepared["shots"][0]["duration_seconds"], 5.0)

    def test_reaction_reveal_and_dialogue_are_counted_as_separate_beats(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        shot = json.loads(shots_response())["shots"][0]
        shot["duration_seconds"] = 3
        shot["visual"]["description"] = (
            "林夏转头，瞳孔微张，看到哥哥站在门边，他的影子朝反方向延伸"
        )
        prepared = _prepare_generated_scene({"scene_id": "1", "shots": [shot]}, bible)

        self.assertEqual(prepared["shots"][0]["duration_seconds"], 6.0)

    def test_step_focus_reaction_and_dialogue_require_five_seconds(self):
        from manju.pipeline.storyboard_agent import _prepare_generated_scene

        bible = json.loads(plan_response())["creative_bible"]
        shot = json.loads(shots_response())["shots"][0]
        shot["duration_seconds"] = 4
        shot["visual"]["description"] = (
            "林夏向前走一步，直视前方，追问，哥哥嘴唇微张却未出声"
        )
        prepared = _prepare_generated_scene({"scene_id": "1", "shots": [shot]}, bible)

        self.assertEqual(prepared["shots"][0]["duration_seconds"], 5.0)

    def test_explicit_source_ending_must_be_final_visual_focus(self):
        plan = json.loads(plan_response())
        plan["creative_bible"]["characters"].append({
            "name": "哥哥",
            "name_en": "Brother",
            "role": "supporting",
            "anchor_description": "28岁男性，黑色短发，方脸，肤色苍白，身材偏瘦，穿深灰夹克",
            "anchor_description_en": "young 28-year-old man, short black hair, square face, pale skin, slim build, wearing a dark gray jacket",
        })
        bad = json.loads(shots_response())
        bad_shot = bad["shots"][0]
        bad_shot["duration_seconds"] = 4
        bad_shot["visual"].update({
            "shot_type": "中近景",
            "composition": "林夏位于画面中央，哥哥在左侧边缘",
            "camera_movement": "缓慢前推，聚焦林夏表情",
            "description": "林夏追问，哥哥嘴唇微张却未出声",
        })
        bad_shot["audio"].update({
            "speaker": "林夏",
            "dialogue": "那我该相信谁？",
        })
        bad_shot["prompts"].update({
            "image_cn": "林夏正面中近景，哥哥位于画面边缘",
            "image_en": "Medium close-up of Lin Xia, Brother at frame edge",
        })
        fixed = json.loads(shots_response())
        fixed_shot = fixed["shots"][0]
        fixed_shot["duration_seconds"] = 3
        fixed_shot["visual"].update({
            "shot_type": "特写",
            "composition": "哥哥面部占满画面，背景完全虚化",
            "camera_movement": "固定镜头",
            "description": "哥哥欲言又止，表情停在痛苦与犹豫之间",
        })
        fixed_shot["audio"].update({"speaker": "", "dialogue": ""})
        fixed_shot["prompts"].update({
            "image_cn": "哥哥面部特写，欲言又止",
            "image_en": "Close-up of Brother's hesitant face",
        })
        source = "林夏追问哥哥。画面停在哥哥欲言又止的表情上。"
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                json.dumps(plan, ensure_ascii=False),
                json.dumps(bad, ensure_ascii=False),
                json.dumps(fixed, ensure_ascii=False),
                review_response(),
            ], text=source)

            self.assertIsNotNone(result)
            self.assertEqual(calls, 4)
            self.assertEqual(result["scenes"][0]["shots"][-1]["visual"]["shot_type"], "特写")
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["revision_count"], 1)
            with open(
                os.path.join(root, "stages", "agent", "latest.json"),
                encoding="utf-8",
            ) as handle:
                latest = json.load(handle)
            with open(
                os.path.join(
                    root, "stages", "agent", latest["run_dir"], "04_review_00.json"
                ),
                encoding="utf-8",
            ) as handle:
                first_review = json.load(handle)
            self.assertEqual(first_review["issues"][0]["category"], "source_fidelity")

    def test_art_direction_advice_does_not_consume_revision_budget(self):
        advice = json.dumps({
            "decision": "revise",
            "summary": "A moving camera may add variety.",
            "issues": [{
                "scene_id": "1",
                "shot_ids": ["1.1"],
                "severity": "high",
                "category": "advisory_art_direction",
                "blocking": True,
                "problem": "The establishing shot is static.",
                "instruction": "Consider a slow push.",
            }],
            "target_scene_ids": ["1"],
        })
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [plan_response(), shots_response(), advice])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 3)
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["revision_count"], 0)
            with open(os.path.join(root, "review.json"), encoding="utf-8") as handle:
                review = json.load(handle)
            self.assertEqual(review["decision"], "accept")
            self.assertEqual(len(review["advisory_issues"]), 1)

    def test_empty_review_has_consistent_summary_and_receives_source(self):
        empty_review = json.dumps({
            "decision": "accept",
            "summary": "Minor advisory suggestions are noted.",
            "issues": [],
            "target_scene_ids": [],
        })
        with tempfile.TemporaryDirectory() as root:
            with patch(
                "manju.pipeline.storyboard_agent.call_llm",
                side_effect=[plan_response(), shots_response(), empty_review],
            ) as mocked:
                result = generate_storyboard_agent(
                    SOURCE,
                    "Mock Story",
                    len(SOURCE),
                    1,
                    os.path.join(root, "stages", "agent"),
                    root,
                )

            self.assertIsNotNone(result)
            self.assertIn(SOURCE, mocked.call_args_list[2].args[1])
            with open(os.path.join(root, "review.json"), encoding="utf-8") as handle:
                review = json.load(handle)
            self.assertEqual(review["issues"], [])
            self.assertEqual(
                review["summary"],
                "Automated review found no blocking or advisory issues.",
            )

    def test_anchor_opinion_is_advisory_and_does_not_trigger_revision(self):
        opinion = json.dumps({
            "decision": "revise",
            "summary": "Add a future character anchor early.",
            "issues": [{
                "scene_id": "1",
                "shot_ids": ["1.1"],
                "severity": "high",
                "category": "source_fidelity",
                "blocking": True,
                "problem": "The future character anchor is missing.",
                "instruction": "Add Brother's anchor before he appears.",
            }],
            "target_scene_ids": ["1"],
        })
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [plan_response(), shots_response(), opinion])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 3)
            with open(os.path.join(root, "review.json"), encoding="utf-8") as handle:
                review = json.load(handle)
            self.assertEqual(review["decision"], "accept")
            self.assertEqual(review["blocking_issues"], [])
            self.assertEqual(
                review["advisory_issues"][0]["category"],
                "advisory_art_direction",
            )

    def test_revision_prompt_contains_blocking_issues_only(self):
        review = json.dumps({
            "decision": "revise",
            "summary": "One blocker and one optional suggestion.",
            "issues": [
                {
                    "scene_id": "1",
                    "shot_ids": ["1.1"],
                    "severity": "high",
                    "category": "continuity",
                    "blocking": True,
                    "problem": "The door action contradicts the source.",
                    "instruction": "Restore the source action.",
                },
                {
                    "scene_id": "1",
                    "shot_ids": ["1.1"],
                    "severity": "low",
                    "category": "advisory_art_direction",
                    "blocking": False,
                    "problem": "A reaction shot might add emotion.",
                    "instruction": "Optionally add a reaction shot.",
                },
            ],
            "target_scene_ids": ["1"],
        })
        with tempfile.TemporaryDirectory() as root:
            with patch(
                "manju.pipeline.storyboard_agent.call_llm",
                side_effect=[
                    plan_response(),
                    shots_response(),
                    review,
                    shots_response("Lin turns toward the source-accurate door"),
                    review_response(),
                ],
            ) as mocked:
                result = generate_storyboard_agent(
                    SOURCE,
                    "Mock Story",
                    len(SOURCE),
                    1,
                    os.path.join(root, "stages", "agent"),
                    root,
                )

            self.assertIsNotNone(result)
            revision_payload = json.loads(mocked.call_args_list[3].args[1])
            self.assertEqual(len(revision_payload["review_issues"]), 1)
            self.assertIn(
                "contradicts the source",
                revision_payload["review_issues"][0]["problem"],
            )
            self.assertNotIn("reaction shot", json.dumps(revision_payload))

    def test_invalid_json_is_repaired_once(self):
        with tempfile.TemporaryDirectory() as root:
            result, calls = self._run(root, [
                "not-json",
                plan_response(),
                shots_response(),
                review_response(),
            ])

            self.assertIsNotNone(result)
            self.assertEqual(calls, 4)
            run_dir = os.path.join(root, "stages", "agent")
            with open(os.path.join(run_dir, "latest.json"), encoding="utf-8") as handle:
                latest = json.load(handle)
            self.assertTrue(os.path.isfile(os.path.join(
                run_dir, latest["run_dir"], "01_plan_raw.txt"
            )))

    def test_failed_scene_node_resumes_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            first, first_calls = self._run(root, [plan_response(), None])
            second, second_calls = self._run(root, [shots_response(), review_response()])

            self.assertIsNone(first)
            self.assertEqual(first_calls, 2)
            self.assertIsNotNone(second)
            self.assertEqual(second_calls, 2)
            self.assertEqual(validate_storyboard(second), [])

    def test_changed_model_creates_a_new_run(self):
        with tempfile.TemporaryDirectory() as root:
            with patch(
                "manju.pipeline.storyboard_agent.get_ai_config",
                return_value=("https://example.invalid/chat/completions", "model-a", "secret"),
            ):
                first, _ = self._run(root, [plan_response(), shots_response(), review_response()])
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                first_manifest = json.load(handle)
            with patch(
                "manju.pipeline.storyboard_agent.get_ai_config",
                return_value=("https://example.invalid/chat/completions", "model-b", "secret"),
            ):
                second, _ = self._run(
                    root,
                    [plan_response(), shots_response(), review_response()],
                )
            with open(os.path.join(root, "agent_run.json"), encoding="utf-8") as handle:
                second_manifest = json.load(handle)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first_manifest["run_id"], second_manifest["run_id"])
            self.assertNotIn("secret", json.dumps(first_manifest))
            self.assertNotIn("secret", json.dumps(second_manifest))
            stage_dir = os.path.join(root, "stages", "agent")
            self.assertEqual(
                len([name for name in os.listdir(stage_dir) if name.startswith("run_")]),
                2,
            )

    def test_cli_workflow_engine_routes_to_frozen_langgraph(self):
        runner = CliRunner()
        generated = normalize_storyboard({
            "title": "Mock Story",
            "creative_bible": {
                "style_anchor": "cinematic anime",
                "characters": [],
            },
            "scenes": json.loads(plan_response())["scenes"],
        })
        generated["scenes"][0]["shots"] = json.loads(shots_response())["shots"]
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "story.txt")
            output = os.path.join(root, "output")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(SOURCE)
            with patch(
                "manju.pipeline.storyboard_agent.generate_storyboard_workflow",
                return_value=generated,
            ) as mocked, patch("manju.pipeline.storyboard.write_xlsx"):
                result = runner.invoke(cli, [
                    "storyboard", source, "--engine", "workflow",
                    "--max-scenes", "1", "-o", output,
                ])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(mocked.call_count, 1)
            with open(os.path.join(output, "storyboard.json"), encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["metadata"]["generation_engine"], "workflow")
            self.assertEqual(saved["metadata"]["source_file"], "story.txt")

    def test_pipeline_passes_workflow_engine_to_storyboard(self):
        runner = CliRunner()
        generated = normalize_storyboard({
            "title": "Mock Story",
            "creative_bible": {
                "style_anchor": "cinematic anime",
                "characters": [],
            },
            "scenes": [{
                **json.loads(plan_response())["scenes"][0],
                "shots": json.loads(shots_response())["shots"],
            }],
        })
        with tempfile.TemporaryDirectory() as root:
            script = os.path.join(root, "script.json")
            with open(script, "w", encoding="utf-8") as handle:
                json.dump({"title": "Mock Story", "scenes": []}, handle)

            def fake_storyboard(source, output_dir=None, **kwargs):
                os.makedirs(output_dir, exist_ok=True)
                with open(os.path.join(output_dir, "storyboard.json"), "w", encoding="utf-8") as handle:
                    json.dump(generated, handle)
                return generated

            with patch("manju.cli.run_storyboard", side_effect=fake_storyboard) as mocked, patch(
                "manju.cli.write_use_guide",
                return_value={"pdf": "guide.pdf", "docx": "guide.docx"},
            ):
                result = runner.invoke(cli, [
                    "pipeline", "--script", script, "-o", root,
                    "--engine", "workflow", "--no-voice", "--no-video",
                ])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(mocked.call_args.kwargs["engine"], "workflow")


if __name__ == "__main__":
    unittest.main()
