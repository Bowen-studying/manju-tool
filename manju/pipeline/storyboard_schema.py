"""Versioned storyboard schema helpers.

The v2 schema keeps visual, audio, prompt, and asset concerns separate while
these accessors continue to accept the flat v1 payload produced by manju 0.5.
"""

from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = "2.0"


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def get_style_anchor(storyboard: dict) -> str:
    return _text(_dict(storyboard.get("creative_bible")).get("style_anchor")) or _text(
        storyboard.get("style_anchor")
    )


def get_characters(storyboard: dict) -> list[dict]:
    characters = _dict(storyboard.get("creative_bible")).get("characters")
    if not isinstance(characters, list):
        characters = storyboard.get("characters", [])
    return [item for item in characters if isinstance(item, dict)]


def get_scene_heading(scene: dict) -> str:
    return _text(scene.get("heading")) or _text(scene.get("scene_heading"))


def get_visual(shot: dict, key: str) -> str:
    aliases = {"description": "visual_description"}
    return _text(_dict(shot.get("visual")).get(key)) or _text(shot.get(aliases.get(key, key)))


def get_audio(shot: dict, key: str) -> str:
    audio = _dict(shot.get("audio"))
    value = _text(audio.get(key))
    if value:
        return value
    if key == "sound_music":
        return _text(shot.get("sound_music"))
    if key in ("dialogue", "narration"):
        legacy = _text(shot.get("dialogue_narration"))
        if key == "dialogue":
            return legacy
    return ""


def get_spoken_text(shot: dict) -> str:
    return get_audio(shot, "dialogue") or get_audio(shot, "narration")


def get_prompt(shot: dict, key: str) -> str:
    aliases = {
        "image_cn": "image_prompt_cn",
        "image_en": "image_prompt_en",
        "video": "video_prompt",
        "video_cn": "视频提示词_中文",
        "video_en": "视频提示词_英文",
    }
    return _text(_dict(shot.get("prompts")).get(key)) or _text(shot.get(aliases.get(key, key)))


def get_duration_seconds(shot: dict) -> float:
    value = shot.get("duration_seconds")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    legacy = _text(shot.get("duration"))
    match = re.search(r"\d+(?:\.\d+)?", legacy)
    return float(match.group()) if match else 3.0


def duration_label(shot: dict) -> str:
    seconds = get_duration_seconds(shot)
    return f"{seconds:g}s"


def normalize_character(character: dict) -> dict:
    return {
        "name": _text(character.get("name")) or "未命名角色",
        "role": _text(character.get("role")),
        "anchor_description": _text(character.get("anchor_description"))
        or _text(character.get("visual_anchor")),
    }


def normalize_shot(shot: dict, scene_id: str, index: int) -> dict:
    audio = _dict(shot.get("audio"))
    dialogue = _text(audio.get("dialogue"))
    narration = _text(audio.get("narration"))
    if not dialogue and not narration:
        dialogue = _text(shot.get("dialogue_narration"))

    assets = _dict(shot.get("assets"))
    status = _dict(shot.get("status"))
    shot_id = _text(shot.get("shot_id")) or f"{scene_id}.{index}"
    normalized_assets = {
        "image": _text(assets.get("image")) or _text(shot.get("image_path")),
        "voice": _text(assets.get("voice")),
        "video": _text(assets.get("video")),
    }
    return {
        "shot_id": shot_id,
        "duration_seconds": get_duration_seconds(shot),
        "visual": {
            "shot_type": get_visual(shot, "shot_type"),
            "composition": get_visual(shot, "composition"),
            "composition_emotion": get_visual(shot, "composition_emotion"),
            "camera_movement": get_visual(shot, "camera_movement"),
            "description": get_visual(shot, "description"),
            "color_tone": get_visual(shot, "color_tone"),
        },
        "audio": {
            "speaker": _text(audio.get("speaker")),
            "dialogue": dialogue,
            "narration": narration,
            "sound_music": get_audio(shot, "sound_music"),
        },
        "prompts": {
            "image_cn": get_prompt(shot, "image_cn"),
            "image_en": get_prompt(shot, "image_en"),
            "video": get_prompt(shot, "video"),
            "video_cn": get_prompt(shot, "video_cn"),
            "video_en": get_prompt(shot, "video_en"),
        },
        "assets": normalized_assets,
        "status": {
            media: _text(status.get(media)) or ("completed" if normalized_assets[media] else "pending")
            for media in ("image", "voice", "video")
        },
    }


def normalize_scene(scene: dict, index: int) -> dict:
    scene_id = str(scene.get("scene_id") or index)
    shots = scene.get("shots", [])
    if not isinstance(shots, list):
        shots = []
    continuity = _dict(scene.get("continuity"))
    return {
        "scene_id": scene_id,
        "heading": get_scene_heading(scene),
        "purpose": _text(scene.get("purpose")),
        "visual_mood": _text(scene.get("visual_mood")),
        "scene_template": _text(scene.get("scene_template")),
        "continuity": {
            "from_previous": _text(continuity.get("from_previous")),
            "to_next": _text(continuity.get("to_next")),
        },
        "shots": [
            normalize_shot(shot, scene_id, shot_index)
            for shot_index, shot in enumerate(shots, 1)
            if isinstance(shot, dict)
        ],
    }


def normalize_storyboard(storyboard: dict, *, title: str = "未命名", metadata: dict | None = None) -> dict:
    scenes = storyboard.get("scenes", [])
    if not isinstance(scenes, list):
        scenes = []
    bible = _dict(storyboard.get("creative_bible"))
    return {
        "schema_version": SCHEMA_VERSION,
        "title": _text(storyboard.get("title")) or title,
        "metadata": {**_dict(storyboard.get("metadata")), **(metadata or {})},
        "creative_bible": {
            "style_anchor": get_style_anchor(storyboard),
            "aspect_ratio": _text(bible.get("aspect_ratio")) or "9:16",
            "characters": [normalize_character(item) for item in get_characters(storyboard)],
        },
        "scenes": [
            normalize_scene(scene, index)
            for index, scene in enumerate(scenes, 1)
            if isinstance(scene, dict)
        ],
    }


def validate_storyboard(storyboard: dict) -> list[str]:
    """Return actionable structural validation errors."""
    errors: list[str] = []
    if not get_style_anchor(storyboard):
        errors.append("分镜缺少创意圣经 style_anchor")
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return ["分镜缺少有效 scenes"]
    seen: set[str] = set()
    for scene_index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict) or not get_scene_heading(scene):
            errors.append(f"场景 {scene_index} 缺少 heading")
        shots = scene.get("shots") if isinstance(scene, dict) else None
        if not isinstance(shots, list) or not shots:
            errors.append(f"场景 {scene_index} 缺少镜头")
            continue
        for shot_index, shot in enumerate(shots, 1):
            shot_id = _text(shot.get("shot_id")) if isinstance(shot, dict) else ""
            if not shot_id:
                errors.append(f"场景 {scene_index} 的镜头 {shot_index} 缺少 shot_id")
            elif shot_id in seen:
                errors.append(f"重复 shot_id: {shot_id}")
            elif not re.fullmatch(r"\d+(?:\.\d+)+", shot_id):
                errors.append(f"非法 shot_id（应如 1.1）: {shot_id}")
            seen.add(shot_id)
            if not get_visual(shot, "description"):
                errors.append(f"镜头 {shot_id or shot_index} 缺少画面描述")
            if not get_prompt(shot, "image_cn"):
                errors.append(f"镜头 {shot_id or shot_index} 缺少中文生图提示词")
            if not get_prompt(shot, "image_en"):
                errors.append(f"镜头 {shot_id or shot_index} 缺少英文生图提示词")
    return errors
