"""manju adapt — novel-to-script adapter.

Reads a novel/short story text file, uses LLM to extract characters, scenes,
and dialogues into a structured script JSON compatible with manju storyboard.
"""

import json
import os
import sys
from datetime import datetime

from manju.utils.ai import call_llm, parse_json_response
from manju.utils.config import count_content_units
from manju.utils.runtime import atomic_write_json, available_path, content_fingerprint, read_json, safe_filename


# ── Script extraction prompt ────────────────────────────────────────────────────

def _build_adapt_prompt(novel_text: str, title: str, genre: str = "") -> tuple[str, str]:
    """Build system and user prompts for novel-to-script adaptation."""

    genre_hint = f"\n【类型提示】{genre}" if genre else ""

    system_prompt = f"""你是专业漫剧编剧。将小说文本转化为可制作的结构化剧本。

【你的任务】
1. 识别全部角色（主角+配角），为每个角色写简短视觉锚定
2. 划分场景（按地点/时间变化），每场景含地点+时间+氛围
3. 提取每句对白并标注说话人
4. 保留叙事连贯性，去除纯描写性段落或压缩为一句话画面描述

【输出格式 — 严格JSON】
{{{{
  "title": "标题",
  "genre": "类型（古风/现代/科幻/悬疑/甜宠...）",
  "characters": [
    {{{{ "name": "角色名", "role": "主角/配角/反派", "visual_anchor": "外貌+服装+体型+标志性特征" }}}}
  ],
  "scenes": [
    {{{{ "scene_id": 1, "location": "地点", "time": "时间", "mood": "氛围关键词",
       "summary": "本场一句话概要",
       "dialogues": [
         {{{{ "character": "说话人", "text": "对白内容" }}}}
       ],
       "action_notes": "舞台指示/动作描述（无对白的叙事推进）"
    }}}}
  ]
}}}}"""

    user_prompt = f"""请将以下小说转化为漫剧剧本。
{genre_hint}

【小说标题】{title}
【小说正文】
{novel_text}

请严格按照JSON格式输出剧本。确保：每个角色有visual_anchor，每场戏有地点+时间+氛围，所有对白标注说话人。"""

    return system_prompt, user_prompt


# ── Main entry point ────────────────────────────────────────────────────────────

def run_adapt(
    file_path: str,
    output_dir: str | None = None,
    genre: str = "",
    output_base: str = "",
) -> dict | None:
    """Adapt a novel text file into a structured script JSON.

    Args:
        file_path: Path to novel text file (.txt)
        output_dir: Output directory (default: OUTPUT_BASE/<date>/)
        genre: Genre hint (古风/现代/科幻/悬疑/甜宠...)
        output_base: Base output directory

    Returns:
        Script dict on success, None on failure.
    """
    # ── Read novel ───────────────────────────────────────────────────────────
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            novel_text = f.read()
    except Exception as e:
        print(f"❌ 读取小说文件失败: {e}", file=sys.stderr)
        return None

    if not novel_text or not novel_text.strip():
        print("❌ 小说文件内容为空", file=sys.stderr)
        return None

    title = os.path.splitext(os.path.basename(file_path))[0]
    word_count = count_content_units(novel_text)

    # ── Determine output directory ──────────────────────────────────────────
    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(output_base or os.path.join(os.getcwd(), "manju-output"), today)
    os.makedirs(output_dir, exist_ok=True)

    print(f"📖 小说: {title}")
    print(f"   字数: {word_count} 中文字符")
    if genre:
        print(f"   类型: {genre}")
    print(f"   输出: {output_dir}")
    print()

    # ── Chunked adaptation: never discard the ending of a long novel ───────
    chunk_size = 50_000
    chunks = [novel_text[i:i + chunk_size] for i in range(0, len(novel_text), chunk_size)]
    stage_dir = os.path.join(output_dir, "adapt_stages")
    os.makedirs(stage_dir, exist_ok=True)
    partials: list[dict] = []
    print(f"🤖 正在分析小说结构... ({len(chunks)} 个分块)")
    for index, chunk in enumerate(chunks, 1):
        fingerprint = content_fingerprint(chunk, genre, "adapt-v2")
        stage_path = os.path.join(stage_dir, f"chunk_{index:03d}.json")
        cached = read_json(stage_path)
        if cached and cached.get("_fingerprint") == fingerprint:
            cached.pop("_fingerprint", None)
            partials.append(cached)
            continue
        system_prompt, user_prompt = _build_adapt_prompt(
            chunk, f"{title}（第{index}/{len(chunks)}部分）", genre)
        response = call_llm(system_prompt, user_prompt, max_tokens=12000)
        part = parse_json_response(response) if response else None
        if not isinstance(part, dict) or not isinstance(part.get("scenes"), list):
            if response:
                with open(os.path.join(stage_dir, f"chunk_{index:03d}_raw.txt"),
                          "w", encoding="utf-8") as handle:
                    handle.write(response)
            print(f"❌ 小说第 {index} 块改编失败；重试时将复用已完成分块", file=sys.stderr)
            return None
        atomic_write_json(stage_path, {**part, "_fingerprint": fingerprint})
        partials.append(part)

    characters: dict[str, dict] = {}
    scenes: list[dict] = []
    for part in partials:
        for character in part.get("characters", []):
            if isinstance(character, dict) and character.get("name"):
                characters.setdefault(str(character["name"]), character)
        scenes.extend(scene for scene in part.get("scenes", []) if isinstance(scene, dict))
    for index, scene in enumerate(scenes, 1):
        scene["scene_id"] = index
    script = {
        "title": title,
        "genre": next((part.get("genre") for part in partials if part.get("genre")), genre),
        "characters": list(characters.values()),
        "scenes": scenes,
    }
    if not scenes:
        print("❌ 改编结果没有有效场景", file=sys.stderr)
        return None

    # ── Add metadata ───────────────────────────────────────────────────────
    script["source_file"] = file_path
    script["source_word_count"] = word_count
    script["adaptation_date"] = datetime.now().isoformat()

    # ── Save JSON ──────────────────────────────────────────────────────────
    json_path = available_path(os.path.join(output_dir, f"{safe_filename(title, 'novel')}_script.json"))
    atomic_write_json(json_path, script)
    print(f"   📄 剧本JSON → {json_path}")

    # ── Summary ────────────────────────────────────────────────────────────
    chars = script.get("characters", [])
    scenes = script.get("scenes", [])
    total_dialogues = sum(len(s.get("dialogues", [])) for s in scenes)
    print(f"\n{'═' * 50}")
    print(f"  ✅ 剧本适配完成")
    print(f"  角色: {len(chars)} 个 — {', '.join(c.get('name', '?') for c in chars)}")
    print(f"  场次: {len(scenes)} 场")
    print(f"  对白: {total_dialogues} 句")
    print(f"  输出: {json_path}")
    print(f"{'═' * 50}")
    print(f"\n下一步: manju storyboard \"{json_path}\"")

    script["_output_path"] = json_path
    return script
