"""Video prompt generation — reads storyboard.json and outputs neutral prompts."""

import json
import os
import re
import sys

from manju.pipeline.storyboard_schema import (
    duration_label,
    get_prompt,
    get_spoken_text,
    get_style_anchor,
    get_visual,
    normalize_storyboard,
)
from manju.utils.runtime import atomic_write_json


# ── Shot type → duration mapping ──────────────────────────────────────────────

SHOT_TYPE_DURATION = {
    "大特写": (1, 2),
    "特写": (1, 2),
    "近景": (2, 3),
    "中景": (3, 4),
    "全景": (4, 5),
    "远景": (5, 6),
    "大远景": (5, 6),
}


def _get_duration(shot_type: str) -> str:
    """Get suggested duration range for a shot type."""
    for key, (lo, hi) in SHOT_TYPE_DURATION.items():
        if key in shot_type:
            return f"{lo}-{hi}s"
    return "2-4s"


# ── Camera movement mapping ───────────────────────────────────────────────────

CAMERA_MOVEMENT_MAP = {
    "固定": "static, subtle breathing",
    "推": "slow push in",
    "拉": "slow pull out",
    "摇": "smooth pan",
    "移": "smooth pan",
    "环绕": "orbit around subject",
    "跟": "tracking shot",
}


def _map_camera(camera_movement: str) -> str:
    """Map Chinese camera movement to a neutral English description."""
    if not camera_movement:
        return "static"
    for key, mapping in CAMERA_MOVEMENT_MAP.items():
        if key in camera_movement:
            return mapping
    return camera_movement


# ── Shot type translation ────────────────────────────────────────────────────

SHOT_TYPE_EN = {
    "大特写": "extreme close-up",
    "特写": "close-up",
    "近景": "medium close-up",
    "中景": "medium shot",
    "全景": "wide shot",
    "远景": "long shot",
    "大远景": "extreme long shot",
}

def _shot_type_en(shot_type: str) -> str:
    for cn, en in SHOT_TYPE_EN.items():
        if cn in shot_type:
            return en
    return shot_type

# ── Composition translation ──────────────────────────────────────────────────

COMPOSITION_EN = {
    "三分法": "rule of thirds",
    "对称式": "symmetrical",
    "对角线": "diagonal",
    "引导线": "leading lines",
    "框架式": "frame within frame",
    "留白": "negative space",
    "中心": "centered",
}

def _composition_en(comp: str) -> str:
    for cn, en in COMPOSITION_EN.items():
        if cn in comp:
            return en
    return comp


# ── Video prompt builder ──────────────────────────────────────────────────────

def _build_video_prompts(shot: dict, style_anchor: str) -> tuple[str, str]:
    """Build neutral Chinese and English video prompts from a shot.
    
    Returns (prompt_cn, prompt_en).
    """
    visual = get_visual(shot, "description")
    camera = get_visual(shot, "camera_movement")
    camera_en = _map_camera(camera)
    dialogue = get_spoken_text(shot)
    shot_type = get_visual(shot, "shot_type")
    composition = get_visual(shot, "composition")
    image_cn = get_prompt(shot, "image_cn")
    image_en = get_prompt(shot, "image_en")

    # ── Chinese prompt: detailed, readable ──────────────────────────────────
    cn_parts = []
    if style_anchor:
        cn_parts.append(f"风格：{style_anchor}")
    if visual:
        cn_parts.append(f"画面：{visual}")
    if image_cn:
        cn_parts.append(f"角色与场景：{image_cn[:120]}")
    cn_parts.append(f"镜头运动：{camera}（{camera_en}）")
    cn_parts.append(f"景别：{shot_type}，构图：{composition}")
    if dialogue:
        cn_parts.append(f"对白：{dialogue}")
    prompt_cn = "；".join(cn_parts)

    # ── English prompt (English only) ───────────────────────────────────────
    en_parts = []
    # Extract English-only from style_anchor: strip CJK + CJK punctuation
    en_style = re.sub(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+', ' ', style_anchor) if style_anchor else ""
    en_style = re.sub(r'\s*[,;，；、。]\s*', ', ', en_style).strip(', ')
    en_style = re.sub(r',\s*,', ',', en_style).strip(', ')
    if en_style and len(en_style) > 3:
        en_parts.append(en_style)
    en_parts.append(f"{_shot_type_en(shot_type)}, {_composition_en(composition)}")
    en_parts.append(f"camera: {camera_en}")
    if image_en:
        en_parts.append(image_en[:300])
    if visual:
        visual_en = re.sub(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+', ' ', visual)
        visual_en = re.sub(r'\s{2,}', ' ', visual_en).strip()
        if visual_en and len(visual_en) > 5:
            en_parts.append(visual_en[:200])
    en_parts.append("high quality, cinematic lighting, smooth motion")
    prompt_en = ". ".join(p for p in en_parts if p and len(p) > 3)

    return prompt_cn, prompt_en


def _generate_video_prompts(storyboard: dict) -> list[dict]:
    """Generate video prompts for all shots in the storyboard."""
    style_anchor = get_style_anchor(storyboard)
    video_prompts = []

    for scene in storyboard.get("scenes", []):
        scene_id = scene.get("scene_id", 0)
        scene_mood = scene.get("visual_mood", "")

        for shot in scene.get("shots", []):
            shot_id = shot.get("shot_id", "?")
            camera_movement = get_visual(shot, "camera_movement")
            prompt_cn, prompt_en = _build_video_prompts(shot, style_anchor)

            video_prompts.append({
                "shot_id": shot_id,
                "scene_id": scene_id,
                "shot_type": get_visual(shot, "shot_type"),
                "composition": get_visual(shot, "composition"),
                "duration": duration_label(shot),
                "camera_movement_original": camera_movement,
                "camera_movement_en": _map_camera(camera_movement),
                "video_prompt_cn": prompt_cn,
                "video_prompt_en": prompt_en,
                "dialogue": get_spoken_text(shot),
                "visual_mood": scene_mood,
            })

    return video_prompts


# ── Markdown output ───────────────────────────────────────────────────────────

def _generate_video_markdown(video_prompts: list[dict], title: str) -> str:
    """Generate human-readable Markdown from video prompts."""
    lines = []
    lines.append(f"# 🎥 视频提示词 — {title}")
    lines.append("")
    lines.append(f"**总计**: {len(video_prompts)} 个镜头")
    lines.append("")

    for vp in video_prompts:
        shot_id = vp.get("shot_id", "?")
        shot_type = vp.get("shot_type", "")
        duration = vp.get("duration", "")
        camera_orig = vp.get("camera_movement_original", "")
        camera_en = vp.get("camera_movement_en", "")
        prompt_cn = vp.get("video_prompt_cn", "")
        prompt_en = vp.get("video_prompt_en", "")
        dialogue = vp.get("dialogue", "")
        mood = vp.get("visual_mood", "")

        lines.append(f"## 镜头 {shot_id}")
        lines.append("")
        lines.append(f"| 项目 | 内容 |")
        lines.append(f"|------|------|")
        lines.append(f"| **景别** | {shot_type} |")
        lines.append(f"| **建议时长** | {duration} |")
        lines.append(f"| **原始运镜** | {camera_orig} |")
        lines.append(f"| **运镜** | {camera_orig} → {camera_en} |")
        if mood:
            lines.append(f"| **氛围** | {mood} |")
        lines.append("")

        if dialogue:
            lines.append(f"**对白**: {dialogue}")
            lines.append("")

        lines.append(f"**中文视频提示词**:")
        lines.append(f"```")
        lines.append(f"{prompt_cn}")
        lines.append(f"```")
        lines.append("")
        lines.append(f"**English Video Prompt**:")
        lines.append(f"```")
        lines.append(f"{prompt_en}")
        lines.append(f"```")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_video(
    storyboard_path: str,
    output_dir: str | None = None,
    strict_exports: bool = False,
) -> list[dict] | None:
    """Generate video prompts from storyboard JSON.

    Args:
        storyboard_path: Path to storyboard.json
        output_dir: Output directory (default: same dir as storyboard.json)

    Returns:
        List of video prompt dicts on success, None on failure.
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

    # ── Count shots ───────────────────────────────────────────────────────────
    total_shots = sum(len(s.get("shots", [])) for s in storyboard.get("scenes", []))
    print(f"🎥 视频提示词生成")
    print(f"   文章: {title}")
    print(f"   镜头数: {total_shots}")
    print(f"   输出: {output_dir}")

    # ── Generate video prompts ────────────────────────────────────────────────
    video_prompts = _generate_video_prompts(storyboard)
    prompt_map = {str(item.get("shot_id", "")): item for item in video_prompts}
    for scene in storyboard.get("scenes", []):
        for shot in scene.get("shots", []):
            generated = prompt_map.get(str(shot.get("shot_id", "")))
            if generated:
                shot.setdefault("prompts", {})["video_cn"] = generated["video_prompt_cn"]
                shot.setdefault("prompts", {})["video_en"] = generated["video_prompt_en"]
    atomic_write_json(storyboard_path, storyboard)

    # ── Save JSON (internal) ────────────────────────────────────────────────────
    json_path = os.path.join(output_dir, "video_prompts.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "title": title,
            "style_anchor": get_style_anchor(storyboard),
            "total_shots": len(video_prompts),
            "shots": video_prompts,
        }, f, ensure_ascii=False, indent=2)

    # ── Save PDF ──────────────────────────────────────────────────────────
    try:
        from manju.utils.formats import write_pdf
        pdf_path = os.path.join(output_dir, "video_prompts.pdf")
        write_pdf({"title": title, "total_shots": len(video_prompts), "shots": video_prompts}, pdf_path, title)
        print(f"   📕 video_prompts.pdf → {pdf_path}")
    except Exception as e:
        print(f"   ⚠ PDF: {e}")
        if strict_exports:
            return None

    # ── Save Markdown ─────────────────────────────────────────────────────
    md_path = os.path.join(output_dir, "video_prompts.md")
    md_content = _generate_video_markdown(video_prompts, title)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"   📝 video_prompts.md  → {md_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═' * 50}")
    print(f"  ✅ 视频提示词生成完成")
    print(f"  镜头总数: {len(video_prompts)}")
    print(f"  输出目录: {output_dir}")
    print(f"{'═' * 50}")

    return video_prompts
