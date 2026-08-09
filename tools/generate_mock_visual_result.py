"""Generate the distributable visual-Agent mock without calling external APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from manju.pipeline.storyboard_schema import normalize_storyboard  # noqa: E402
from manju.pipeline.visual_agent import run_image_agent  # noqa: E402


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def _storyboard() -> dict:
    key_prop = {
        "prop_id": "prop_letter",
        "name": "旧信",
        "description": "无可读文字的泛黄折叠信纸",
        "continuity_required": True,
    }
    return normalize_storyboard({
        "title": "通用视觉 Agent Mock",
        "metadata": {
            "generation_engine": "agent", "agent_status": "completed",
            "agent_verification_state": "verified",
        },
        "creative_bible": {
            "style_anchor": "cinematic graphic novel, grounded lighting",
            "aspect_ratio": "9:16",
            "characters": [
                {"character_id": "c1", "name": "阿宁", "role": "主角",
                 "anchor_description": "短黑发，深色外套"},
                {"character_id": "c2", "name": "阿哲", "role": "同伴",
                 "anchor_description": "短发，灰色夹克"},
            ],
        },
        "scenes": [{
            "scene_id": "1", "heading": "INT. 旧房间 - 夜",
            "purpose": "发现旧信里的线索", "visual_mood": "紧张",
            "continuity": {}, "key_props": [key_prop],
            "shots": [
                {
                    "shot_id": "1.1", "duration_seconds": 3,
                    "visual": {
                        "shot_type": "近景", "composition": "中置",
                        "composition_emotion": "紧张", "camera_movement": "固定",
                        "description": "阿宁独自拿起旧信，阿哲仍在画外", "color_tone": "冷色",
                        "visible_character_ids": ["c1"],
                        "key_props": [key_prop],
                    },
                    "visible_prop_ids": ["prop_letter"],
                    "audio": {},
                    "prompts": {
                        "image_cn": "阿宁独自拿起无可读文字的旧信",
                        "image_en": "Ani alone picks up an old letter with no readable text",
                        "video": "",
                    },
                },
                {
                    "shot_id": "1.2", "duration_seconds": 3,
                    "visual": {
                        "shot_type": "中景", "composition": "对角线",
                        "composition_emotion": "警觉", "camera_movement": "固定",
                        "description": "阿哲走进画面，和阿宁一起查看旧信", "color_tone": "冷色",
                        "visible_character_ids": ["c1", "c2"],
                        "key_props": [key_prop],
                    },
                    "visible_prop_ids": ["prop_letter"],
                    "audio": {},
                    "prompts": {
                        "image_cn": "阿宁与阿哲一起查看无可读文字的旧信",
                        "image_en": "Ani and Azhe inspect an old letter with no readable text",
                        "video": "",
                    },
                },
            ],
        }],
    })


class MockImageProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, prompt: str, output_path: str,
                 references: list[str], size: str) -> str:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.02)
            self.calls.append({
                "path": output_path, "references": list(references), "size": size,
            })
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            Image.new("RGB", (96, 128), (35, 70, 125)).save(output_path)
            return output_path
        finally:
            with self.lock:
                self.active -= 1


def _vision_unavailable(_task: str, _paths: list[str], _context: dict) -> None:
    return None


def _approve(output_dir: Path, manifest: dict) -> None:
    pending = manifest["pending_approval"]
    request_path = output_dir / pending["request_path"]
    decision_path = output_dir / pending["decision_path"]
    with request_path.open(encoding="utf-8") as handle:
        request = json.load(handle)
    with decision_path.open(encoding="utf-8") as handle:
        decision = json.load(handle)
    decision["decision"] = "approve"
    decision["reviewer"] = "Offline Mock Harness"
    decision["reviewed_item_ids"] = request["item_ids"]
    decision["reviewed_image_fingerprints"] = request.get("reviewed_image_fingerprints", {})
    if request["stage"].startswith("foundation_lock_"):
        decision["change_note"] = (
            "Offline Mock selected deterministic placeholders to validate locking and recovery."
        )
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
            if isinstance(contract, dict)
            and contract.get("role") == "canonical_geometry_anchor"
        }
    if request["stage"].startswith("manual_review_"):
        decision["override_reason"] = (
            "Mock 运行无视觉 API；仅人工确认流程与文件结构，不代表画质验收。"
        )
    _write_json(decision_path, decision)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "mock_visual_result_v3_5",
        help="New output directory; an existing directory is never overwritten.",
    )
    args = parser.parse_args()
    output_dir = args.output.resolve()
    if output_dir.exists():
        raise SystemExit(f"refusing to overwrite existing directory: {output_dir}")
    output_dir.mkdir(parents=True)
    storyboard_path = output_dir / "storyboard.json"
    _write_json(storyboard_path, _storyboard())

    image_provider = MockImageProvider()
    for _ in range(30):
        manifest = run_image_agent(
            str(storyboard_path), str(output_dir), execute_paid_calls=True, resume=True,
            foundation_candidates=3, image_parallelism=4,
            image_provider=image_provider,
            vision_provider=_vision_unavailable, size=None,
        )
        if manifest["status"] == "completed":
            break
        if manifest["status"] != "awaiting_approval":
            raise RuntimeError(json.dumps(manifest, ensure_ascii=False, indent=2))
        _approve(output_dir, manifest)
    else:
        raise RuntimeError("mock visual Agent did not complete within 30 approval cycles")

    summary = {
        "status": manifest["status"], "stop_reason": manifest["stop_reason"],
        "run_id": manifest["run_id"], "counters": manifest["counters"],
        "quality_gate": manifest.get("quality_gate", {}),
        "parallel_peak": image_provider.peak,
        "image_calls_observed": len(image_provider.calls),
        "external_api_calls": 0,
    }
    _write_json(output_dir / "mock_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
