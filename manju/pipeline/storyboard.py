"""Storyboard generation with a staged, versioned output pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime

from manju.pipeline.generate_image import run_batch_images
from manju.pipeline.storyboard_schema import (
    duration_label,
    get_audio,
    get_characters,
    get_prompt,
    get_scene_heading,
    get_spoken_text,
    get_style_anchor,
    get_visual,
    normalize_storyboard,
)
from manju.pipeline.storyboard_stages import generate_storyboard_staged
from manju.utils.config import count_content_units
from manju.utils.formats import write_xlsx
from manju.utils.runtime import atomic_write_json, content_fingerprint, safe_filename


def _extract_title(file_path: str) -> str:
    """Extract a clean title from a filename."""
    name = os.path.splitext(os.path.basename(file_path))[0]
    for suffix in ("_script", "_storyboard", "_adapt", "_renamed"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name or "未命名"


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _scenes_by_word_count(word_count: int) -> int:
    if word_count <= 2000:
        return 3
    if word_count <= 6000:
        return 4
    return 6


def _read_story_source(file_path: str) -> tuple[str, str] | None:
    """Return (LLM input text, preferred title), accepting txt/json/docx."""
    try:
        if file_path.lower().endswith(".docx"):
            from manju.utils.formats import read_input

            value = read_input(file_path)
            if value is None:
                return None
            raw_text = (
                json.dumps(value, ensure_ascii=False, indent=2)
                if isinstance(value, dict)
                else str(value)
            )
            return raw_text, _extract_title(file_path)

        with open(file_path, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
        title = _extract_title(file_path)
        if file_path.lower().endswith(".json"):
            try:
                payload = json.loads(raw_text)
                if isinstance(payload, dict):
                    candidate = payload.get("title")
                    if isinstance(candidate, str) and candidate.strip():
                        title = candidate.strip()
            except json.JSONDecodeError:
                # Let the LLM see malformed-but-readable text; the caller still
                # reports file errors separately from model errors.
                pass
        return raw_text, title
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"❌ 读取文件失败: {exc}", file=sys.stderr)
        return None


def _generate_markdown(storyboard: dict) -> str:
    """Render either v1 or v2 storyboard data as readable Markdown."""
    title = storyboard.get("title", "未命名")
    lines = [f"# 📋 分镜脚本 — {title}", "", f"**视觉风格**：{get_style_anchor(storyboard)}", ""]

    characters = get_characters(storyboard)
    if characters:
        lines.extend(["## 👥 角色视觉锚定", ""])
        for character in characters:
            lines.append(
                f"- **{character.get('name', '?')}**：{character.get('anchor_description', '')}"
            )
        lines.append("")

    for scene in storyboard.get("scenes", []):
        scene_id = scene.get("scene_id", "?")
        lines.append(f"## 🎬 场景 {scene_id}：{get_scene_heading(scene)}")
        lines.append(f"**氛围**：{scene.get('visual_mood', '')}")
        if scene.get("purpose"):
            lines.append(f"**叙事目的**：{scene['purpose']}")
        if scene.get("scene_template"):
            lines.append(f"**场景母版**：{scene['scene_template']}")
        lines.append("")

        for shot in scene.get("shots", []):
            shot_id = shot.get("shot_id", "?")
            dialogue = get_spoken_text(shot)
            lines.extend([
                f"### 镜头 {shot_id}",
                "",
                "| 项目 | 内容 |",
                "|------|------|",
                f"| **景别** | {get_visual(shot, 'shot_type')} |",
                f"| **构图** | {get_visual(shot, 'composition')} |",
                f"| **构图情感** | {get_visual(shot, 'composition_emotion')} |",
                f"| **运动/机位** | {get_visual(shot, 'camera_movement')} |",
                f"| **色调/情绪** | {get_visual(shot, 'color_tone')} |",
                f"| **时长** | {duration_label(shot)} |",
                "",
                "**画面内容**：",
                f"> {get_visual(shot, 'description')}",
                "",
            ])
            if dialogue:
                lines.extend(["**对白/画外音**：", f"> {dialogue}", ""])
            sound = get_audio(shot, "sound_music")
            if sound:
                lines.extend([f"**音效/音乐**：{sound}", ""])
            lines.extend([
                "**中文生图提示词**：",
                "```",
                get_prompt(shot, "image_cn"),
                "```",
                "",
                "**English Image Prompt**：",
                "```",
                get_prompt(shot, "image_en"),
                "```",
                "",
            ])
            video_prompt = get_prompt(shot, "video")
            if video_prompt:
                lines.extend(["**生视频提示词**：", "```", video_prompt, "```", ""])
            lines.extend(["---", ""])
    return "\n".join(lines)


def _generate_images_from_storyboard(storyboard: dict, output_dir: str) -> int:
    """Generate images and attach deterministic local paths to successful shots."""
    shots_info: list[dict] = []
    for scene in storyboard.get("scenes", []):
        for shot in scene.get("shots", []):
            shot_id = str(shot.get("shot_id", ""))
            prompt = get_prompt(shot, "image_en") or get_prompt(shot, "image_cn")
            if prompt and shot_id:
                prompt_hash = content_fingerprint(prompt, length=8)
                shots_info.append({
                    "shot_id": shot_id,
                    "reference_group": str(scene.get("scene_id", "default")),
                    "prompt": prompt,
                    "output_filename": f"shot_{safe_filename(shot_id, 'unknown')}_{prompt_hash}.png",
                    "shot": shot,
                })
    if not shots_info:
        print("   ⚠️ 未找到可用的生图提示词")
        return 0

    count = run_batch_images(shots_info, output_dir)
    for item in shots_info:
        path = os.path.join(output_dir, "images", item["output_filename"])
        if os.path.isfile(path):
            item["shot"].setdefault("assets", {})["image"] = os.path.relpath(path, output_dir)
            item["shot"].setdefault("status", {})["image"] = "completed"
    return count


def run_storyboard(
    file_path: str,
    output_dir: str | None = None,
    max_scenes: int | None = None,
    image_api: bool = False,
    output_base: str = "",
    resume: bool = True,
    strict_exports: bool = False,
) -> dict | None:
    """Generate storyboard v2 via plan -> per-scene shots -> validation."""
    source = _read_story_source(file_path)
    if source is None:
        return None
    raw_text, title = source
    if not raw_text.strip():
        print("❌ 文件内容为空", file=sys.stderr)
        return None

    word_count = count_content_units(raw_text)
    target_scenes = max_scenes if max_scenes is not None else _scenes_by_word_count(word_count)
    target_scenes = max(1, min(target_scenes, 8))

    now = datetime.now()
    today = f"{now.year}.{now.month}.{now.day}"
    storyboard_dir = output_dir or os.path.join(output_base, today, "storyboard")
    os.makedirs(storyboard_dir, exist_ok=True)

    print(f"📖 文章: {title}")
    print(f"   字数: {word_count} → 目标场景: {target_scenes} 场")
    print(f"   输出: {storyboard_dir}")
    print("\n🎬 多阶段生成分镜脚本中...")
    sys.stdout.flush()

    storyboard = generate_storyboard_staged(
        _clean_text(raw_text),
        title,
        word_count,
        target_scenes,
        os.path.join(storyboard_dir, "stages"),
        resume=resume,
    )
    if storyboard is None:
        return None

    storyboard = normalize_storyboard(storyboard, title=title, metadata={
        "source_file": os.path.abspath(file_path),
        "source_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "word_count": word_count,
        "target_scene_count": target_scenes,
        "generation_flow": "plan -> per-scene shots -> normalize/validate",
    })
    total_scenes = len(storyboard["scenes"])
    total_shots = sum(len(scene.get("shots", [])) for scene in storyboard["scenes"])
    print(f"   ✅ 生成 {total_scenes} 场戏, {total_shots} 个镜头")

    json_path = os.path.join(storyboard_dir, "storyboard.json")
    atomic_write_json(json_path, storyboard)

    try:
        xlsx_path = os.path.join(storyboard_dir, "storyboard.xlsx")
        write_xlsx(storyboard, xlsx_path)
        print(f"   📊 storyboard.xlsx → {xlsx_path}")
    except Exception as exc:
        print(f"   ⚠ Excel: {exc}")
        if strict_exports:
            return None

    md_path = os.path.join(storyboard_dir, "storyboard.md")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(_generate_markdown(storyboard))
    print(f"   📝 storyboard.md  → {md_path}")

    if image_api:
        image_count = _generate_images_from_storyboard(storyboard, storyboard_dir)
        print(f"   🖼️  生图完成: {image_count}/{total_shots} 张")
        atomic_write_json(json_path, storyboard)

    print(f"\n{'═' * 50}")
    print("  ✅ 分镜生成完成")
    print(f"  输出目录: {storyboard_dir}")
    print(f"  场景: {total_scenes} 场 | 镜头: {total_shots} 个")
    print(f"  视觉风格: {(get_style_anchor(storyboard)[:60] or 'N/A')}...")
    print(f"{'═' * 50}")
    return storyboard
