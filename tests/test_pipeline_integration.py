import json
import os
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from manju.cli import cli
from manju.pipeline.adapt import run_adapt
from manju.pipeline.create import run_create
from manju.pipeline.storyboard_schema import normalize_storyboard

from test_storyboard_pipeline import LEGACY_STORYBOARD


SCRIPT_RESPONSE = json.dumps({
    "title": "重生：归来",
    "genre": "现代",
    "logline": "测试",
    "characters": [{"name": "阿宁", "role": "主角", "visual_anchor": "短发"}],
    "scenes": [{"scene_id": 1, "location": "天台", "time": "夜",
                "mood": "紧张", "summary": "相遇", "dialogues": [], "action_notes": ""}],
}, ensure_ascii=False)


class CreationAndAdaptationTests(unittest.TestCase):
    def test_create_returns_actual_sanitized_output_path(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.create.call_llm", return_value=SCRIPT_RESPONSE):
            result = run_create({"title": "重生:归来", "premise": "测试"},
                                output_dir=directory, interactive=False)
        self.assertIn("重生_归来_script.json", result["_output_path"])

    def test_long_adaptation_processes_all_chunks_including_ending(self):
        text = "开端。" + "中" * 50_000 + "结局。"
        part_one = json.dumps({
            "genre": "现代", "characters": [{"name": "甲"}],
            "scenes": [{"scene_id": 1, "summary": "前半"}],
        }, ensure_ascii=False)
        part_two = json.dumps({
            "genre": "现代", "characters": [{"name": "乙"}],
            "scenes": [{"scene_id": 1, "summary": "结局"}],
        }, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(text)
            with patch("manju.pipeline.adapt.call_llm",
                       side_effect=[part_one, part_two]) as mocked:
                result = run_adapt(source, output_dir=directory)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(result["scenes"]), 2)
        self.assertEqual(result["scenes"][-1]["summary"], "结局")


class PipelineAssetTests(unittest.TestCase):
    def test_pipeline_renders_and_writes_voice_video_assets(self):
        runner = CliRunner()
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        with tempfile.TemporaryDirectory() as directory:
            storyboard_path = os.path.join(directory, "storyboard.json")
            with open(storyboard_path, "w", encoding="utf-8") as handle:
                json.dump(storyboard, handle, ensure_ascii=False)
            audio_path = os.path.join(directory, "audio", "shot_1.1.mp3")
            video_path = os.path.join(directory, "videos", "shot_1.1.mp4")
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)
            os.makedirs(os.path.dirname(video_path), exist_ok=True)
            with open(audio_path, "wb") as handle:
                handle.write(b"audio")
            with open(video_path, "wb") as handle:
                handle.write(b"video")

            voice_lines = [{
                "shot_id": "1.1", "text": "谁在那里？", "character": "阿宁",
                "speed": 1.0, "pitch": 5, "volume": 5,
            }]
            video_prompts = [{
                "shot_id": "1.1", "video_prompt_en": "move",
                "video_prompt_cn": "移动",
            }]

            def fake_video(source, output_dir=None, strict_exports=False):
                with open(os.path.join(output_dir, "video_prompts.json"), "w", encoding="utf-8") as handle:
                    json.dump({"shots": video_prompts}, handle)
                return video_prompts

            with patch("manju.cli.run_voice", return_value=voice_lines), \
                 patch("manju.cli.run_batch_speak", return_value={"1.1": audio_path}), \
                 patch("manju.cli.run_video", side_effect=fake_video), \
                 patch("manju.cli.run_generate", return_value=video_path), \
                 patch("manju.utils.formats.write_xlsx"), \
                 patch("manju.cli.write_use_guide", return_value={
                     "pdf": os.path.join(directory, "guide.pdf"),
                     "docx": os.path.join(directory, "guide.docx"),
                 }):
                result = runner.invoke(cli, [
                    "pipeline", "--storyboard-json", storyboard_path,
                    "--speak", "--render-videos", "-o", directory,
                ])

            self.assertEqual(result.exit_code, 0, result.output)
            with open(storyboard_path, encoding="utf-8") as handle:
                state = json.load(handle)
            shot = state["scenes"][0]["shots"][0]
            self.assertEqual(shot["status"]["voice"], "completed")
            self.assertEqual(shot["status"]["video"], "completed")
            self.assertTrue(shot["assets"]["voice"])
            self.assertTrue(shot["assets"]["video"])

    def test_pipeline_rejects_dependent_flags(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["pipeline", "--speak", "--no-voice"])
        self.assertNotEqual(result.exit_code, 0)
        result = runner.invoke(cli, ["pipeline", "--render-videos", "--no-video"])
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
