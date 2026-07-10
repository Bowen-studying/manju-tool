import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from manju.cli import cli
from manju.pipeline import generate_image, generate_video, generate_voice
from manju.pipeline.storyboard_stages import generate_storyboard_staged
from manju.pipeline.voice import _generate_voice_scripts
from manju.utils import ai
from manju.utils.formats import _build_pdf_html
from manju.utils.runtime import join_api_url, safe_filename

from test_storyboard_pipeline import LEGACY_STORYBOARD, _plan_response, _shots_response
from manju.pipeline.storyboard_schema import normalize_storyboard


class FakeResponse:
    def __init__(self, payload, binary=False):
        self.payload = payload if binary else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class RuntimeTests(unittest.TestCase):
    def test_endpoint_joining_and_filename_safety(self):
        self.assertEqual(join_api_url("https://host/v1", "chat/completions"),
                         "https://host/v1/chat/completions")
        self.assertEqual(join_api_url("https://host/v1/chat/completions", "chat/completions"),
                         "https://host/v1/chat/completions")
        self.assertNotIn(":", safe_filename("重生：归来/第一章"))
        self.assertEqual(safe_filename("CON"), "_CON")

    def test_generic_llm_base_is_normalized_and_missing_config_not_cached(self):
        ai.reset_ai_config()
        with patch.dict(os.environ, {}, clear=True), patch("manju.utils.config.os.path.expanduser", return_value="Z:/missing"):
            self.assertEqual(ai.get_ai_config(), (None, None, None))
        ai.reset_ai_config()
        with patch.dict(os.environ, {
            "LLM_API_KEY": "key", "LLM_API_BASE": "https://host/v1", "LLM_MODEL": "model"
        }, clear=True):
            url, model, key = ai.get_ai_config()
        self.assertEqual(url, "https://host/v1/chat/completions")
        self.assertEqual((model, key), ("model", "key"))
        ai.reset_ai_config()

    def test_llm_http_error_retries_and_extracts_content(self):
        error = urllib.error.HTTPError("https://host", 500, "bad", {}, io.BytesIO(b"temporary"))
        with patch("manju.utils.ai.get_ai_config", return_value=("https://host", "m", "k")), \
             patch("manju.utils.ai.time.sleep"), \
             patch("manju.utils.ai.urllib.request.urlopen", side_effect=[
                 error, FakeResponse({"choices": [{"message": {"content": "ok"}}]})
             ]):
            self.assertEqual(ai.call_llm("s", "u", retries=1), "ok")


class ImageApiTests(unittest.TestCase):
    def test_b64_json_is_downloaded(self):
        encoded = base64.b64encode(b"fake-png").decode()
        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.generate_image._get_image_config", return_value={
                 "api_base": "https://host/v1", "api_key": "k", "model": "m"
             }), patch("manju.pipeline.generate_image.urllib.request.urlopen",
                       return_value=FakeResponse({"data": [{"b64_json": encoded}]})):
            path = generate_image.run_image("prompt", output_dir=directory)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"fake-png")

    def test_local_reference_uses_multipart_edits_endpoint(self):
        encoded = base64.b64encode(b"edited").decode()
        captured = []

        def fake_open(request, timeout=0):
            captured.append(request)
            return FakeResponse({"data": [{"b64_json": encoded}]})

        with tempfile.TemporaryDirectory() as directory:
            reference = os.path.join(directory, "ref.png")
            with open(reference, "wb") as handle:
                handle.write(b"reference")
            with patch("manju.pipeline.generate_image._get_image_config", return_value={
                "api_base": "https://host/v1", "api_key": "k", "model": "m"
            }), patch("manju.pipeline.generate_image.urllib.request.urlopen", side_effect=fake_open):
                path = generate_image.run_image("edit", image_path=reference, output_dir=directory)
            self.assertTrue(path and os.path.isfile(path))
            self.assertTrue(captured[0].full_url.endswith("/v1/images/edits"))
            self.assertIn("multipart/form-data", captured[0].headers["Content-type"])

    def test_content_change_invalidates_image_cache(self):
        calls = []

        def fake_generate(*args, **kwargs):
            calls.append(args[0])
            return "data:image/png;base64," + base64.b64encode(b"image-data" * 80).decode()

        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.generate_image._get_image_config", return_value={
                 "api_base": "https://host/v1", "api_key": "k", "model": "m"
             }), patch("manju.pipeline.generate_image._generate_txt2img", side_effect=fake_generate):
            generate_image.run_image("one", output_dir=directory, output_name="same")
            generate_image.run_image("one", output_dir=directory, output_name="same")
            generate_image.run_image("two", output_dir=directory, output_name="same")
        self.assertEqual(calls, ["one", "two"])


class VideoAndVoiceApiTests(unittest.TestCase):
    def test_video_accepts_synchronous_nested_url(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.generate_video._get_video_config", return_value={
                 "api_base": "https://host/v1", "api_key": "k", "model": "m",
                 "poll_base": "", "max_wait": 5,
             }), patch("manju.pipeline.generate_video._create_video",
                       return_value={"data": {"url": "https://download/video"}}), \
             patch("manju.pipeline.generate_video._download_video", side_effect=self._write_video):
            path = generate_video.run_generate("prompt", output_dir=directory)
            self.assertTrue(path and os.path.isfile(path))

    def test_video_recovery_has_real_query_url(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.generate_video._get_video_config", return_value={
                 "api_base": "https://host/v1", "api_key": "k", "model": "m",
                 "poll_base": "", "max_wait": 1,
             }), patch("manju.pipeline.generate_video._create_video", return_value={"id": "task1"}), \
             patch("manju.pipeline.generate_video._poll_video", return_value=None):
            self.assertIsNone(generate_video.run_generate("prompt", output_dir=directory))
            recovery = next(name for name in os.listdir(directory) if name.startswith("video_recovery_"))
            with open(os.path.join(directory, recovery), encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["query_url"], "https://host/v1/videos/task1")

    def test_voice_endpoint_does_not_duplicate_v1(self):
        requests = []

        def fake_open(request, timeout=0):
            requests.append(request)
            return FakeResponse(b"x" * 600, binary=True)

        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.generate_voice.urllib.request.urlopen", side_effect=fake_open):
            ok = generate_voice._speak_api(
                "hello", os.path.join(directory, "a.mp3"),
                cfg={"api_base": "https://host/v1", "api_key": "k", "model": "tts"})
        self.assertTrue(ok)
        self.assertEqual(requests[0].full_url, "https://host/v1/audio/speech")

    @staticmethod
    def _write_video(url, path):
        with open(path, "wb") as handle:
            handle.write(b"video")
        return True

    def test_characters_receive_distinct_stable_voices(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        first = storyboard["scenes"][0]["shots"][0]
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["shot_id"] = "1.2"
        second["audio"]["speaker"] = "阿杰"
        second["audio"]["dialogue"] = "快走"
        storyboard["scenes"][0]["shots"].append(second)
        with patch("manju.pipeline.voice.call_llm", return_value=None):
            lines = _generate_voice_scripts(storyboard)
        self.assertNotEqual(lines[0]["voice_edge"], lines[1]["voice_edge"])
        self.assertNotEqual(lines[0]["voice_api"], lines[1]["voice_api"])

    def test_emotion_classification_chunks_and_fills_partial_results(self):
        storyboard = normalize_storyboard(LEGACY_STORYBOARD)
        template = storyboard["scenes"][0]["shots"][0]
        storyboard["scenes"][0]["shots"] = []
        for index in range(45):
            shot = json.loads(json.dumps(template, ensure_ascii=False))
            shot["shot_id"] = f"1.{index + 1}"
            shot["audio"]["dialogue"] = "快走！"
            storyboard["scenes"][0]["shots"].append(shot)
        with patch("manju.pipeline.voice.call_llm", side_effect=["[1] 焦急", None]) as mocked:
            lines = _generate_voice_scripts(storyboard)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(lines), 45)
        self.assertTrue(all(line["emotion"] == "焦急" for line in lines))


class ResumeAndCliTests(unittest.TestCase):
    def test_storyboard_second_run_uses_stage_cache(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.storyboard_stages.call_llm",
                   side_effect=[_plan_response(), _shots_response()]) as mocked:
            first = generate_storyboard_staged("故事", "标题", 2, 1, directory)
            second = generate_storyboard_staged("故事", "标题", 2, 1, directory)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(mocked.call_count, 2)

    def test_long_storyboard_uses_chunk_summaries(self):
        plan = json.loads(_plan_response())
        plan["scenes"][0]["source_chunk_ids"] = [2]
        responses = [
            json.dumps({"summary": "开端"}, ensure_ascii=False),
            json.dumps({"summary": "结局"}, ensure_ascii=False),
            json.dumps(plan, ensure_ascii=False),
            _shots_response(),
        ]
        with tempfile.TemporaryDirectory() as directory, \
             patch("manju.pipeline.storyboard_stages.call_llm", side_effect=responses) as mocked:
            result = generate_storyboard_staged("甲" * 40_001 + "结局", "标题", 40_003, 1, directory)
            run_dir = next(os.path.join(directory, name) for name in os.listdir(directory)
                           if name.startswith("run_"))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "00_summary_002.json")))
        self.assertIsNotNone(result)
        self.assertEqual(mocked.call_count, 4)

    def test_single_generation_failures_return_nonzero(self):
        runner = CliRunner()
        with patch("manju.cli.run_image", return_value=None):
            result = runner.invoke(cli, ["image", "prompt"])
        self.assertNotEqual(result.exit_code, 0)
        with patch("manju.cli.run_speak", return_value=None):
            result = runner.invoke(cli, ["speak", "text"])
        self.assertNotEqual(result.exit_code, 0)
        with patch("manju.cli.run_generate", return_value=None):
            result = runner.invoke(cli, ["generate", "prompt"])
        self.assertNotEqual(result.exit_code, 0)

    def test_pdf_html_escapes_model_content(self):
        html = _build_pdf_html({"lines": [{
            "shot_id": "1.1", "character": "<img src='x'>", "text": "a&b",
            "emotion": "", "speed": 1.0, "pitch": 5, "volume": 5,
        }]}, "<title>")
        self.assertNotIn("<img src='x'>", html)
        self.assertIn("a&amp;b", html)
        self.assertIn("&lt;title&gt;", html)


if __name__ == "__main__":
    unittest.main()
