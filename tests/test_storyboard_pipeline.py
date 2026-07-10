import json
import builtins
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

from manju.pipeline.storyboard_schema import (
    get_spoken_text,
    get_style_anchor,
    get_visual,
    normalize_storyboard,
    validate_storyboard,
)
from manju.pipeline.storyboard import (
    _generate_images_from_storyboard,
    run_storyboard,
)
from manju.pipeline.storyboard_stages import generate_storyboard_staged
from manju.pipeline.video import _generate_video_prompts, run_video
from manju.pipeline.voice import _generate_voice_markdown, _generate_voice_scripts, run_voice
from manju.utils.formats import HAS_DOCX, HAS_EXCEL, _flatten_for_excel, write_docx, write_pdf
import manju.utils.formats as formats_module
from manju.utils.use_guide import write_use_guide


HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None
HAS_GUIDE_EXPORTS = (
    importlib.util.find_spec("docx") is not None
    and (
        importlib.util.find_spec("weasyprint") is not None
        or HAS_REPORTLAB
    )
)


LEGACY_STORYBOARD = {
    "title": "旧格式",
    "style_anchor": "cinematic anime",
    "characters": [{"name": "阿宁", "anchor_description": "短黑发少女"}],
    "scenes": [{
        "scene_id": 1,
        "scene_heading": "EXT. 天台 - 黄昏",
        "visual_mood": "紧张",
        "scene_template": "夕阳逆光",
        "shots": [{
            "shot_id": "1.1",
            "shot_type": "近景",
            "composition": "三分法",
            "composition_emotion": "被困",
            "camera_movement": "缓慢推近",
            "duration": "3s",
            "visual_description": "阿宁转身看向门口",
            "dialogue_narration": "阿宁：谁在那里？",
            "sound_music": "风声",
            "color_tone": "冷蓝",
            "image_prompt_cn": "中文提示词",
            "image_prompt_en": "English prompt",
            "video_prompt": "hair moving in wind",
        }],
    }],
}


def _plan_response():
    return json.dumps({
        "title": "测试故事",
        "creative_bible": {
            "style_anchor": "cinematic anime",
            "aspect_ratio": "9:16",
            "characters": [{
                "name": "阿宁", "role": "主角", "anchor_description": "短黑发少女"
            }],
        },
        "scenes": [{
            "scene_id": "1", "heading": "EXT. 天台 - 黄昏", "purpose": "发现追兵",
            "visual_mood": "紧张", "scene_template": "夕阳逆光",
            "continuity": {"from_previous": "", "to_next": "逃跑"},
        }],
    }, ensure_ascii=False)


def _shots_response():
    return json.dumps({"shots": [{
        "shot_id": "1.1",
        "duration_seconds": 3,
        "visual": {
            "shot_type": "近景", "composition": "三分法",
            "composition_emotion": "被困", "camera_movement": "缓慢推近",
            "description": "阿宁转身看向门口", "color_tone": "冷蓝",
        },
        "audio": {"speaker": "阿宁", "dialogue": "谁在那里？", "narration": "", "sound_music": "风声"},
        "prompts": {"image_cn": "中文提示词", "image_en": "English prompt", "video": "hair moving"},
    }]}, ensure_ascii=False)


class StoryboardSchemaTests(unittest.TestCase):
    def test_legacy_storyboard_is_normalized_without_data_loss(self):
        result = normalize_storyboard(LEGACY_STORYBOARD)

        self.assertEqual(result["schema_version"], "2.0")
        self.assertEqual(get_style_anchor(result), "cinematic anime")
        shot = result["scenes"][0]["shots"][0]
        self.assertEqual(get_visual(shot, "description"), "阿宁转身看向门口")
        self.assertEqual(get_spoken_text(shot), "阿宁：谁在那里？")
        self.assertEqual(shot["prompts"]["video"], "hair moving in wind")
        self.assertEqual(validate_storyboard(result), [])

    def test_v2_storyboard_feeds_video_and_excel_consumers(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)

        prompts = _generate_video_prompts(storyboard)
        rows = _flatten_for_excel(storyboard)

        self.assertEqual(prompts[0]["shot_type"], "近景")
        self.assertIn("阿宁转身看向门口", prompts[0]["video_prompt_cn"])
        self.assertIn("English prompt", prompts[0]["video_prompt_en"])
        self.assertEqual(prompts[0]["duration"], "3s")
        self.assertEqual(rows[0]["画面描述"], "阿宁转身看向门口")
        self.assertEqual(rows[0]["时长"], "3s")

    def test_formats_module_loads_when_docx_dependency_is_unavailable(self):
        original_import = builtins.__import__

        def import_without_docx(name, *args, **kwargs):
            if name == "docx" or name.startswith("docx."):
                raise ImportError("simulated missing python-docx")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location(
            "formats_without_docx", formats_module.__file__
        )
        isolated_module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", side_effect=import_without_docx):
            spec.loader.exec_module(isolated_module)

        self.assertFalse(isolated_module.HAS_DOCX)
        self.assertIsNone(isolated_module.BODY)


class MultiStageGenerationTests(unittest.TestCase):
    def test_plan_and_scene_calls_are_assembled_and_saved(self):
        with tempfile.TemporaryDirectory() as stage_dir:
            with patch(
                "manju.pipeline.storyboard_stages.call_llm",
                side_effect=[_plan_response(), _shots_response()],
            ) as mocked:
                result = generate_storyboard_staged(
                    "阿宁来到天台。", "测试故事", 8, 1, stage_dir,
                )

            self.assertIsNotNone(result)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(result["schema_version"], "2.0")
            with open(os.path.join(stage_dir, "latest.json"), encoding="utf-8") as handle:
                latest = json.load(handle)
            run_dir = os.path.join(stage_dir, latest["run_dir"])
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "01_plan.json")))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "02_scene_001.json")))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "03_storyboard.json")))

    def test_scene_failure_keeps_plan_artifact(self):
        with tempfile.TemporaryDirectory() as stage_dir:
            with patch(
                "manju.pipeline.storyboard_stages.call_llm",
                side_effect=[_plan_response(), "not json", "still not json"],
            ):
                result = generate_storyboard_staged(
                    "阿宁来到天台。", "测试故事", 8, 1, stage_dir,
                )

            self.assertIsNone(result)
            run_dir = next(os.path.join(stage_dir, name) for name in os.listdir(stage_dir)
                           if name.startswith("run_"))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "01_plan.json")))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "02_scene_001_raw.txt")))

    def test_wrong_plan_scene_count_fails_before_shot_generation(self):
        with tempfile.TemporaryDirectory() as stage_dir:
            with patch(
                "manju.pipeline.storyboard_stages.call_llm",
                return_value=_plan_response(),
            ) as mocked:
                result = generate_storyboard_staged(
                    "阿宁来到天台。", "测试故事", 8, 2, stage_dir,
                )

            self.assertIsNone(result)
            self.assertEqual(mocked.call_count, 2)


class StoryboardOutputTests(unittest.TestCase):
    def test_run_storyboard_writes_v2_outputs_and_uses_json_title(self):
        generated = normalize_storyboard({
            "title": "模型标题",
            "style_anchor": "cinematic anime",
            "scenes": LEGACY_STORYBOARD["scenes"],
        })
        with tempfile.TemporaryDirectory() as output_dir:
            source_path = os.path.join(output_dir, "input_script.json")
            with open(source_path, "w", encoding="utf-8") as handle:
                json.dump({"title": "输入标题", "scenes": []}, handle, ensure_ascii=False)

            with patch(
                "manju.pipeline.storyboard.generate_storyboard_staged",
                return_value=generated,
            ) as mocked:
                result = run_storyboard(source_path, output_dir=output_dir, max_scenes=1)

            self.assertEqual(mocked.call_args.args[1], "输入标题")
            self.assertEqual(result["schema_version"], "2.0")
            self.assertEqual(result["metadata"]["target_scene_count"], 1)
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "storyboard.json")))
            self.assertEqual(
                os.path.isfile(os.path.join(output_dir, "storyboard.xlsx")),
                HAS_EXCEL,
            )
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "storyboard.md")))

    @unittest.skipUnless(HAS_DOCX, "python-docx not installed")
    def test_v2_storyboard_exports_to_word(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        with tempfile.TemporaryDirectory() as output_dir:
            path = os.path.join(output_dir, "storyboard.docx")
            write_docx(storyboard, path, storyboard["title"])
            self.assertGreater(os.path.getsize(path), 1000)

    @unittest.skipUnless(HAS_GUIDE_EXPORTS, "PDF/Word export dependencies not installed")
    def test_use_guide_exports_real_pdf_and_docx(self):
        with tempfile.TemporaryDirectory() as output_dir:
            generated = write_use_guide(
                output_dir,
                {
                    "storyboard_xlsx": "storyboard.xlsx",
                    "voice_pdf": "voice_prompts.pdf",
                    "video_pdf": "video_prompts.pdf",
                },
            )

            self.assertEqual(set(generated), {"pdf", "docx"})
            for path in generated.values():
                self.assertTrue(os.path.isfile(path))
                self.assertGreater(os.path.getsize(path), 1000)

    @unittest.skipUnless(HAS_REPORTLAB, "reportlab not installed")
    def test_reportlab_pdf_works_without_weasyprint_runtime(self):
        original_import = builtins.__import__

        def import_without_weasyprint(name, *args, **kwargs):
            if name == "weasyprint" or name.startswith("weasyprint."):
                raise OSError("simulated missing GTK/Pango runtime")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as output_dir:
            path = os.path.join(output_dir, "voice.pdf")
            with patch("builtins.__import__", side_effect=import_without_weasyprint):
                write_pdf({
                    "title": "测试",
                    "lines": [{
                        "shot_id": "1.1",
                        "character": "阿宁",
                        "text": "谁在那里？",
                        "emotion": "紧张",
                        "speed": 1.0,
                        "pitch": 5,
                        "volume": 5,
                    }],
                }, path)

            self.assertGreater(os.path.getsize(path), 1000)

    def test_generated_image_path_is_attached_to_v2_asset(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        with tempfile.TemporaryDirectory() as output_dir:
            def fake_batch(prompts, target_dir):
                images_dir = os.path.join(target_dir, "images")
                os.makedirs(images_dir, exist_ok=True)
                for item in prompts:
                    with open(os.path.join(images_dir, item["output_filename"]), "wb") as handle:
                        handle.write(b"png")
                return len(prompts)

            with patch("manju.pipeline.storyboard.run_batch_images", side_effect=fake_batch):
                count = _generate_images_from_storyboard(storyboard, output_dir)

            shot = storyboard["scenes"][0]["shots"][0]
            self.assertEqual(count, 1)
            self.assertRegex(shot["assets"]["image"], r"images[\\/]shot_1\.1_[0-9a-f]{8}\.png")
            self.assertEqual(shot["status"]["image"], "completed")

    def test_v2_file_runs_through_voice_and_video_outputs(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        with tempfile.TemporaryDirectory() as output_dir:
            source_path = os.path.join(output_dir, "storyboard.json")
            with open(source_path, "w", encoding="utf-8") as handle:
                json.dump(storyboard, handle, ensure_ascii=False)
            with patch("manju.pipeline.voice.call_llm", return_value=None), patch(
                "manju.utils.formats.write_pdf"
            ):
                voice_result = run_voice(source_path, output_dir=output_dir)
                video_result = run_video(source_path, output_dir=output_dir)

            self.assertEqual(voice_result[0]["character"], "阿宁")
            self.assertEqual(video_result[0]["shot_id"], "1.1")
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "voice_scripts.json")))
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "video_prompts.json")))


class VoiceRegressionTests(unittest.TestCase):
    def test_silent_shot_markdown_does_not_compare_placeholders_as_numbers(self):
        markdown = _generate_voice_markdown([{
            "shot_id": "1.1", "scene_id": "1", "character": "—",
            "text": "（无对白）", "emotion": "—", "emotion_label": "—",
            "speed": "—", "pitch": "—", "volume": "—",
            "voice_description": "纯画面镜头，无配音",
        }], "无声测试")

        self.assertIn("**语速** | —", markdown)

    def test_run_voice_accepts_v2_silent_shot(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        shot = storyboard["scenes"][0]["shots"][0]
        shot["audio"] = {"speaker": "", "dialogue": "", "narration": "", "sound_music": "风声"}
        with tempfile.TemporaryDirectory() as output_dir:
            path = os.path.join(output_dir, "storyboard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(storyboard, handle, ensure_ascii=False)
            with patch("manju.utils.formats.write_pdf"):
                result = run_voice(path, output_dir=output_dir)

            self.assertEqual(result[0]["text"], "（无对白）")
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "voice_scripts.md")))

    def test_explicit_v2_speaker_preserves_colon_inside_dialogue(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        storyboard["scenes"][0]["shots"][0]["audio"] = {
            "speaker": "阿宁",
            "dialogue": "记住：不要回头。",
            "narration": "",
            "sound_music": "",
        }
        with patch("manju.pipeline.voice._batch_infer_emotions", return_value={1: "平静"}):
            result = _generate_voice_scripts(storyboard)

        self.assertEqual(result[0]["text"], "记住：不要回头。")


if __name__ == "__main__":
    unittest.main()
