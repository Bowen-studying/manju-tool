"""LangGraph orchestration for the resumable storyboard workflow.

The graph deliberately keeps model calls behind manju's provider-neutral
OpenAI-compatible client. LangGraph owns orchestration and checkpoints only;
credentials are never added to graph state or run artifacts.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from manju.pipeline.storyboard_schema import normalize_storyboard, validate_storyboard
from manju.pipeline.storyboard_stages import (
    STAGE_VERSION,
    _chunk_text,
    _plan_prompts,
    _scene_prompts,
    _scene_source,
)
from manju.utils.ai import call_llm, get_ai_config, parse_json_response
from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json


WORKFLOW_VERSION = "6"
# Backwards-compatible constant for callers of the original PoC module.
AGENT_VERSION = WORKFLOW_VERSION
DEFAULT_MAX_REVISIONS = 2

NAMED_STYLE_REPLACEMENTS = {
    "新海诚式": "细腻电影级光影、通透高饱和天空与雨后反射",
    "新海诚风格": "细腻电影级光影、通透高饱和天空与雨后反射",
    "Makoto Shinkai style": "cinematic luminous skies, detailed atmospheric lighting, rain-washed reflections",
    "makoto shinkai style": "cinematic luminous skies, detailed atmospheric lighting, rain-washed reflections",
}

ANCHOR_STATE_CN = (
    "眼神", "表情", "神情", "目光", "紧握", "手持", "拿着", "握着",
    "信封", "信纸", "影子朝", "欲言又止", "站在", "走向", "回头",
)
ANCHOR_STATE_EN = (
    "alert", "determined", "expression", "holding", "clutching", "carrying",
    "envelope", "letter", "shadow extending", "about to speak", "looking", "gaze",
)
EMPTY_ANCHOR_CLAIMS_CN = ("无其他永久标记", "无明显永久标记", "无特殊永久标记")
EMPTY_ANCHOR_CLAIMS_EN = (
    "no other permanent marks", "no visible permanent marks", "no special permanent marks",
)
BLOCKING_REVIEW_CATEGORIES = {
    "schema", "source_fidelity", "continuity", "shootability", "safety",
    "asset_binding", "visible_entity_consistency",
}


def _replace_named_styles(value):
    """Replace known living-creator style names with observable traits."""
    if isinstance(value, str):
        for named_style, replacement in NAMED_STYLE_REPLACEMENTS.items():
            value = value.replace(named_style, replacement)
        return value
    if isinstance(value, list):
        return [_replace_named_styles(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_named_styles(item) for key, item in value.items()}
    return value


def _canonical_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).lower()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _contains_canonical(container: str, value: str) -> bool:
    needle = _canonical_text(value)
    return bool(needle) and needle in _canonical_text(container)


def _dedupe_anchor_markers(text: str, marker: str) -> str:
    """Keep the authored prefix and the first complete injected anchor suffix.

    Anchor descriptions are structured prose and may legitimately contain
    semicolons. Splitting on punctuation corrupts those descriptions, so the
    marker itself is the only safe boundary.
    """
    value = str(text).strip()
    match = re.search(re.escape(marker), value, flags=re.IGNORECASE)
    if not match:
        return value
    prefix = value[:match.start()].rstrip(" ；;")
    suffix = value[match.start():].strip(" ；;")
    second = re.search(re.escape(marker), suffix[len(marker):], flags=re.IGNORECASE)
    if second:
        suffix = suffix[:len(marker) + second.start()].rstrip(" ；;")
    separator = "; " if marker.lower().startswith("fixed") else "；"
    return separator.join(part for part in (prefix, suffix) if part)


def _strip_anchor_markers(text: str, marker: str) -> str:
    """Remove the whole generated suffix without parsing its punctuation."""
    value = str(text).strip()
    match = re.search(re.escape(marker), value, flags=re.IGNORECASE)
    return value[:match.start()].rstrip(" ；;") if match else value


def _visible_characters(shot: dict, characters: list[dict]) -> list[dict]:
    """Identify characters framed in the still image, excluding prompt anchors."""
    visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
    # Explicit semantic IDs are the source of truth. A hand, silhouette,
    # costume or other body-detail shot still depicts that character; the old
    # object-keyword heuristic incorrectly erased such IDs.
    explicit = visual.get("visible_character_ids")
    if isinstance(explicit, list):
        requested = {str(item).strip().lower() for item in explicit if str(item).strip()}
        return [
            character for character in characters
            if any(str(character.get(key, "")).strip().lower() in requested
                   for key in ("character_id", "name", "name_en"))
        ]
    prompts = shot.get("prompts", {}) if isinstance(shot.get("prompts"), dict) else {}
    image_cn = _strip_anchor_markers(str(prompts.get("image_cn", "")), "角色固定锚点")
    image_en = _strip_anchor_markers(
        str(prompts.get("image_en", "")), "fixed character anchor"
    )
    # Image prompts describe framed subjects more reliably than prose such as
    # "looks toward Alice" or "moves in Bob's direction", which only references
    # an off-camera person. Use composition/action text only when image prompts
    # contain no named subject at all.
    primary = " ".join((image_cn, image_en)).lower()

    def mentioned(character: dict, text: str) -> bool:
        name = str(character.get("name", "")).strip().lower()
        name_en = str(character.get("name_en", "")).strip().lower()
        return bool((name and name in text) or (name_en and name_en in text))

    visible = [character for character in characters if mentioned(character, primary)]
    if visible:
        return visible
    fallback = " ".join((
        str(visual.get("composition", "")),
        str(visual.get("description", "")),
    )).lower()
    return [character for character in characters if mentioned(character, fallback)]


def _requires_character_identity_anchor(shot: dict) -> bool:
    """Whether a framed character needs the complete face/body identity anchor.

    Body-detail shots still keep visible_character_ids for continuity and asset
    selection, but a complete face/body description can make a hand or costume
    insert unshootable. This generic framing check never changes visibility.
    """
    visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
    prompts = shot.get("prompts", {}) if isinstance(shot.get("prompts"), dict) else {}
    shot_type = str(visual.get("shot_type", "")).casefold()
    text = " ".join((
        str(visual.get("description", "")), str(visual.get("composition", "")),
        str(prompts.get("image_cn", "")), str(prompts.get("image_en", "")),
    )).casefold()
    detail = any(token in shot_type for token in (
        "close-up", "close up", "macro", "detail", "\u7279\u5199", "\u5c40\u90e8",
    ))
    body_detail = any(token in text for token in (
        "hand", "finger", "arm", "sleeve", "foot", "shoe", "leg", "costume detail",
        "\u624b", "\u624b\u6307", "\u624b\u81c2", "\u8896", "\u811a", "\u978b", "\u817f", "\u670d\u88c5\u5c40\u90e8",
    ))
    identity_visible = any(token in text for token in (
        "face", "portrait", "head", "eye", "hair", "full body", "half body",
        "\u8138", "\u9762\u90e8", "\u5934", "\u773c", "\u5934\u53d1", "\u5168\u8eab", "\u534a\u8eab",
    ))
    return not (detail and body_detail and not identity_visible)


def _sequential_action_count(shot: dict) -> int:
    visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
    audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
    text = str(visual.get("description", "")).lower()
    clauses = [
        clause.strip()
        for clause in re.split(
            r"[，,；;。.!！?？]+|(?:随后|然后|接着|继而|after that|then)",
            text,
        )
        if clause.strip()
    ]
    action = re.compile(
        r"打开|推开|吹开|吹动|握紧?|拿起?|手持|收紧|"
        r"出现|进入|走出|现身|站到|"
        r"转身|转头|回头|抬头|低头|看向|看见|看到|直视|凝视|"
        r"迈出|走一步|走近|走向|跑向|向前走|"
        r"消失|褪色|破碎|变化|延伸|伸向|"
        r"瞳孔微张|睁大|皱眉|嘴唇微张|欲言又止|定格|颤抖|停住|"
        r"open(?:s|ed)?|swings? open|blow|hold|grip|tighten|"
        r"appear|enter|emerge|stand(?:s|ing)? by|"
        r"turn|look(?:s|ed)?|glance|stare|widen|"
        r"step|walk|run|approach|disappear|fade|shatter|transform|extend|"
        r"lips? part|hesitat|freeze|tremble|stop"
    )
    count = sum(1 for clause in clauses if action.search(clause))
    if str(audio.get("dialogue", "") or audio.get("narration", "")).strip():
        count += 1
    return count


def _clean_plan_anchors(plan):
    """Remove meaningless negative filler from reusable identity anchors."""
    if not isinstance(plan, dict):
        return plan
    bible = plan.get("creative_bible", {})
    characters = bible.get("characters", []) if isinstance(bible, dict) else []
    for character in characters if isinstance(characters, list) else []:
        if not isinstance(character, dict):
            continue
        for key, phrases in (
            ("anchor_description", EMPTY_ANCHOR_CLAIMS_CN),
            ("anchor_description_en", EMPTY_ANCHOR_CLAIMS_EN),
        ):
            value = str(character.get(key, ""))
            for phrase in phrases:
                value = re.sub(
                    rf"\s*[，,；;]?\s*{re.escape(phrase)}",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )
            character[key] = value.strip(" ，,；;")
    return plan


def _anchor_errors(character: dict) -> list[str]:
    name = str(character.get("name", "unnamed"))
    name_en = str(character.get("name_en", "")).strip()
    cn = str(character.get("anchor_description", "")).strip()
    en = str(character.get("anchor_description_en", "")).strip()
    # Age is a source fact, not a universally safe visual-design default.  The
    # planning prompt preserves it when authored, but a story that omits age
    # must not be forced into inventing one merely to pass this shape check.
    groups = {
        "gender": ("woman", "man", "male", "female", "boy", "girl", "男性", "女性", "男", "女"),
        "hair/face": ("hair", "face", "eye", "beard", "发", "脸", "眼", "眉", "胡", "面容"),
        "clothing": ("wear", "coat", "shirt", "dress", "jacket", "trouser", "uniform", "clothing",
                     "穿", "衣", "风衣", "外套", "衬衫", "裙", "裤", "制服"),
    }
    errors: list[str] = []
    if len(cn) < 16:
        errors.append(f"{name}: Chinese visual anchor is too short")
    if len(en) < 20:
        errors.append(f"{name}: English visual anchor is missing or too short")
    if len(name_en) < 2 or re.search(r"[\u4e00-\u9fff]", name_en):
        errors.append(f"{name}: name_en is missing or contains Chinese text")
    if not re.search(r"[\u4e00-\u9fff]", cn):
        errors.append(f"{name}: Chinese visual anchor is not written in Chinese")
    if re.search(r"[\u4e00-\u9fff]", en):
        errors.append(f"{name}: English visual anchor contains Chinese text")
    for label, tokens in groups.items():
        if not any(token in cn.lower() for token in tokens):
            errors.append(f"{name}: Chinese visual anchor lacks {label}")
        if not any(token in en.lower() for token in tokens):
            errors.append(f"{name}: English visual anchor lacks {label}")
    if any(token in cn for token in ANCHOR_STATE_CN):
        errors.append(f"{name}: Chinese visual anchor contains scene-specific state")
    if any(token in en.lower() for token in ANCHOR_STATE_EN):
        errors.append(f"{name}: English visual anchor contains scene-specific state")
    if any(token in cn for token in EMPTY_ANCHOR_CLAIMS_CN):
        errors.append(f"{name}: Chinese visual anchor contains meaningless negative filler")
    if any(token in en.lower() for token in EMPTY_ANCHOR_CLAIMS_EN):
        errors.append(f"{name}: English visual anchor contains meaningless negative filler")
    return errors


def _plan_quality_errors(plan: dict | None, expected_scenes: int) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan is not a JSON object"]
    scenes = [item for item in plan.get("scenes", []) if isinstance(item, dict)]
    errors = []
    if len(scenes) != expected_scenes:
        errors.append(f"expected exactly {expected_scenes} scenes, got {len(scenes)}")
    bible = plan.get("creative_bible")
    if not isinstance(bible, dict) or not str(bible.get("style_anchor", "")).strip():
        errors.append("creative bible lacks style_anchor")
    characters = bible.get("characters", []) if isinstance(bible, dict) else []
    if isinstance(characters, list):
        for character in characters:
            if isinstance(character, dict):
                errors.extend(_anchor_errors(character))
    return errors


def _prepare_generated_scene(scene: dict, bible: dict) -> dict:
    """Normalize prompts and inject each identity anchor at most once."""
    scene = _replace_named_styles(scene)
    characters = bible.get("characters", []) if isinstance(bible, dict) else []
    for shot in scene.get("shots", []):
        if not isinstance(shot, dict):
            continue
        prompts = shot.setdefault("prompts", {})
        if not isinstance(prompts, dict):
            prompts = {}
            shot["prompts"] = prompts
        if not str(prompts.get("video_cn", "")).strip() and str(prompts.get("video", "")).strip():
            prompts["video_cn"] = prompts["video"]
        image_cn = _strip_anchor_markers(str(prompts.get("image_cn", "")), "角色固定锚点")
        image_en = _strip_anchor_markers(
            str(prompts.get("image_en", "")), "fixed character anchor"
        )
        for character in characters if isinstance(characters, list) else []:
            if not isinstance(character, dict):
                continue
            name = str(character.get("name", "")).strip()
            name_en = str(character.get("name_en", "")).strip()
            if name and name_en:
                image_en = image_en.replace(name, name_en)
        prompts["image_cn"] = image_cn
        prompts["image_en"] = image_en

        visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
        if not str(visual.get("color_tone", "")).strip():
            visual["color_tone"] = (
                str(scene.get("visual_mood", "")).strip()
                or str(bible.get("style_anchor", "")).strip()
                or "neutral cinematic color palette"
            )
            shot["visual"] = visual
        visible_characters = _visible_characters(
            shot,
            [item for item in characters if isinstance(item, dict)],
        )
        visual["visible_character_ids"] = [
            str(character.get("character_id") or character.get("name") or character.get("name_en"))
            for character in visible_characters
        ]
        shot["visual"] = visual
        if _requires_character_identity_anchor(shot):
            for character in visible_characters:
                name = str(character.get("name", "")).strip()
                name_en = str(character.get("name_en", "")).strip() or "character"
                anchor_cn = str(character.get("anchor_description", "")).strip()
                anchor_en = str(character.get("anchor_description_en", "")).strip()
                image_cn = str(prompts.get("image_cn", "")).strip()
                image_en = str(prompts.get("image_en", "")).strip()
                if anchor_cn and not _contains_canonical(image_cn, anchor_cn):
                    prompts["image_cn"] = f"{image_cn}；角色固定锚点：{name}，{anchor_cn}".strip("；")
                if anchor_en and not _contains_canonical(image_en, anchor_en):
                    prompts["image_en"] = (
                        f"{image_en}; fixed character anchor for {name_en}: {anchor_en}"
                    ).strip("; ")
        # The prompts were stripped before injection, therefore each visible
        # character now has exactly one intact marker clause. Do not split the
        # newly injected anchor prose on semicolons.
        duration = float(shot.get("duration_seconds", 0) or 0)
        action_load = _sequential_action_count(shot)
        minimum_duration = action_load + 1 if action_load >= 3 else 0
        if 0 < duration < minimum_duration:
            shot["duration_seconds"] = float(minimum_duration)
    return scene


def _quality_issue(
    code: str,
    scene_id: str,
    shot_id: str,
    problem: str,
    instruction: str,
    severity: str = "high",
) -> dict:
    return {
        "code": code,
        "scene_id": scene_id,
        "shot_ids": [shot_id] if shot_id else [],
        "severity": severity,
        "problem": problem,
        "instruction": instruction,
    }


def _explicit_ending_target(source_text: str) -> str:
    cn_matches = list(re.finditer(
        r"(?:画面|镜头)(?:最终|最后)?(?:停在|定格在|落在|停留在)([^。！？\n]{2,60})",
        str(source_text),
    ))
    if cn_matches:
        return cn_matches[-1].group(1).strip()
    en_matches = list(re.finditer(
        r"(?:the\s+)?(?:final\s+)?(?:shot|image|frame|scene)\s+"
        r"(?:ends?|freezes?|holds?|lands?|stops?)\s+(?:on|with)\s+([^.!?\n]{2,80})",
        str(source_text),
        flags=re.IGNORECASE,
    ))
    return en_matches[-1].group(1).strip() if en_matches else ""


def _ending_fidelity_issues(storyboard: dict, source_text: str) -> list[dict]:
    """Protect an explicitly authored final visual beat from reframing or omission."""
    target = _explicit_ending_target(source_text)
    if not target:
        return []
    scenes = [scene for scene in storyboard.get("scenes", []) if isinstance(scene, dict)]
    if not scenes:
        return []
    final_scene = scenes[-1]
    shots = [shot for shot in final_scene.get("shots", []) if isinstance(shot, dict)]
    if not shots:
        return []
    final_shot = shots[-1]
    bible = storyboard.get("creative_bible", {})
    characters = bible.get("characters", []) if isinstance(bible, dict) else []
    target_characters = []
    for character in characters if isinstance(characters, list) else []:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name", "")).strip()
        name_en = str(character.get("name_en", "")).strip()
        if (name and name in target) or (name_en and name_en.lower() in target.lower()):
            target_characters.append(character)
    if not target_characters:
        return []

    visual = final_shot.get("visual", {}) if isinstance(final_shot.get("visual"), dict) else {}
    composition = str(visual.get("composition", ""))
    description = str(visual.get("description", ""))
    movement = str(visual.get("camera_movement", ""))
    shot_type = str(visual.get("shot_type", "")).lower()
    final_text = " ".join((composition, description, movement)).lower()
    all_characters = [item for item in characters if isinstance(item, dict)]
    problems: list[str] = []
    for character in target_characters:
        name = str(character.get("name", "")).strip()
        name_en = str(character.get("name_en", "")).strip()
        aliases = [item for item in (name, name_en) if item]
        if not any(alias.lower() in final_text for alias in aliases):
            problems.append(f"ending subject {name or name_en} is absent from the final shot")
            continue
        secondary_framing = bool(name) and (
            re.search(
                rf"{re.escape(name)}(?:位于|在|处于).{{0,12}}(?:边缘|背景)",
                composition,
            )
            or re.search(rf"{re.escape(name)}.{{0,8}}(?:模糊|剪影)", composition)
        )
        if not secondary_framing and name_en:
            secondary_framing = bool(
                re.search(
                    rf"{re.escape(name_en)}.{{0,18}}(?:frame edge|edge of (?:the )?frame|background)",
                    composition,
                    flags=re.IGNORECASE,
                )
                or re.search(
                    rf"{re.escape(name_en)}.{{0,10}}(?:blurred|silhouette)",
                    composition,
                    flags=re.IGNORECASE,
                )
            )
        if secondary_framing:
            problems.append(f"ending subject {name} is framed as a secondary/background figure")
        focus_text = movement.lower()
        if ("聚焦" in movement or "focus" in focus_text) and not any(
            alias.lower() in focus_text for alias in aliases
        ):
            other_named = any(
                str(other.get("name", "")).strip()
                and str(other.get("name", "")).strip() in movement
                for other in all_characters
                if other is not character
            )
            if other_named:
                problems.append(f"camera focus is assigned to another character instead of {name or name_en}")
    expression_target = any(token in target.lower() for token in (
        "表情", "面部", "脸", "眼神", "神情", "expression", "face", "eyes",
    ))
    if expression_target and not any(token in shot_type for token in (
        "特写", "近景", "close-up", "close up", "close shot",
    )):
        problems.append("the authored ending is an expression beat but the final framing is not close enough")
    if not problems:
        return []
    scene_id = str(final_scene.get("scene_id", ""))
    shot_id = str(final_shot.get("shot_id", ""))
    return [{**_quality_issue(
        "ending_fidelity",
        scene_id,
        shot_id,
        "Explicit source ending is not the visual focus of the final shot: " + "; ".join(problems),
        f"Reframe or add a final shot that clearly lands on: {target}",
    ), "category": "source_fidelity"}]


def _deterministic_quality_issues(storyboard: dict, source_text: str = "") -> list[dict]:
    """Catch objective defects before asking the model to review itself."""
    issues: list[dict] = []
    bible = storyboard.get("creative_bible", {})
    characters = bible.get("characters", []) if isinstance(bible, dict) else []
    # Match explicit camera movement, not semantic focus changes such as
    # "焦点从甲转移到乙". Single-character terms (especially "移") caused
    # false camera conflicts in otherwise valid locked-off rack-focus shots.
    moving_terms = (
        "推镜", "拉镜", "摇镜", "移镜", "跟镜", "跟拍", "环绕镜头",
        "升镜", "降镜", "镜头推进", "镜头拉远", "镜头平移", "摄像机移动",
        "zoom in", "zoom out", "pan left", "pan right", "tilt up", "tilt down",
        "dolly", "tracking shot", "camera moves", "camera movement",
    )
    medium_or_wide = ("中景", "全景", "远景", "medium shot", "wide shot", "long shot")
    named_styles = tuple(NAMED_STYLE_REPLACEMENTS)

    for scene in storyboard.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("scene_id", ""))
        for shot in scene.get("shots", []):
            if not isinstance(shot, dict):
                continue
            shot_id = str(shot.get("shot_id", ""))
            visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
            audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
            prompts = shot.get("prompts", {}) if isinstance(shot.get("prompts"), dict) else {}
            movement = str(visual.get("camera_movement", "")).lower()
            shot_type = str(visual.get("shot_type", "")).lower()
            description = str(visual.get("description", ""))
            dialogue = str(audio.get("dialogue", "") or audio.get("narration", "")).strip()
            duration = float(shot.get("duration_seconds", 0) or 0)

            if ("固定" in movement or "static" in movement) and any(
                term in movement for term in moving_terms
            ):
                issues.append(_quality_issue(
                    "camera_conflict", scene_id, shot_id,
                    f"Camera instruction conflicts: {movement}",
                    "Choose either a locked-off camera or one explicit camera movement.",
                ))

            action_load = _sequential_action_count(shot)
            minimum_duration = action_load + 1 if action_load >= 3 else 0
            if 0 < duration < minimum_duration:
                issues.append(_quality_issue(
                    "action_density", scene_id, shot_id,
                    f"{duration:g}s shot contains {action_load} sequential action/dialogue beats.",
                    f"Extend duration to at least {minimum_duration}s, split the shot, or simplify the sequence.",
                    severity="medium",
                ))

            spatial_blob = " ".join((
                str(visual.get("composition", "")),
                description,
                str(prompts.get("image_cn", "")),
                str(prompts.get("image_en", "")),
                str(prompts.get("video_cn", "")),
                str(prompts.get("video_en", "")),
            )).lower()
            face_closeup = any(token in shot_type for token in (
                "特写", "close-up", "close up",
            )) and any(token in spatial_blob for token in (
                "面部", "脸", "眼神", "face", "eyes", "gaze",
            ))
            tight_face_frame = any(token in spatial_blob for token in (
                "面部占满画面", "脸占满画面", "背景完全虚化",
                "face fills the frame", "background completely blurred",
            ))
            full_shadow_action = bool(re.search(
                r"影子.{0,12}(?:延伸|伸向|反方向)|"
                r"shadow.{0,24}(?:extend|opposite direction|full-body)",
                spatial_blob,
            ))
            if face_closeup and tight_face_frame and full_shadow_action:
                issues.append(_quality_issue(
                    "closeup_shadow_conflict", scene_id, shot_id,
                    "A tight face close-up with a fully blurred background cannot also clearly show the character's extending full-body shadow.",
                    "Remove the full-body shadow from this close-up, or widen/recompose the shot so the projected shadow is visibly framed.",
                ))

            if ("表情" in movement or "expression" in movement) and any(
                term in shot_type for term in medium_or_wide
            ):
                issues.append(_quality_issue(
                    "framing_mismatch", scene_id, shot_id,
                    "The camera is meant to land on a facial expression but the framing is too wide.",
                    "Use a close-up/close shot for the expression, or change the camera endpoint.",
                    severity="medium",
                ))

            if not str(prompts.get("video_cn", "")).strip() or not str(
                prompts.get("video_en", "")
            ).strip():
                issues.append(_quality_issue(
                    "missing_video_prompt", scene_id, shot_id,
                    "Bilingual video prompts are incomplete.",
                    "Provide both video_cn and video_en with shootable motion instructions.",
                    severity="medium",
                ))
            else:
                if not re.search(r"[\u4e00-\u9fff]", str(prompts.get("video_cn", ""))):
                    issues.append(_quality_issue(
                        "video_language_mismatch", scene_id, shot_id,
                        "video_cn is not written in Chinese.",
                        "Write a Chinese motion prompt in video_cn.",
                        severity="medium",
                    ))

            if str(prompts.get("image_en", "")).strip() and re.search(
                r"[\u4e00-\u9fff]", str(prompts.get("image_en", ""))
            ):
                issues.append(_quality_issue(
                    "image_language_mismatch", scene_id, shot_id,
                    "image_en contains Chinese text.",
                    "Use name_en and English-only terminology in image_en.",
                    severity="medium",
                ))
                if re.search(r"[\u4e00-\u9fff]", str(prompts.get("video_en", ""))):
                    issues.append(_quality_issue(
                        "video_language_mismatch", scene_id, shot_id,
                        "video_en contains Chinese text.",
                        "Write an English motion prompt in video_en.",
                        severity="medium",
                    ))

            prompt_blob = " ".join(str(value) for value in prompts.values())
            if any(named.lower() in prompt_blob.lower() for named in named_styles):
                issues.append(_quality_issue(
                    "named_creator_style", scene_id, shot_id,
                    "Prompt names a living creator as the requested style.",
                    "Replace the creator name with observable lighting, color, material, and camera traits.",
                    severity="medium",
                ))

            visible_characters = _visible_characters(
                shot,
                [item for item in characters if isinstance(item, dict)],
            )
            if not _requires_character_identity_anchor(shot):
                continue
            for character in visible_characters:
                name = str(character.get("name", "")).strip()
                anchor_cn = str(character.get("anchor_description", "")).strip()
                anchor_en = str(character.get("anchor_description_en", "")).strip()
                if not _contains_canonical(str(prompts.get("image_cn", "")), anchor_cn) or not (
                    _contains_canonical(str(prompts.get("image_en", "")), anchor_en)
                ):
                    issues.append(_quality_issue(
                        "missing_character_anchor", scene_id, shot_id,
                        f"Visible character {name} does not carry both fixed visual anchors.",
                        "Include the complete Chinese and English fixed anchors in the image prompts.",
                    ))
    issues.extend(_ending_fidelity_issues(storyboard, source_text))
    return issues


class StoryboardAgentState(TypedDict, total=False):
    """Serializable state persisted after every LangGraph node."""

    input_text: str
    title: str
    word_count: int
    scene_count: int
    stage_dir: str
    run_dir: str
    run_id: str
    model_name: str
    chunks: list[str]
    summaries: list[str]
    plan: dict
    scene_index: int
    completed_scenes: list[dict]
    storyboard: dict
    validation_errors: list[str]
    review: dict
    revision_count: int
    revision_targets: list[str]
    revision_cursor: int
    max_revisions: int
    status: str


def _call_json(
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
    raw_path: str,
) -> dict | None:
    """Call the configured LLM and repair malformed JSON once."""
    response = call_llm(
        system,
        user,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    parsed = parse_json_response(response) if response else None
    if isinstance(parsed, dict):
        return parsed
    if not response:
        return None

    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as handle:
        handle.write(response)
    repaired = call_llm(
        "You repair JSON. Return legal JSON only and preserve the original meaning.",
        f"Repair this output:\n{response[:30000]}",
        max_tokens=max_tokens,
        temperature=0,
    )
    parsed = parse_json_response(repaired) if repaired else None
    if isinstance(parsed, dict):
        return parsed
    if repaired:
        with open(raw_path.replace("_raw", "_repair_raw"), "w", encoding="utf-8") as handle:
            handle.write(repaired)
    return None


def _prepare_node(state: StoryboardAgentState) -> dict:
    chunks = _chunk_text(state["input_text"])
    return {
        "chunks": chunks,
        "summaries": [],
        "scene_index": 0,
        "completed_scenes": [],
        "revision_count": 0,
        "revision_targets": [],
        "revision_cursor": 0,
        "status": "running",
    }


def _summarize_node(state: StoryboardAgentState) -> dict:
    chunks = state["chunks"]
    if len(chunks) == 1:
        return {"summaries": chunks}

    summaries: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        path = os.path.join(state["run_dir"], f"00_summary_{index:03d}.json")
        fingerprint = content_fingerprint(chunk, "agent-summary-v1")
        cached = read_json(path)
        if cached and cached.get("fingerprint") == fingerprint and cached.get("summary"):
            summaries.append(str(cached["summary"]))
            continue
        result = _call_json(
            "Summarize the story chunk. Preserve characters, locations, chronology, "
            "causality, foreshadowing, and ending information. Return JSON only.",
            f'Return {{"summary":"a complete concise summary"}}. '
            f"Chunk {index}/{len(chunks)}:\n{chunk}",
            max_tokens=1800,
            temperature=0.2,
            raw_path=os.path.join(state["run_dir"], f"00_summary_{index:03d}_raw.txt"),
        )
        summary = result.get("summary") if isinstance(result, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError(f"story chunk {index} summary failed")
        atomic_write_json(path, {"fingerprint": fingerprint, "summary": summary.strip()})
        summaries.append(summary.strip())
    return {"summaries": summaries}


def _plan_node(state: StoryboardAgentState) -> dict:
    summaries = state["summaries"]
    chunks = state["chunks"]
    plan_input = state["input_text"] if len(chunks) == 1 else "\n\n".join(
        f"[Chunk {index}] {summary}" for index, summary in enumerate(summaries, 1)
    )
    system, user = _plan_prompts(
        plan_input,
        state["title"],
        state["word_count"],
        state["scene_count"],
        len(chunks),
    )
    plan = _call_json(
        system,
        user,
        max_tokens=5000,
        temperature=0.3,
        raw_path=os.path.join(state["run_dir"], "01_plan_raw.txt"),
    )
    plan = _clean_plan_anchors(_replace_named_styles(plan))
    plan_errors = _plan_quality_errors(plan, state["scene_count"])
    if plan_errors:
        plan = _call_json(
            system,
            user
            + "\nCorrect every planning defect below. Return the complete corrected plan, not a patch:\n- "
            + "\n- ".join(plan_errors),
            max_tokens=5000,
            temperature=0.2,
            raw_path=os.path.join(state["run_dir"], "01_plan_retry_raw.txt"),
        )
        plan = _clean_plan_anchors(_replace_named_styles(plan))
        plan_errors = _plan_quality_errors(plan, state["scene_count"])
    if not plan or plan_errors:
        raise RuntimeError("scene plan generation failed: " + "; ".join(plan_errors))
    plan.setdefault("title", state["title"])
    plan.setdefault("creative_bible", {})
    atomic_write_json(os.path.join(state["run_dir"], "01_plan.json"), plan)
    return {"plan": plan, "scene_index": 0, "completed_scenes": []}


def _generate_scene_node(state: StoryboardAgentState) -> dict:
    index = state["scene_index"]
    scenes = state["plan"]["scenes"]
    if index >= len(scenes):
        return {}
    scene = dict(scenes[index])
    scene_id = str(scene.get("scene_id") or index + 1)
    scene["scene_id"] = scene_id
    source = _scene_source(scene, state["chunks"])
    system, user = _scene_prompts(
        source,
        state["title"],
        state["plan"]["creative_bible"],
        scene,
    )
    result = _call_json(
        system,
        user,
        max_tokens=7000,
        temperature=0.35,
        raw_path=os.path.join(state["run_dir"], f"02_scene_{index + 1:03d}_raw.txt"),
    )
    shots = result.get("shots") if isinstance(result, dict) else None
    if not isinstance(shots, list) or not shots:
        raise RuntimeError(f"scene {scene_id} shot generation failed")
    generated = _prepare_generated_scene(
        {**scene, "shots": shots},
        state["plan"]["creative_bible"],
    )
    atomic_write_json(
        os.path.join(state["run_dir"], f"02_scene_{index + 1:03d}.json"),
        generated,
    )
    return {
        "completed_scenes": [*state.get("completed_scenes", []), generated],
        "scene_index": index + 1,
    }


def _scene_route(state: StoryboardAgentState) -> Literal["more", "assemble"]:
    return "more" if state["scene_index"] < len(state["plan"]["scenes"]) else "assemble"


def _assemble_node(state: StoryboardAgentState) -> dict:
    storyboard = normalize_storyboard({
        "title": state["plan"].get("title", state["title"]),
        "creative_bible": state["plan"].get("creative_bible", {}),
        "scenes": state["completed_scenes"],
    }, title=state["title"])
    errors = validate_storyboard(storyboard)
    atomic_write_json(os.path.join(state["run_dir"], "03_storyboard.json"), storyboard)
    return {"storyboard": storyboard, "validation_errors": errors}


def _review_node(state: StoryboardAgentState) -> dict:
    errors = state.get("validation_errors", [])
    deterministic_issues = _deterministic_quality_issues(
        state.get("storyboard", {}),
        state.get("input_text", ""),
    )
    review_index = state.get("revision_count", 0)
    if errors or deterministic_issues:
        schema_issues = [
            {
                "code": "schema_validation",
                "category": "schema",
                "blocking": True,
                "severity": "high",
                "problem": error,
                "instruction": "Repair this structural issue.",
            }
            for error in errors
        ]
        deterministic_issues = [
            {**issue, "category": issue.get("category", "shootability"), "blocking": True}
            for issue in deterministic_issues
        ]
        combined_issues = [*schema_issues, *deterministic_issues]
        targets = sorted({
            str(issue.get("scene_id"))
            for issue in deterministic_issues
            if str(issue.get("scene_id", "")).strip()
        }) or [str(scene.get("scene_id")) for scene in state.get("completed_scenes", [])]
        review = {
            "decision": "revise",
            "summary": "Deterministic quality gates found objective storyboard defects.",
            "issues": combined_issues,
            "blocking_issues": combined_issues,
            "advisory_issues": [],
            "target_scene_ids": targets,
            "deterministic": True,
        }
    else:
        system = (
            "You are an independent storyboard quality reviewer. Evaluate text-level production "
            "readiness only; do not claim generated-image consistency because no media exists yet. "
            "Check continuity, shootability, action density, dialogue delivery, "
            "shot duration, and framing. Preserve the source timeline. Distinguish blocking production "
            "defects from subjective art-direction suggestions. A preference for seeing a face, adding a "
            "reaction shot, or varying camera movement is advisory and never blocking by itself. "
            "Fixed-anchor coverage was already validated deterministically. Do not request anchors for "
            "off-camera, background-only, or future characters, and do not treat anchor opinions as blocking. "
            "Compare the storyboard against the provided source, including its explicitly authored final image. "
            "A source motif only needs to appear at the narrative beat where it is introduced; do not require it "
            "to be repeated in every later shot or close-up. A dedicated close-up is a valid way to realize an "
            "explicit source ending on a character's face or expression and is not an unsupported addition. "
            "If proposing new shots, use numeric dot IDs such as 1.2.1 (never 1.2b) and duration >= 1 second. "
            "Return JSON only."
        )
        review_source = state.get("input_text", "")
        if len(review_source) > 20000:
            review_source = "\n\n".join(state.get("summaries", []))
        review_source = review_source[:24000]
        storyboard_json = json.dumps(state["storyboard"], ensure_ascii=False)
        storyboard_budget = max(12000, 60000 - len(review_source))
        user = (
            "Return this shape: "
            '{"decision":"accept|revise","summary":"...","issues":['
            '{"scene_id":"1","shot_ids":["1.1"],"severity":"low|medium|high",'
            '"category":"schema|source_fidelity|continuity|shootability|safety|advisory_art_direction",'
            '"blocking":false,"problem":"...","instruction":"..."}],"target_scene_ids":["1"]}. '
            "Set blocking=true only for a high-severity objective defect in an allowed blocking category. "
            "Medium issues and artistic preferences remain advisory.\nSource:\n"
            + review_source
            + "\nStoryboard:\n"
            + storyboard_json[:storyboard_budget]
        )
        result = _call_json(
            system,
            user,
            max_tokens=3000,
            temperature=0.1,
            raw_path=os.path.join(state["run_dir"], f"04_review_{review_index:02d}_raw.txt"),
        )
        if not isinstance(result, dict):
            review = {
                "decision": "accept",
                "summary": "Automated quality review was unavailable; structural validation passed.",
                "issues": [],
                "blocking_issues": [],
                "advisory_issues": [],
                "target_scene_ids": [],
                "fallback": True,
            }
        else:
            normalized_issues: list[dict] = []
            blocking_issues: list[dict] = []
            advisory_issues: list[dict] = []
            raw_issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
            ending_target = _explicit_ending_target(state.get("input_text", ""))
            for raw_issue in raw_issues:
                if not isinstance(raw_issue, dict):
                    continue
                severity = str(raw_issue.get("severity", "medium")).lower()
                category = str(raw_issue.get("category", "advisory_art_direction")).lower()
                issue_text = " ".join((
                    str(raw_issue.get("problem", "")),
                    str(raw_issue.get("instruction", "")),
                )).lower()
                shot_ids = raw_issue.get("shot_ids", [])
                shot_ids = shot_ids if isinstance(shot_ids, list) else []
                repeat_motif_opinion = (
                    category == "source_fidelity"
                    and len(shot_ids) > 1
                    and any(token in issue_text for token in (
                        "所有镜头", "每个镜头", "均需", "全部加入", "一致体现",
                        "every shot", "all shots", "each shot", "all later shots",
                        "consistently in", "repeat in",
                    ))
                )
                ending_closeup_opinion = (
                    bool(ending_target)
                    and any(token in ending_target.lower() for token in (
                        "表情", "面部", "脸", "眼神", "expression", "face", "eyes",
                    ))
                    and any(token in issue_text for token in (
                        "特写", "close-up", "close up",
                    ))
                    and any(token in issue_text for token in (
                        "not explicitly", "not required", "unsupported addition",
                        "未明确要求", "没有明确要求", "并非必须", "新增",
                    ))
                )
                if repeat_motif_opinion or ending_closeup_opinion:
                    continue
                anchor_opinion = "锚点" in issue_text or "anchor" in issue_text
                if anchor_opinion:
                    category = "advisory_art_direction"
                    if severity == "high":
                        severity = "medium"
                requested_blocking = bool(raw_issue.get(
                    "blocking", severity == "high" and category in BLOCKING_REVIEW_CATEGORIES
                ))
                blocking = (
                    requested_blocking
                    and severity == "high"
                    and category in BLOCKING_REVIEW_CATEGORIES
                    and not anchor_opinion
                )
                issue = {
                    **raw_issue,
                    "severity": severity,
                    "category": category,
                    "blocking": blocking,
                }
                normalized_issues.append(issue)
                (blocking_issues if blocking else advisory_issues).append(issue)
            targets = sorted({
                str(issue.get("scene_id"))
                for issue in blocking_issues
                if str(issue.get("scene_id", "")).strip()
            })
            summary = str(result.get("summary", ""))
            if not normalized_issues:
                summary = "Automated review found no blocking or advisory issues."
            elif not blocking_issues and str(result.get("decision", "")).lower() == "revise":
                summary = "Automated review found no blocking defects; advisory issues remain."
            review = {
                "decision": "revise" if blocking_issues else "accept",
                "summary": summary,
                "issues": normalized_issues,
                "blocking_issues": blocking_issues,
                "advisory_issues": advisory_issues,
                "target_scene_ids": targets,
            }
    review["revision_round"] = review_index
    atomic_write_json(
        os.path.join(state["run_dir"], f"04_review_{review_index:02d}.json"),
        review,
    )
    return {"review": review}


def _review_route(state: StoryboardAgentState) -> Literal["revise", "finalize"]:
    wants_revision = state.get("review", {}).get("decision") == "revise"
    return (
        "revise"
        if wants_revision and state.get("revision_count", 0) < state["max_revisions"]
        else "finalize"
    )


def _prepare_revision_node(state: StoryboardAgentState) -> dict:
    available = [str(scene.get("scene_id")) for scene in state["completed_scenes"]]
    requested = [
        str(item) for item in state.get("review", {}).get("target_scene_ids", [])
        if str(item) in available
    ]
    targets = requested or available
    return {
        "revision_count": state.get("revision_count", 0) + 1,
        "revision_targets": targets,
        "revision_cursor": 0,
    }


def _revise_scene_node(state: StoryboardAgentState) -> dict:
    cursor = state["revision_cursor"]
    targets = state["revision_targets"]
    if cursor >= len(targets):
        return {}
    scene_id = targets[cursor]
    scenes = [dict(scene) for scene in state["completed_scenes"]]
    scene_index = next(
        (index for index, scene in enumerate(scenes) if str(scene.get("scene_id")) == scene_id),
        None,
    )
    if scene_index is None:
        return {"revision_cursor": cursor + 1}
    scene = scenes[scene_index]
    relevant_issues = [
        issue for issue in state.get("review", {}).get("blocking_issues", [])
        if isinstance(issue, dict) and str(issue.get("scene_id", scene_id)) == scene_id
    ]
    source = _scene_source(scene, state["chunks"])
    system = (
        "You revise one storyboard scene. Preserve story facts and the creative bible, fix the review "
        "issues, and return JSON only as {\"shots\":[...]}. Each shot must keep the v2 shape. "
        "Visible characters must repeat the complete Chinese and English fixed anchors in image_cn "
        "and image_en. Provide non-empty video, video_cn, and video_en. Do not name living creators "
        "as a style. Keep short shots simple, avoid contradictory camera instructions, and use a "
        "close-up when the camera lands on a facial expression. Preserve the original source timeline. "
        "New shot IDs must use numeric dot notation such as 1.2.1, never letter suffixes, and every "
        "shot duration must be at least 1 second. Address blocking issues only; do not expand scenes "
        "to satisfy advisory art-direction preferences."
    )
    user = json.dumps({
        "creative_bible": state["plan"].get("creative_bible", {}),
        "scene": scene,
        "review_issues": relevant_issues,
        "source": source,
    }, ensure_ascii=False)[:60000]
    result = _call_json(
        system,
        user,
        max_tokens=7000,
        temperature=0.2,
        raw_path=os.path.join(
            state["run_dir"],
            f"05_revision_{state['revision_count']:02d}_scene_{scene_index + 1:03d}_raw.txt",
        ),
    )
    shots = result.get("shots") if isinstance(result, dict) else None
    if not isinstance(shots, list) or not shots:
        raise RuntimeError(f"scene {scene_id} revision failed")
    scenes[scene_index] = _prepare_generated_scene(
        {**scene, "shots": shots},
        state["plan"].get("creative_bible", {}),
    )
    atomic_write_json(
        os.path.join(
            state["run_dir"],
            f"05_revision_{state['revision_count']:02d}_scene_{scene_index + 1:03d}.json",
        ),
        scenes[scene_index],
    )
    return {"completed_scenes": scenes, "revision_cursor": cursor + 1}


def _revision_route(state: StoryboardAgentState) -> Literal["more", "assemble"]:
    return "more" if state["revision_cursor"] < len(state["revision_targets"]) else "assemble"


def _finalize_node(state: StoryboardAgentState) -> dict:
    accepted = state.get("review", {}).get("decision") == "accept"
    return {"status": "completed" if accepted else "needs_review"}


def build_storyboard_graph(checkpointer: SqliteSaver):
    """Build the graph separately so tests can inspect and invoke it."""
    graph = StateGraph(StoryboardAgentState)
    graph.add_node("prepare", _prepare_node)
    graph.add_node("summarize", _summarize_node)
    graph.add_node("plan", _plan_node)
    graph.add_node("generate_scene", _generate_scene_node)
    graph.add_node("assemble", _assemble_node)
    graph.add_node("review", _review_node)
    graph.add_node("prepare_revision", _prepare_revision_node)
    graph.add_node("revise_scene", _revise_scene_node)
    graph.add_node("finalize", _finalize_node)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "summarize")
    graph.add_edge("summarize", "plan")
    graph.add_edge("plan", "generate_scene")
    graph.add_conditional_edges(
        "generate_scene", _scene_route, {"more": "generate_scene", "assemble": "assemble"}
    )
    graph.add_edge("assemble", "review")
    graph.add_conditional_edges(
        "review", _review_route, {"revise": "prepare_revision", "finalize": "finalize"}
    )
    graph.add_edge("prepare_revision", "revise_scene")
    graph.add_conditional_edges(
        "revise_scene", _revision_route, {"more": "revise_scene", "assemble": "assemble"}
    )
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _run_manifest(state: StoryboardAgentState, checkpoint_path: str) -> dict:
    return {
        "engine": "workflow",
        "agent_version": AGENT_VERSION,
        "langgraph_engine": True,
        "run_id": state["run_id"],
        "title": state["title"],
        "scene_count": state["scene_count"],
        "model": state.get("model_name", ""),
        "status": state.get("status", "unknown"),
        "revision_count": state.get("revision_count", 0),
        "review_decision": state.get("review", {}).get("decision", ""),
        "checkpoint": os.path.relpath(checkpoint_path, os.path.dirname(state["stage_dir"])),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def generate_storyboard_workflow(
    text: str,
    title: str,
    word_count: int,
    scene_count: int,
    stage_dir: str,
    output_dir: str,
    *,
    resume: bool = True,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
) -> dict | None:
    """Run or resume the frozen v6 fixed LangGraph storyboard workflow."""
    os.makedirs(stage_dir, exist_ok=True)
    _, configured_model, _ = get_ai_config()
    model_name = configured_model or "unconfigured"
    fingerprint = content_fingerprint(
        text,
        title,
        scene_count,
        STAGE_VERSION,
        AGENT_VERSION,
        max_revisions,
        model_name,
    )
    stable_run_id = f"storyboard-{fingerprint}"
    run_id = stable_run_id if resume else (
        stable_run_id + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    )
    run_dir = os.path.join(stage_dir, f"run_{fingerprint}")
    if not resume:
        run_dir += "_" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    os.makedirs(run_dir, exist_ok=True)
    checkpoint_path = os.path.join(stage_dir, "checkpoints.sqlite")
    storyboard_path = os.path.join(run_dir, "03_storyboard.json")
    manifest_path = os.path.join(output_dir, "agent_run.json")
    review_path = os.path.join(output_dir, "review.json")

    existing_manifest = read_json(manifest_path) if resume else None
    if (
        existing_manifest
        and existing_manifest.get("run_id") == run_id
        and existing_manifest.get("status") in {"completed", "needs_review"}
        and os.path.isfile(storyboard_path)
    ):
        return read_json(storyboard_path)

    initial: StoryboardAgentState = {
        "input_text": text,
        "title": title,
        "word_count": word_count,
        "scene_count": scene_count,
        "stage_dir": stage_dir,
        "run_dir": run_dir,
        "run_id": run_id,
        "model_name": model_name,
        "max_revisions": max(0, max_revisions),
        "status": "running",
    }
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    graph = build_storyboard_graph(saver)
    config = {"configurable": {"thread_id": run_id}}
    try:
        prior = saver.get(config) if resume else None
        result = graph.invoke(None if prior else initial, config=config)
        if not isinstance(result, dict) or not isinstance(result.get("storyboard"), dict):
            raise RuntimeError("LangGraph run did not produce a storyboard")
        final_state: StoryboardAgentState = result
        atomic_write_json(storyboard_path, final_state["storyboard"])
        atomic_write_json(review_path, final_state.get("review", {}))
        atomic_write_json(manifest_path, _run_manifest(final_state, checkpoint_path))
        atomic_write_json(os.path.join(stage_dir, "latest.json"), {
            "run_dir": os.path.relpath(run_dir, stage_dir),
            "run_id": run_id,
            "status": final_state.get("status", "unknown"),
        })
        return final_state["storyboard"]
    except Exception as exc:
        failed = {**initial, "status": "failed"}
        manifest = _run_manifest(failed, checkpoint_path)
        manifest["error_type"] = type(exc).__name__
        atomic_write_json(manifest_path, manifest)
        print(f"Agent workflow failed: {exc}", file=sys.stderr)
        return None
    finally:
        connection.close()


# Compatibility alias: before the supervisor Agent was introduced this module's
# fixed graph was published locally under this name. New CLI dispatch does not use
# the alias; it is kept for existing Python integrations and the v6 regression suite.
generate_storyboard_agent = generate_storyboard_workflow
