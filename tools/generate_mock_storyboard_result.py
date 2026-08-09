"""Generate a completed storyboard-supervisor artifact without external APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manju.pipeline.storyboard_supervisor as supervisor  # noqa: E402
from manju.pipeline.storyboard_schema import normalize_storyboard  # noqa: E402
from manju.utils.runtime import atomic_write_json  # noqa: E402


SOURCE = "林夏在雨后的天台握着旧信，抬头看向哥哥。"


def _action(snapshot: dict) -> dict:
    if not snapshot.get("source_analyzed"):
        name = "analyze_source"
    elif not snapshot.get("plan_exists"):
        name = "create_plan"
    elif set(snapshot.get("planned_scene_ids", [])) - set(snapshot.get("completed_scene_ids", [])):
        name = "generate_scenes"
    elif not snapshot.get("storyboard_assembled"):
        name = "assemble_storyboard"
    elif snapshot.get("schema_issues") is None:
        name = "validate_schema"
    elif snapshot.get("source_issues") is None:
        name = "compare_source"
    elif snapshot.get("shootability_issues") is None:
        name = "inspect_shootability"
    elif snapshot.get("review_blocking_issues") is None:
        name = "review_storyboard"
    else:
        name = "finalize"
    return {"action": name, "args": {}, "decision_summary": "offline deterministic mock"}


def _mock_llm(system: str, user: str, **_kwargs) -> str:
    if "Current state:\n" in user:
        snapshot = json.loads(user.split("Current state:\n", 1)[1])
        return json.dumps(_action(snapshot), ensure_ascii=False)
    if system.startswith("SOURCE_MODEL_EXTRACTION_V2"):
        return json.dumps({
            "summary": SOURCE,
            "entities": [
                {"name": "林夏", "kind": "character", "source_quote": "林夏"},
                {"name": "哥哥", "kind": "character", "source_quote": "哥哥"},
                {"name": "旧信", "kind": "prop", "source_quote": "旧信",
                 "required_visual_consistency": True},
            ],
            "beats": [{"source_quote": SOURCE, "must_preserve_facts": [SOURCE]}],
        }, ensure_ascii=False)
    if system.startswith("你是漫剧总导演和美术指导"):
        return json.dumps({
            "title": "离线分镜 Mock",
            "creative_bible": {
                "style_anchor": "cinematic graphic novel, grounded lighting",
                "aspect_ratio": "9:16",
                "characters": [
                    {"character_id": "c1", "name": "林夏", "name_en": "Lin Xia",
                     "role": "主角",
                     "anchor_description": "女性，短黑发，鹅蛋脸，深色眼睛，中等身材，穿红色风衣",
                     "anchor_description_en": "woman, short black hair, oval face, dark eyes, medium build, wearing a red trench coat"},
                    {"character_id": "c2", "name": "哥哥", "name_en": "Older Brother",
                     "role": "哥哥",
                     "anchor_description": "男性，短黑发，方脸，深色眼睛，高瘦身材，穿深色外套",
                     "anchor_description_en": "man, short black hair, square face, dark eyes, tall slim build, wearing a dark coat"},
                ],
            },
            "scenes": [{
                "scene_id": "1", "heading": "EXT. 天台 - 雨后",
                "purpose": "兄妹重逢", "visual_mood": "克制",
                "source_chunk_ids": [1], "source_beat_ids": ["beat_0001"],
                "continuity": {},
            }],
        }, ensure_ascii=False)
    if system.startswith("你是影视分镜导演与AI提示词工程师"):
        return json.dumps({"shots": [{
            "shot_id": "1.1", "duration_seconds": 4,
            "source_beat_ids": ["beat_0001"],
            "visual": {
                "shot_type": "中景", "composition": "双人纵深",
                "composition_emotion": "克制", "camera_movement": "固定机位",
                "description": SOURCE, "color_tone": "雨后冷色",
                "visible_character_ids": ["c1", "c2"],
            },
            "audio": {},
            "prompts": {
                "image_cn": SOURCE, "image_en": "Two siblings reunite on a wet rooftop",
                "video": "雨后微风吹动衣角", "video_cn": "雨后微风吹动衣角",
                "video_en": "A light breeze moves their coats after the rain",
            },
        }]}, ensure_ascii=False)
    return json.dumps({"summary": "No objective blockers.", "issues": []}, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "mock_result_v3_5")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing directory: {output}")
    output.mkdir(parents=True)
    stage_dir = output / "stages" / "agent"
    original = supervisor.call_llm
    supervisor.call_llm = _mock_llm
    try:
        storyboard = supervisor.generate_storyboard_agent(
            SOURCE, "离线分镜 Mock", len(SOURCE), 1,
            str(stage_dir), str(output), resume=False,
        )
    finally:
        supervisor.call_llm = original
    manifest = json.loads((output / "agent_run.json").read_text(encoding="utf-8"))
    if not isinstance(storyboard, dict) or manifest.get("status") != "completed":
        raise RuntimeError(json.dumps(manifest, ensure_ascii=False, indent=2))
    storyboard = normalize_storyboard(storyboard, metadata={
        "generation_engine": "agent", "agent_status": "completed",
        "agent_verification_state": "verified", "external_api_calls": 0,
    })
    atomic_write_json(output / "storyboard.json", storyboard)
    atomic_write_json(output / "mock_summary.json", {
        "status": "completed", "external_api_calls": 0,
        "tool_steps": manifest.get("tool_steps"),
        "model_calls": manifest.get("model_calls"),
        "supervisor_agent_version": manifest.get("supervisor_agent_version"),
    })
    print(json.dumps(json.loads((output / "mock_summary.json").read_text(encoding="utf-8")),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
