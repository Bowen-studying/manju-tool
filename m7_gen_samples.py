#!/usr/bin/env python3
"""Generate the M7 fixed 20-sample set with a versioned manifest.

Each sample records: id, kind (script/novel/storyboard/legacy_storyboard/complex),
source SHA-256, expected pipeline, enabled config, budget ceiling and allowed
manual gates.  Sources are deterministic so re-running regenerates identical
bytes (same SHA-256).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m7_samples")
MANIFEST = os.path.join(OUT_DIR, "manifest.json")

SAMPLES: list[dict] = []


def add_sample(sample_id: str, kind: str, filename: str, content: str, *, expected: str, config: dict, budget: int = 1000, gates: list[str] | None = None) -> None:
    SAMPLES.append({
        "id": sample_id, "kind": kind, "filename": filename,
        "content": content,
        "expected_stage": expected, "enabled_config": config,
        "budget_ceiling_minor": budget,
        "allowed_manual_gates": gates or [],
    })


# ---------- A: 5 short scripts (source_type=script) ----------
add_sample(
    "s01-short-dialogue", "script", "s01_short_dialogue.txt",
    "角色A：你来了。\n角色B：嗯，等很久了。\n角色A：东西带了吗？\n角色B：带了。\n（两人对视，沉默片刻）\n角色A：那就开始吧。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "s02-monologue", "script", "s02_monologue.txt",
    "（舞台中央，独白）\n我站在这里，等着天亮。\n没有人知道昨天发生了什么，也没有人想知道。\n（停顿）\n但只要太阳照常升起，我就还会继续走。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "s03-silent-action", "script", "s03_silent_action.txt",
    "（无对白动作场景）\n深夜的仓库。\n一个人影从通风口翻入，落地无声。\n他扫视四周，快步走向保险柜，掏出工具开始解锁。\n警报突然响起，灯光大亮。",
    expected="visual", config={"visual": True},
)

add_sample(
    "s04-special-chars", "script", "s04_special_chars.txt",
    "角色甲：他说——'明天见'，然后就走了。\n角色乙：等等，\"明天见\"？不是\"后天见\"？\n角色甲：你自己听！他说的就是：明天见…明天见！\n（两人同时愣住）\n角色乙：……那完了。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "s05-single-shot", "script", "s05_single_shot.txt",
    "一扇门。\n门缓缓打开，光线涌入。\n一个人站在门口，影子拉得很长。",
    expected="visual", config={"visual": True},
)

# ---------- B: 5 novel fragments (source_type=novel) ----------
add_sample(
    "n01-short-novel", "novel", "n01_short_novel.txt",
    "雨下了一整夜。\n林晚坐在窗边，看着雨滴顺着玻璃滑落。她想起十年前那个同样下着雨的夜晚，想起父亲临走前说的那句话：\n\"不管发生什么，都要把灯留着。\"\n她伸手，打开了台灯。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

LONG_NOVEL = "\n".join(
    f"第{section}段。夜色如墨，城市在远处沉睡。{i}号路灯下，一个身影缓缓走过，影子被拉得很长很长。"
    for section in range(1, 41) for i in range(1, 4)
)
add_sample(
    "n02-long-novel", "novel", "n02_long_novel.txt", LONG_NOVEL,
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "n03-dialogue-novel", "novel", "n03_dialogue_novel.txt",
    "\"你真的要走？\"小满问。\n阿澈没有回头：\"火车不等人。\"\n\"那……我等你回来。\"\n\"别等。\"\n阿澈踏上车门，又停住，低声说：\"等我。\"\n小满笑了，眼泪却掉了下来。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "n04-scenery-novel", "novel", "n04_scenery_novel.txt",
    "山谷里起了雾。\n雾是淡青色的，从溪谷底部缓缓升起，缠绕着老松的枝干。他站在崖边，看着雾海翻涌，心里却意外地平静。\n风从耳畔掠过，带着松脂和泥土的气息。\n他想，也许这就是归处。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "n05-formatted-novel", "novel", "n05_formatted_novel.txt",
    "【楔子】\n那是公元2077年。\n——也是人类最后一次看见星空的一年。\n（档案记载：\"观测站全体人员失踪，原因不明。\"）\n\n【第一章】\n废墟之上，少女捡起一块残破的屏幕。\n屏幕上，一行字在闪烁：\n\"他们来了。\"",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

# ---------- C: 5 legacy storyboards (source_type=storyboard, JSON) ----------
def sb_v2(shot_count: int = 2, *, seed_speaker: str = "A") -> dict:
    return {
        "schema_version": "2.0",
        "title": "legacy-v2",
        "creative_bible": {"style_anchor": "ink", "characters": [{"name": seed_speaker}]},
        "scenes": [{
            "scene_id": "1",
            "heading": "INT. ROOM - DAY",
            "shots": [
                {
                    "shot_id": f"1.{i}",
                    "duration_seconds": 2.5,
                    "visual": {"description": f"Shot {i} visual"},
                    "audio": {"speaker": seed_speaker, "dialogue": f"第{i}句对白"},
                    "prompts": {"image_cn": f"画面{i}", "image_en": f"frame{i}"},
                }
                for i in range(1, shot_count + 1)
            ],
        }],
    }


def sb_v1() -> dict:
    return {
        "schema_version": "1.0",
        "title": "legacy-v1",
        "style_anchor": "flat watercolor",
        "scenes": [{
            "scene_id": "1",
            "scene_heading": "EXT. STREET - NIGHT",
            "shots": [
                {"shot_id": "1.1", "visual_description": "一个人走过街灯",
                 "image_prompt_cn": "街灯下的人物", "image_prompt_en": "a person walking under a street lamp",
                 "dialogue_narration": "夜色很静。"},
            ],
        }],
    }


add_sample(
    "c01-legacy-v2", "legacy_storyboard", "c01_legacy_v2.json",
    json.dumps(sb_v2(2), ensure_ascii=False, indent=2),
    expected="video_prompt+visual", config={"video_prompt": True, "visual": True},
)

add_sample(
    "c02-legacy-v1", "legacy_storyboard", "c02_legacy_v1.json",
    json.dumps(sb_v1(), ensure_ascii=False, indent=2),
    expected="video_prompt+visual", config={"video_prompt": True, "visual": True},
)

add_sample(
    "c03-legacy-mixed", "legacy_storyboard", "c03_legacy_mixed.json",
    json.dumps({
        "schema_version": "2.0",
        "title": "legacy-mixed",
        "creative_bible": {"style_anchor": "ink", "characters": []},
        "scenes": [{
            "scene_id": "1",
            "heading": "EXT. STREET - NIGHT",
            "shots": [
                {"shot_id": "1.1", "duration_seconds": 3, "visual": {"description": "wide shot"},
                 "audio": {"speaker": "B", "dialogue": "新旧混合"}, "prompts": {"image_cn": "画面", "image_en": "frame"}},
                {"shot_id": "1.2", "visual_description": "legacy shot", "dialogue_narration": "旧字段台词",
                 "image_prompt_cn": "画面", "image_prompt_en": "frame"},
            ],
        }],
    }, ensure_ascii=False, indent=2),
    expected="video_prompt+visual", config={"video_prompt": True, "visual": True},
)

add_sample(
    "c04-legacy-long", "legacy_storyboard", "c04_legacy_long.json",
    json.dumps(sb_v2(32, seed_speaker="C"), ensure_ascii=False, indent=2),
    expected="video_prompt+visual", config={"video_prompt": True, "visual": True},
)

add_sample(
    "c05-legacy-silent", "legacy_storyboard", "c05_legacy_silent.json",
    json.dumps({
        "schema_version": "2.0",
        "title": "legacy-silent",
        "creative_bible": {"style_anchor": "ink", "characters": []},
        "scenes": [{
            "scene_id": "1",
            "heading": "INT. VAULT - NIGHT",
            "shots": [
                {"shot_id": "1.1", "duration_seconds": 4, "visual": {"description": "empty vault"},
                 "audio": {}, "prompts": {"image_cn": "金库", "image_en": "vault"}},
                {"shot_id": "1.2", "duration_seconds": 5, "visual": {"description": "door opens"},
                 "audio": {"narration": "门开了"}, "prompts": {"image_cn": "开门", "image_en": "door"}},
            ],
        }],
    }, ensure_ascii=False, indent=2),
    expected="video_prompt+visual", config={"video_prompt": True, "visual": True},
)

# ---------- D: 5 complex multi-role / multi-scene ----------
add_sample(
    "x01-duplicate-name", "script", "x01_duplicate_name.txt",
    "角色A：你是谁？\n角色A：我是A。\n角色B：他也是A？\n角色A：不，我是另一个A。\n角色B：……你们俩谁先来的？",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

MULTI_SCENE = "\n".join(
    f"【场景{i}】\n第{i}个场景。角色{i}：这是场景{i}的对白。\n（场景{i}的动作描述）"
    for i in range(1, 6)
)
add_sample(
    "x02-multi-scene", "script", "x02_multi_scene.txt", MULTI_SCENE,
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "x03-mixed-cues", "script", "x03_mixed_cues.txt",
    "角色甲：有对白。\n（纯动作，无对白）\n旁白：也有旁白。\n（又一个无对白镜头）\n角色乙：最后一句对白。",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

add_sample(
    "x04-punctuation-storm", "script", "x04_punctuation_storm.txt",
    "角色一：他说\"（别走）\"！\n角色二：'这、这、这……'\n角色一：---不是吧---\n角色二：……（沉默）……\n角色一：￥%……&*（乱码般的台词）",
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)

EIGHT_ROLES = "\n".join(
    f"角色{name}：我是{name}，第{i}句。"
    for i, name in enumerate(["一", "二", "三", "四", "五", "六", "七", "八"], start=1)
)
add_sample(
    "x05-eight-roles", "script", "x05_eight_roles.txt", EIGHT_ROLES,
    expected="voice_tts+visual", config={"voice_script": True, "voice_director": True, "voice_tts": "offline_mock"},
)


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest_entries = []
    for sample in SAMPLES:
        rel = sample["filename"]
        path = os.path.join(OUT_DIR, rel)
        content = sample["content"]
        if rel.endswith(".json"):
            raw = content.encode("utf-8")
        else:
            raw = content.encode("utf-8")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        sha = hashlib.sha256(raw).hexdigest()
        manifest_entries.append({
            "id": sample["id"], "kind": sample["kind"], "filename": rel,
            "source_sha256": sha, "bytes": len(raw),
            "expected_stage": sample["expected_stage"],
            "enabled_config": sample["enabled_config"],
            "budget_ceiling_minor": sample["budget_ceiling_minor"],
            "allowed_manual_gates": sample["allowed_manual_gates"],
        })
    manifest = {
        "schema_version": "m7-samples-v1",
        "generated_at": "2026-08-23T00:00:00Z",
        "count": len(manifest_entries),
        "samples": manifest_entries,
    }
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(f"wrote {len(manifest_entries)} samples to {OUT_DIR}")
    for entry in manifest_entries:
        print(f"  {entry['id']:24s} {entry['kind']:18s} {entry['source_sha256'][:12]}  {entry['bytes']}B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
