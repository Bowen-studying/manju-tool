#!/usr/bin/env python3
"""M7 blind review material generator.

Runs BOTH the frozen workflow engine and the Agent engine on the same story
inputs, anonymises the storyboards (no engine name / path / timestamp) and
emits reviewer-facing A/B documents plus a private seed->engine mapping.

Environment:
  LLM_API_BASE / LLM_API_KEY / LLM_MODEL (OpenAI-compatible endpoint)
  HTTPS_PROXY for the endpoint (optional)

Usage:
  python m7_blind_review_generate.py [--review-dir PATH] [--only s01 agent]
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from manju.pipeline.storyboard import run_storyboard

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_DIR = os.path.abspath(os.environ.get(
    "MANJU_M7_REVIEW_DIR", os.path.join(REPO_DIR, "m7_blind_review_output")
))
RAW_DIR = os.path.join(REVIEW_DIR, "raw")
ANON_DIR = os.path.join(REVIEW_DIR, "匿名评审版")
MAPPING_FILE = os.path.join(REVIEW_DIR, "映射-保密-请勿随评审分发.json")
SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m7_samples")

# 20 story inputs: 15 from the fixed M7 sample set + 5 new inputs written below.
INPUTS = [
    ("s01", os.path.join(SAMPLES_DIR, "s01_short_dialogue.txt")),
    ("s02", os.path.join(SAMPLES_DIR, "s02_monologue.txt")),
    ("s03", os.path.join(SAMPLES_DIR, "s03_silent_action.txt")),
    ("s04", os.path.join(SAMPLES_DIR, "s04_special_chars.txt")),
    ("s05", os.path.join(SAMPLES_DIR, "s05_single_shot.txt")),
    ("n01", os.path.join(SAMPLES_DIR, "n01_short_novel.txt")),
    ("n02", os.path.join(SAMPLES_DIR, "n02_long_novel.txt")),
    ("n03", os.path.join(SAMPLES_DIR, "n03_dialogue_novel.txt")),
    ("n04", os.path.join(SAMPLES_DIR, "n04_scenery_novel.txt")),
    ("n05", os.path.join(SAMPLES_DIR, "n05_formatted_novel.txt")),
    ("x01", os.path.join(SAMPLES_DIR, "x01_duplicate_name.txt")),
    ("x02", os.path.join(SAMPLES_DIR, "x02_multi_scene.txt")),
    ("x03", os.path.join(SAMPLES_DIR, "x03_mixed_cues.txt")),
    ("x04", os.path.join(SAMPLES_DIR, "x04_punctuation_storm.txt")),
    ("x05", os.path.join(SAMPLES_DIR, "x05_eight_roles.txt")),
    ("b01", os.path.join(REVIEW_DIR, "extra", "b01_railway_station.txt")),
    ("b02", os.path.join(REVIEW_DIR, "extra", "b02_midnight_cafe.txt")),
    ("b03", os.path.join(REVIEW_DIR, "extra", "b03_old_photo.txt")),
    ("b04", os.path.join(REVIEW_DIR, "extra", "b04_storm_at_sea.txt")),
    ("b05", os.path.join(REVIEW_DIR, "extra", "b05_orphanage_letter.txt")),
]


def _write_extra_inputs() -> None:
    os.makedirs(os.path.join(REVIEW_DIR, "extra"), exist_ok=True)
    extras = {
        "b01_railway_station.txt": (
            "凌晨四点的火车站，林静拖着行李箱站在空荡的候车大厅。广播里反复播放晚点通知。"
            "一个穿军大衣的老人坐在长椅上，从口袋里掏出一个铝饭盒，打开后递给她一半馒头。"
            "她摇摇头，眼泪却先掉了下来。老人把馒头放在她手边，起身走向检票口。"
            "列车进站时，她把馒头掰成两半，一半放进嘴里，另一半攥在手里。"
        ),
        "b02_midnight_cafe.txt": (
            "午夜十二点的咖啡馆，只剩一个客人。老板娘擦着杯子，问他是不是又加班。"
            "他没回答，只盯着窗外的雨。墙上挂钟指向十二点零五分。"
            "老板娘端来一杯热牛奶，说今晚这杯算她的。他喝了一口，突然笑了，说原来你还记得。"
            "雨停的时候，他把零钱压在杯底，推门离开，没有回头。"
        ),
        "b03_old_photo.txt": (
            "搬家那天，陈默在旧书箱底翻出一张泛黄的照片。照片里一家三口站在老屋前，"
            "母亲扎着辫子，父亲抱着他，他手里举着一串糖葫芦。照片背面写着'1998年春天'。"
            "他摩挲着照片，想起母亲去年去世前一直念叨着老屋前的石榴树。"
            "傍晚他开车回到老屋，树还在，房子已经拆了一半。他把照片夹进石榴树的树缝里，"
            "对着空荡荡的院子站了很久。"
        ),
        "b04_storm_at_sea.txt": (
            "风暴来临前的海面异常平静。老船长站在甲板上，望着远处压过来的黑云。"
            "水手们忙着收帆，只有见习生阿远愣在原地。船长把望远镜塞给他，说看仔细了，"
            "这片海明天就不一样了。黑云吞没最后一丝光时，浪头第一次拍上船舷。"
            "阿远抓紧桅杆，看见船长在驾驶舱里稳稳地握着舵轮，像一尊雕像。"
            "天亮时海面重归平静，阿远在航海日志上写下：风暴过去了，船还在。"
        ),
        "b05_orphanage_letter.txt": (
            "孤儿院院长把一封信交给小雨，说是十年前有人留下的。信封上没有署名，"
            "只写着'给小雨'。信里夹着一张褪色的合影，和一个地址。"
            "小雨按地址找到一座小城，敲开那扇门，开门的老太太愣了一下，"
            "颤抖着问她是不是叫小雨。小雨点点头，老太太把她拉进屋里，"
            "墙上的相框里，正是那张合影里的年轻女人。"
        ),
    }
    for name, content in extras.items():
        path = os.path.join(REVIEW_DIR, "extra", name)
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)


def _anonymise_storyboard(storyboard: dict) -> str:
    """Render scenes/shots/dialogue only. No engine, path, timestamp metadata."""
    lines: list[str] = []
    for scene in storyboard.get("scenes", []):
        heading = scene.get("heading") or scene.get("scene_heading") or "场景"
        lines.append(f"【场景】{heading}")
        for shot in scene.get("shots", []):
            lines.append(f"  镜头: {shot.get('visual', {}).get('description', '') if isinstance(shot.get('visual'), dict) else shot.get('visual_description', '')}")
            audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
            if audio.get("dialogue"):
                speaker = audio.get("speaker", "")
                lines.append(f"    对白({speaker}): {audio['dialogue']}")
            if audio.get("narration"):
                lines.append(f"    旁白: {audio['narration']}")
    return "\n".join(lines)


def _generate_pair(input_id: str, path: str, only_engines: set[str] | None = None) -> dict:
    engines = ["agent", "workflow"]
    if only_engines:
        engines = [e for e in engines if e in only_engines]
    out: dict[str, dict] = {}
    for engine in engines:
        print(f"  [{input_id}] engine={engine} ...")
        result = run_storyboard(
            path, output_dir=os.path.join(RAW_DIR, input_id, engine),
            engine=engine, agent_max_revisions=2, agent_max_steps=30, image_api=False,
        )
        if result is None:
            print(f"  [{input_id}] {engine} FAILED")
            continue
        os.makedirs(os.path.join(RAW_DIR, input_id), exist_ok=True)
        with open(os.path.join(RAW_DIR, input_id, engine + ".json"), "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        out[engine] = result
    return out


def main() -> int:
    global REVIEW_DIR, RAW_DIR, ANON_DIR, MAPPING_FILE, INPUTS
    only = set()
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--review-dir":
            REVIEW_DIR = os.path.abspath(args[i + 1])
            RAW_DIR = os.path.join(REVIEW_DIR, "raw")
            ANON_DIR = os.path.join(REVIEW_DIR, "匿名评审版")
            MAPPING_FILE = os.path.join(REVIEW_DIR, "映射-保密-请勿随评审分发.json")
            INPUTS = [
                (input_id, os.path.join(REVIEW_DIR, "extra", os.path.basename(path)))
                if input_id.startswith("b") else (input_id, path)
                for input_id, path in INPUTS
            ]
            i += 2
        elif args[i] == "--only":
            only.add(args[i + 1]); only.add(args[i + 2]); i += 3
        else:
            only.add(args[i]); i += 1
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(ANON_DIR, exist_ok=True)
    _write_extra_inputs()

    pairs: list[dict] = []
    for input_id, path in INPUTS:
        if only and input_id not in only:
            continue
        print(f"=== {input_id}: {os.path.basename(path)} ===")
        generated = _generate_pair(input_id, path, only_engines=only or None)
        if "agent" not in generated or "workflow" not in generated:
            print(f"  skip pair (missing engine output)")
            continue
        pairs.append({"input_id": input_id, "source": os.path.basename(path),
                      "agent": generated["agent"], "workflow": generated["workflow"]})

    if not pairs:
        print("no complete pairs generated")
        return 1

    # Random A/B assignment with a recorded seed (mapping stays private).
    seed = int.from_bytes(os.urandom(4), "big")
    rng = random.Random(seed)
    mapping: dict[str, dict] = {"seed": seed, "pairs": {}}
    order: list[str] = []
    for idx, pair in enumerate(pairs, start=1):
        label = f"组{idx:02d}"
        left_engine, right_engine = ("agent", "workflow") if rng.random() < 0.5 else ("workflow", "agent")
        mapping["pairs"][label] = {
            "input_id": pair["input_id"], "source": pair["source"],
            "A": left_engine, "B": right_engine,
        }
        order.append((label, pair, left_engine, right_engine))

    for label, pair, left_engine, right_engine in order:
        anon = os.path.join(ANON_DIR, f"{label}.md")
        left = _anonymise_storyboard(pair[left_engine])
        right = _anonymise_storyboard(pair[right_engine])
        with open(anon, "w", encoding="utf-8") as handle:
            handle.write(f"# {label}（故事输入：{pair['source']}）\n\n")
            handle.write("## 输入原文\n\n")
            with open(os.path.join(REVIEW_DIR, "extra" if pair["input_id"].startswith("b") else SAMPLES_DIR, pair["source"]), encoding="utf-8") as src:
                handle.write(src.read().strip() + "\n\n")
            handle.write("## A\n\n" + left + "\n\n")
            handle.write("## B\n\n" + right + "\n")

    with open(MAPPING_FILE, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle, ensure_ascii=False, indent=2)

    print(f"\n生成 {len(order)} 组评审材料 -> {ANON_DIR}")
    print(f"随机种子与映射（保密）-> {MAPPING_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
