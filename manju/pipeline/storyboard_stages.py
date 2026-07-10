"""Resumable multi-stage LLM generation for storyboard schema v2."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from manju.pipeline.storyboard_schema import normalize_storyboard, validate_storyboard
from manju.utils.ai import call_llm, parse_json_response
from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json

STAGE_VERSION = "2"
CHUNK_CHARS = 40_000


def _chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(len(text), cursor + limit)
        if end < len(text):
            boundary = max(text.rfind("\n\n", cursor, end), text.rfind("。", cursor, end))
            if boundary > cursor + limit // 2:
                end = boundary + 1
        chunks.append(text[cursor:end])
        cursor = end
    return chunks


def _call_json(system: str, user: str, max_tokens: int, temperature: float,
               raw_path: str) -> dict | None:
    response = call_llm(system, user, max_tokens=max_tokens, temperature=temperature)
    parsed = parse_json_response(response) if response else None
    if isinstance(parsed, dict):
        return parsed
    if response:
        with open(raw_path, "w", encoding="utf-8") as handle:
            handle.write(response)
        repair_system = "你是JSON修复器。只返回合法JSON，不添加解释，不改变原始语义。"
        repair_user = f"请修复以下输出：\n{response[:30000]}"
        repaired = call_llm(repair_system, repair_user, max_tokens=max_tokens, temperature=0)
        parsed = parse_json_response(repaired) if repaired else None
        if isinstance(parsed, dict):
            return parsed
        if repaired:
            with open(raw_path.replace("_raw", "_repair_raw"), "w", encoding="utf-8") as handle:
                handle.write(repaired)
    return None


def _summarize_chunks(chunks: list[str], run_dir: str, resume: bool) -> list[str] | None:
    if len(chunks) == 1:
        return chunks
    summaries: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        path = os.path.join(run_dir, f"00_summary_{index:03d}.json")
        fingerprint = content_fingerprint(chunk, "summary-v1")
        cached = read_json(path) if resume else None
        if cached and cached.get("fingerprint") == fingerprint and cached.get("summary"):
            summaries.append(str(cached["summary"]))
            continue
        result = _call_json(
            "你是故事编辑。概括指定原文分块，保留人物、地点、时间、事件因果、伏笔和结局信息。只输出JSON。",
            f'输出 {{"summary":"不超过1200字的完整摘要"}}。\n\n分块 {index}/{len(chunks)}：\n{chunk}',
            1800, 0.2, os.path.join(run_dir, f"00_summary_{index:03d}_raw.txt"),
        )
        summary = result.get("summary") if isinstance(result, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            print(f"❌ 原文第 {index} 块摘要失败", file=sys.stderr)
            return None
        atomic_write_json(path, {"fingerprint": fingerprint, "summary": summary.strip()})
        summaries.append(summary.strip())
    return summaries


def _plan_prompts(text: str, title: str, word_count: int, scene_count: int,
                  chunk_count: int) -> tuple[str, str]:
    system = """你是漫剧总导演和美术指导。先做创意圣经与场景规划，不生成具体镜头。
仅输出 JSON。角色锚定必须具体并可原样复用。每个场景用 source_chunk_ids 标出相关原文分块。
输出结构：
{"title":"标题","creative_bible":{"style_anchor":"固定风格","aspect_ratio":"9:16",
"characters":[{"name":"角色","role":"定位","anchor_description":"固定视觉锚定"}]},
"scenes":[{"scene_id":"1","heading":"INT./EXT. 地点 - 时间","purpose":"叙事目的",
"visual_mood":"氛围","scene_template":"环境、光影、色彩、记忆点",
"source_chunk_ids":[1],"continuity":{"from_previous":"承接","to_next":"转场"}}]}"""
    user = f"""为《{title}》规划恰好 {scene_count} 个场景。内容量约 {word_count}，原文分为 {chunk_count} 块。
覆盖完整故事弧线与结局，source_chunk_ids 只能使用 1..{chunk_count}。

原文或分块摘要：
{text}"""
    return system, user


def _scene_prompts(text: str, title: str, bible: dict, scene: dict) -> tuple[str, str]:
    system = """你是影视分镜导演与AI提示词工程师。只为指定场景生成2-5个镜头，仅输出JSON。
结构：{"shots":[{"shot_id":"场景号.镜头号","duration_seconds":3,
"visual":{"shot_type":"景别","composition":"构图","composition_emotion":"情绪作用",
"camera_movement":"运镜机位","description":"可拍摄动作","color_tone":"色彩情绪"},
"audio":{"speaker":"","dialogue":"","narration":"","sound_music":""},
"prompts":{"image_cn":"五要素中文提示词","image_en":"five-element English prompt","video":"动态描述"},
"assets":{"image":"","voice":"","video":""},
"status":{"image":"pending","voice":"pending","video":"pending"}}]}
要求：角色完整复用视觉锚定；对白与旁白分开；镜头、动作、视线和轴线连续；duration_seconds为数字。"""
    context = {"title": title, "creative_bible": bible, "target_scene": scene}
    user = f"场景号必须为 {scene.get('scene_id')}。\n上下文：{json.dumps(context, ensure_ascii=False)}\n相关原文：\n{text}"
    return system, user


def _scene_source(scene: dict, chunks: list[str]) -> str:
    ids = scene.get("source_chunk_ids", [])
    selected = [chunks[index - 1] for index in ids
                if isinstance(index, int) and 1 <= index <= len(chunks)]
    if not selected:
        scene_number = max(1, int(str(scene.get("scene_id", "1")).split(".")[0] or 1))
        selected = [chunks[min(len(chunks) - 1, scene_number - 1)]]
    # A scene never receives the whole long novel; cap selected relevant context.
    return "\n\n".join(selected)[:60_000]


def generate_storyboard_staged(text: str, title: str, word_count: int,
                               scene_count: int, stage_dir: str,
                               resume: bool = True) -> dict | None:
    source_fingerprint = content_fingerprint(text, title, scene_count, STAGE_VERSION)
    run_dir = os.path.join(stage_dir, f"run_{source_fingerprint}")
    os.makedirs(run_dir, exist_ok=True)
    atomic_write_json(os.path.join(run_dir, "manifest.json"), {
        "stage_version": STAGE_VERSION, "source_fingerprint": source_fingerprint,
        "title": title, "scene_count": scene_count,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })

    chunks = _chunk_text(text)
    summaries = _summarize_chunks(chunks, run_dir, resume)
    if summaries is None:
        return None
    plan_input = text if len(chunks) == 1 else "\n\n".join(
        f"[分块 {index}] {summary}" for index, summary in enumerate(summaries, 1))

    plan_path = os.path.join(run_dir, "01_plan.json")
    plan = read_json(plan_path) if resume else None
    if not plan:
        print("   ① 创意圣经与场景规划...")
        system, user = _plan_prompts(plan_input, title, word_count, scene_count, len(chunks))
        plan = _call_json(system, user, 5000, 0.3,
                          os.path.join(run_dir, "01_plan_raw.txt"))
        if not plan:
            print("❌ 场景规划生成失败", file=sys.stderr)
            return None
        scenes = [scene for scene in plan.get("scenes", []) if isinstance(scene, dict)]
        if len(scenes) != scene_count:
            correction = user + f"\n上次数量错误。必须精确输出 {scene_count} 个 scenes。"
            plan = _call_json(system, correction, 5000, 0.2,
                              os.path.join(run_dir, "01_plan_retry_raw.txt"))
        atomic_write_json(plan_path, plan or {})
    scenes = [scene for scene in (plan or {}).get("scenes", []) if isinstance(scene, dict)]
    if len(scenes) != scene_count:
        print(f"❌ 场景规划数量不符：期望 {scene_count}，实际 {len(scenes)}", file=sys.stderr)
        return None
    plan.setdefault("title", title)
    plan.setdefault("creative_bible", {})

    print(f"   ② 逐场生成镜头（{len(scenes)} 场）...")
    completed: list[dict] = []
    for index, scene in enumerate(scenes, 1):
        scene_id = str(scene.get("scene_id") or index)
        scene["scene_id"] = scene_id
        source = _scene_source(scene, chunks)
        fingerprint = content_fingerprint(scene, plan["creative_bible"], source, "shots-v2")
        path = os.path.join(run_dir, f"02_scene_{index:03d}.json")
        cached = read_json(path) if resume else None
        if cached and cached.get("_stage_fingerprint") == fingerprint and cached.get("shots"):
            cached.pop("_stage_fingerprint", None)
            completed.append(cached)
            print(f"      [{index}/{len(scenes)}] 场景 {scene_id} ⏭ 已恢复")
            continue
        print(f"      [{index}/{len(scenes)}] 场景 {scene_id} 生成中")
        system, user = _scene_prompts(source, title, plan["creative_bible"], scene)
        result = _call_json(system, user, 7000, 0.35,
                            os.path.join(run_dir, f"02_scene_{index:03d}_raw.txt"))
        shots = result.get("shots") if isinstance(result, dict) else None
        if not isinstance(shots, list) or not shots:
            print(f"❌ 场景 {scene_id} 镜头生成失败；下次可从本运行目录续跑", file=sys.stderr)
            return None
        scene_result = {**scene, "shots": shots}
        atomic_write_json(path, {**scene_result, "_stage_fingerprint": fingerprint})
        completed.append(scene_result)

    storyboard = normalize_storyboard({
        "title": plan.get("title", title), "creative_bible": plan["creative_bible"],
        "scenes": completed,
    }, title=title)
    errors = validate_storyboard(storyboard)
    if errors:
        atomic_write_json(os.path.join(run_dir, "03_invalid_storyboard.json"), storyboard)
        print("❌ 分镜结构校验失败: " + "；".join(errors[:8]), file=sys.stderr)
        return None
    atomic_write_json(os.path.join(run_dir, "03_storyboard.json"), storyboard)
    atomic_write_json(os.path.join(stage_dir, "latest.json"), {
        "run_dir": os.path.relpath(run_dir, stage_dir), "source_fingerprint": source_fingerprint,
        "completed": True,
    })
    return storyboard
