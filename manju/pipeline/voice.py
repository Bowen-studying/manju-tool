"""Voice script generation — reads storyboard.json, outputs voice parameters for TTS."""

import json
import os
import re
import sys

from manju.pipeline.storyboard_schema import (
    get_audio,
    get_spoken_text,
    get_visual,
    normalize_storyboard,
)
from manju.utils.ai import call_llm


# ── Emotion → parameter mapping ────────────────────────────────────────────────

EMOTION_PARAM_MAP = {
    "平静":    {"speed": 1.0, "pitch": 5, "volume": 5, "label": "日常对话"},
    "悲伤":    {"speed": 0.5, "pitch": 3, "volume": 3, "label": "悲伤"},
    "愤怒":    {"speed": 1.5, "pitch": 8, "volume": 8, "label": "愤怒"},
    "兴奋":    {"speed": 1.6, "pitch": 9, "volume": 9, "label": "兴奋/狂喜"},
    "狂喜":    {"speed": 1.6, "pitch": 9, "volume": 9, "label": "兴奋/狂喜"},
    "恐惧":    {"speed": 1.7, "pitch": 7, "volume": 4, "label": "恐惧/惊慌"},
    "惊慌":    {"speed": 1.7, "pitch": 7, "volume": 4, "label": "恐惧/惊慌"},
    "冷漠":    {"speed": 0.8, "pitch": 2, "volume": 5, "label": "冷漠/威胁"},
    "威胁":    {"speed": 0.8, "pitch": 2, "volume": 5, "label": "冷漠/威胁"},
    "温柔":    {"speed": 0.7, "pitch": 4, "volume": 4, "label": "温柔/宠溺"},
    "宠溺":    {"speed": 0.7, "pitch": 4, "volume": 4, "label": "温柔/宠溺"},
    "焦急":    {"speed": 1.4, "pitch": 6, "volume": 7, "label": "焦急"},
    "内心独白": {"speed": 0.8, "pitch": 5, "volume": 2, "label": "内心独白"},
}

DEFAULT_EMOTION_PARAMS = {"speed": 1.0, "pitch": 5, "volume": 5, "label": "日常对话"}

EDGE_CAST = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-XiaohanNeural", "zh-CN-YunyangNeural",
]
API_CAST = ["nova", "echo", "shimmer", "onyx", "alloy", "fable"]


def _heuristic_emotion(item: dict) -> str:
    combined = f"{item.get('dialogue', '')}{item.get('visual_desc', '')}{item.get('mood', '')}"
    markers = re.findall(
        r"【(愤怒|悲伤|兴奋|狂喜|恐惧|惊慌|冷漠|威胁|温柔|宠溺|焦急)】", combined)
    if markers:
        return markers[0]
    rules = [
        (["怒", "恨", "滚", "杀"], "愤怒"), (["呜", "哭", "泪", "痛", "悲"], "悲伤"),
        (["怕", "不要", "救命"], "恐惧"), (["哼", "不屑", "冷笑"], "冷漠"),
        (["快", "急", "来不及", "赶紧"], "焦急"),
        (["乖", "宝宝", "爱你", "温柔"], "温柔"), (["哈", "笑", "太好了"], "兴奋"),
    ]
    return next((emotion for words, emotion in rules if any(word in combined for word in words)), "平静")


def _batch_infer_emotions(dialogue_lines: list[dict]) -> dict[int, str]:
    """Batch-classify emotions for all dialogue lines using LLM.

    Falls back to keyword heuristic if LLM is unavailable.
    Each dict has keys: idx, dialogue, visual_desc, mood, character.
    Returns {idx: emotion_label}.
    """
    if not dialogue_lines:
        return {}

    # ── Try LLM first ──────────────────────────────────────────────────────
    system_prompt = (
        "你是一个漫剧配音导演。为每句对白选择最准确的情绪标签。\n"
        "可选标签：平静, 悲伤, 愤怒, 兴奋, 恐惧, 冷漠, 威胁, 温柔, 焦急, 内心独白\n\n"
        "关键规则：\n"
        "1. 讽刺、反语、冷笑 → 选「冷漠」，不要因为\"！\"选愤怒\n"
        "2. 威胁、警告 → 选「威胁」\n"
        "3. 表面平静但暗藏情绪 → 选实际情绪，不选「平静」\n"
        "4. 漫剧需要外放情绪，优先判断真实情感而非字面\n\n"
        "输出格式：每行 [序号] 情绪标签\n"
        "示例：\n[1] 冷漠\n[2] 愤怒\n[3] 威胁"
    )

    emotions: dict[int, str] = {}
    # Bounded chunks avoid output truncation on long scripts.
    for start in range(0, len(dialogue_lines), 40):
        chunk = dialogue_lines[start:start + 40]
        lines_text = "".join(
            f"[{item['idx']}] 角色={item['character']}, 台词=\"{item['dialogue']}\", "
            f"画面=\"{str(item.get('visual_desc', ''))[:80]}\", 场景氛围=\"{item.get('mood', '')}\"\n"
            for item in chunk
        )
        response = call_llm(system_prompt, lines_text, max_tokens=1200, temperature=0.1)
        if not response:
            continue
        pattern = re.findall(r"\[(\d+)\]\s*(\S+)", response)
        for idx_str, emo in pattern:
            idx = int(idx_str)
            if emo in EMOTION_PARAM_MAP:
                emotions[idx] = emo
    missing = 0
    for item in dialogue_lines:
        if item["idx"] not in emotions:
            emotions[item["idx"]] = _heuristic_emotion(item)
            missing += 1
    if missing:
        print(f"   ⚡ {missing} 句使用关键词情绪推断")
    if len(emotions) > missing:
        print(f"   🤖 LLM情绪分类: {len(emotions) - missing}/{len(dialogue_lines)} 句")
    return emotions


def _parse_character_from_dialogue(dialogue: str) -> str:
    """Extract speaker character name from dialogue text.

    Supports formats:
    - "角色名：台词" or "角色名:台词"
    - "【角色名】台词"
    """
    match = re.match(r"^(.+?)[:：]", dialogue)
    if match:
        name = match.group(1).strip()
        name = re.sub(r"[【\[\]】]", "", name).strip()
        if name and len(name) <= 8:
            return name

    match = re.search(r"【(.+?)】", dialogue)
    if match:
        return match.group(1).strip()

    return "旁白"


def _clean_dialogue_text(dialogue: str) -> str:
    """Remove character prefix from dialogue, keep only spoken text."""
    cleaned = re.sub(r"^.+?[:：]\s*", "", dialogue)
    cleaned = re.sub(r"【.+?】", "", cleaned).strip()
    return cleaned


def _generate_voice_description(character: str, emotion: str, params: dict) -> str:
    """Generate voice description for TTS voice cloning."""
    descriptions = {
        "平静": f"{character}用平静的语气说话，声线{_pitch_desc(params['pitch'])}，{_speed_desc(params['speed'])}",
        "愤怒": f"{character}愤怒地咆哮，声线{_pitch_desc(params['pitch'])}，语速急促，充满爆发力",
        "悲伤": f"{character}带着哭腔低语，声线{_pitch_desc(params['pitch'])}，语速缓慢沉重，气息断续",
        "兴奋": f"{character}兴奋激动，声线{_pitch_desc(params['pitch'])}，语速飞快，语调上扬",
        "恐惧": f"{character}声音发抖，声线{_pitch_desc(params['pitch'])}，语速急促断断续续，气息不稳",
        "冷漠": f"{character}冷漠疏离的语调，声线{_pitch_desc(params['pitch'])}，语速平稳不带感情",
        "温柔": f"{character}温柔宠溺的语气，声线{_pitch_desc(params['pitch'])}，语速舒缓轻柔",
        "焦急": f"{character}焦急催促，声线{_pitch_desc(params['pitch'])}，语速急促，略有颤音",
        "内心独白": f"{character}内心独白，声线{_pitch_desc(params['pitch'])}，语速缓慢，气息声明显，带有回声感",
        "狂喜": f"{character}狂喜大笑，声线{_pitch_desc(params['pitch'])}，语调极高上扬，语速极快",
        "惊慌": f"{character}惊慌失措，声线{_pitch_desc(params['pitch'])}，语速极快混乱，气息紊乱",
        "威胁": f"{character}低沉威胁的语气，声线{_pitch_desc(params['pitch'])}，语速缓慢而有压迫感",
        "宠溺": f"{character}宠溺的语气，声线{_pitch_desc(params['pitch'])}，语速舒缓，尾音上扬",
    }
    return descriptions.get(emotion, descriptions["平静"])


def _pitch_desc(pitch: int) -> str:
    if pitch <= 2:
        return "极低沉"
    elif pitch <= 4:
        return "低沉"
    elif pitch <= 6:
        return "中等"
    elif pitch <= 9:
        return "偏高"
    else:
        return "极高亢"


def _speed_desc(speed: float) -> str:
    if speed <= 0.5:
        return "极慢"
    elif speed <= 0.8:
        return "缓慢"
    elif speed <= 1.2:
        return "正常"
    elif speed <= 1.5:
        return "较快"
    elif speed <= 1.8:
        return "很快"
    else:
        return "极快"


# ── Voice script generation ─────────────────────────────────────────────────────

def _generate_voice_scripts(storyboard: dict) -> list[dict]:
    """Generate voice parameters for all dialogue lines in the storyboard.

    Returns list of voice script dicts.
    """
    voice_scripts = []

    # ── Phase 1: collect ALL shots (with and without dialogue) ──────────────
    dialogue_lines: list[dict] = []
    silent_shots: list[dict] = []

    idx = 0
    for scene in storyboard.get("scenes", []):
        scene_id = scene.get("scene_id", 0)
        scene_mood = scene.get("visual_mood", "")

        for shot in scene.get("shots", []):
            shot_id = shot.get("shot_id", "?")
            dialogue = get_spoken_text(shot)
            visual_desc = get_visual(shot, "description")

            idx += 1
            if not dialogue or not dialogue.strip():
                silent_shots.append({
                    "idx": idx, "scene_id": scene_id, "shot_id": shot_id,
                    "character": "—", "text": "（无对白）",
                    "emotion": "—", "voice_description": "纯画面镜头，无配音",
                    "speed": "—", "pitch": "—", "volume": "—",
                })
                continue

            explicit_speaker = get_audio(shot, "speaker")
            character = explicit_speaker or _parse_character_from_dialogue(dialogue)
            # v2 already separates speaker from dialogue. Only strip a prefix
            # when consuming legacy combined text such as "角色：台词".
            text = dialogue if explicit_speaker else _clean_dialogue_text(dialogue)

            if not text or not text.strip():
                silent_shots.append({
                    "idx": idx, "scene_id": scene_id, "shot_id": shot_id,
                    "character": character or "—", "text": "（无有效台词）",
                    "emotion": "—", "voice_description": "无有效对白，跳过配音",
                    "speed": "—", "pitch": "—", "volume": "—",
                })
                continue

            dialogue_lines.append({
                "idx": idx,
                "scene_id": scene_id,
                "shot_id": shot_id,
                "character": character,
                "dialogue": dialogue,
                "text": text,
                "visual_desc": visual_desc,
                "mood": scene_mood,
            })

    if not dialogue_lines and not silent_shots:
        return []

    # ── Phase 2: batch-classify emotions ─────────────────────────────────
    emotions = _batch_infer_emotions(dialogue_lines) if dialogue_lines else {}

    # Stable per-project voice casting: each character keeps one voice.
    character_order = list(dict.fromkeys(item["character"] for item in dialogue_lines))
    cast = {
        character: {
            "voice_edge": EDGE_CAST[index % len(EDGE_CAST)],
            "voice_api": API_CAST[index % len(API_CAST)],
        }
        for index, character in enumerate(character_order)
    }

    # ── Phase 3: build voice entries (all shots, sorted by idx) ────────────
    for item in dialogue_lines:
        emotion = emotions.get(item["idx"], "平静")
        params = EMOTION_PARAM_MAP.get(emotion, DEFAULT_EMOTION_PARAMS)
        voice_desc = _generate_voice_description(item["character"], emotion, params)

        voice_scripts.append({
            "_order": item["idx"],
            "shot_id": item["shot_id"],
            "scene_id": item["scene_id"],
            "character": item["character"],
            "text": item["text"],
            "emotion": emotion,
            "emotion_label": params["label"],
            "speed": params["speed"],
            "pitch": params["pitch"],
            "volume": params["volume"],
            "voice_description": voice_desc,
            **cast[item["character"]],
        })

    # Add silent shots to complete the full shot list
    for s in silent_shots:
        voice_scripts.append({
            "_order": s["idx"],
            "shot_id": s["shot_id"],
            "scene_id": s["scene_id"],
            "character": s["character"],
            "text": s["text"],
            "emotion": s["emotion"],
            "emotion_label": s["emotion"],
            "speed": s["speed"],
            "pitch": s["pitch"],
            "volume": s["volume"],
            "voice_description": s["voice_description"],
            "voice_edge": "",
            "voice_api": "",
        })

    voice_scripts.sort(key=lambda item: item["_order"])
    for item in voice_scripts:
        del item["_order"]

    return voice_scripts


# ── Markdown output ─────────────────────────────────────────────────────────────

def _generate_voice_markdown(voice_scripts: list[dict], title: str) -> str:
    """Generate human-readable Markdown from voice scripts."""
    lines = []
    lines.append(f"# 🎙️ 配音脚本 — {title}")
    lines.append("")
    lines.append(f"**总计**: {len(voice_scripts)} 句对白/旁白")
    lines.append("")

    from collections import defaultdict
    char_lines = defaultdict(list)
    for vs in voice_scripts:
        char_lines[vs["character"]].append(vs)

    for char, entries in char_lines.items():
        lines.append(f"## 角色: {char}")
        lines.append(f"**共 {len(entries)} 句**")
        lines.append("")

        for i, vs in enumerate(entries, 1):
            shot_id = vs.get("shot_id", "?")
            emotion = vs.get("emotion", "")
            text = vs.get("text", "")
            speed = vs.get("speed", 1.0)
            pitch = vs.get("pitch", 5)
            volume = vs.get("volume", 5)
            voice_desc = vs.get("voice_description", "")
            voice_edge = vs.get("voice_edge", "")
            voice_api = vs.get("voice_api", "")

            lines.append(f"### {i}. 镜头 {shot_id}")
            lines.append("")
            lines.append(f"| 参数 | 值 |")
            lines.append(f"|------|------|")
            lines.append(f"| **情绪** | {emotion} |")
            if isinstance(speed, (int, float)) and isinstance(pitch, int):
                lines.append(f"| **语速** | {speed} ({_speed_desc(speed)}) |")
                lines.append(f"| **声调** | {pitch} ({_pitch_desc(pitch)}) |")
                lines.append(f"| **音量** | {volume}/10 |")
            else:
                # Silent shots intentionally carry display placeholders rather
                # than numeric TTS parameters.
                lines.append(f"| **语速** | {speed} |")
                lines.append(f"| **声调** | {pitch} |")
                lines.append(f"| **音量** | {volume} |")
            if voice_edge:
                lines.append(f"| **Edge 音色** | {voice_edge} |")
            if voice_api:
                lines.append(f"| **API 音色** | {voice_api} |")
            lines.append("")
            lines.append(f"**对白文本**:")
            lines.append(f"> {text}")
            lines.append("")
            lines.append(f"**音色描述**:")
            lines.append(f"> {voice_desc}")
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ── Main entry point ────────────────────────────────────────────────────────────

def run_voice(
    storyboard_path: str,
    output_dir: str | None = None,
    strict_exports: bool = False,
) -> list[dict] | None:
    """Generate voice scripts from storyboard JSON.

    Args:
        storyboard_path: Path to storyboard.json
        output_dir: Output directory (default: same dir as storyboard.json)

    Returns:
        List of voice script dicts on success, None on failure.
    """
    # ── Read storyboard ───────────────────────────────────────────────────────
    try:
        with open(storyboard_path, "r", encoding="utf-8") as f:
            storyboard = json.load(f)
    except Exception as e:
        print(f"❌ 读取 storyboard.json 失败: {e}", file=sys.stderr)
        return None
    storyboard = normalize_storyboard(storyboard)

    title = storyboard.get("title", "未命名")

    # ── Determine output directory ────────────────────────────────────────────
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(storyboard_path))

    os.makedirs(output_dir, exist_ok=True)

    # ── Extract dialogue lines ────────────────────────────────────────────────
    total_shots = sum(len(s.get("shots", [])) for s in storyboard.get("scenes", []))
    print(f"🎙️  配音脚本生成")
    print(f"   文章: {title}")
    print(f"   总镜头: {total_shots}")
    print(f"   输出: {output_dir}")

    # ── Generate voice scripts ────────────────────────────────────────────────
    voice_scripts = _generate_voice_scripts(storyboard)

    if not voice_scripts:
        print("   ⚠️ 未检测到对白/旁白内容")
        return []

    print(f"   提取 {len(voice_scripts)} 句对白/旁白")

    # ── Save JSON ──────────────────────────────────────────────────────────────
    json_path = os.path.join(output_dir, "voice_scripts.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "title": title,
            "total_lines": len(voice_scripts),
            "lines": voice_scripts,
        }, f, ensure_ascii=False, indent=2)

    # ── Save PDF ──────────────────────────────────────────────────────────────
    try:
        from manju.utils.formats import write_pdf
        pdf_path = os.path.join(output_dir, "voice_scripts.pdf")
        write_pdf({"title": title, "total_lines": len(voice_scripts), "lines": voice_scripts}, pdf_path, title)
        print(f"   📕 voice_scripts.pdf → {pdf_path}")
    except Exception as e:
        print(f"   ⚠ PDF: {e}")
        if strict_exports:
            return None

    # ── Save Markdown ─────────────────────────────────────────────────────────
    md_path = os.path.join(output_dir, "voice_scripts.md")
    md_content = _generate_voice_markdown(voice_scripts, title)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"   📝 voice_scripts.md  → {md_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    char_count = len(set(vs["character"] for vs in voice_scripts))
    print(f"\n{'═' * 50}")
    print(f"  ✅ 配音脚本生成完成")
    print(f"  对白总数: {len(voice_scripts)} 句")
    print(f"  涉及角色: {char_count} 个")
    print(f"  输出目录: {output_dir}")
    print(f"{'═' * 50}")

    return voice_scripts
