"""manju create — AI script creator from user-provided key information.

Collects genre, premise, character info, and world rules from the user,
then generates a complete structured script via LLM.
"""

import json
import os
import re
import sys
from datetime import datetime

from manju.utils.ai import call_llm, parse_json_response
from manju.utils.runtime import atomic_write_json, available_path, safe_filename


# ── Script generation prompt ────────────────────────────────────────────────────

def _build_create_prompt(params: dict) -> tuple[str, str]:
    """Build system and user prompts for original script creation.

    params keys:
      - title: str
      - genre: str
      - premise: str (one-sentence hook)
      - protagonist: str (name + description)
      - conflict: str (core conflict)
      - world_rules: str (special rules or setting)
      - target_duration: str (target scenes or minutes)
    """
    title = params.get("title", "未命名")
    genre = params.get("genre", "古风")
    premise = params.get("premise", "")
    protagonist = params.get("protagonist", "")
    conflict = params.get("conflict", "")
    world_rules = params.get("world_rules", "")
    target = params.get("target_duration", "6-8场")

    system_prompt = f"""你是顶尖漫剧编剧，擅长构造充满反转和钩子、角色鲜明、对白有力、场景紧凑的短剧剧本。

【创作约束】
- 类型：{genre}
- 目标场次：{target}
- 每场必有冲突或情绪变化
- 对白要有潜文本（角色说人话，但字面之下藏着真实意图）
- 漫剧小屏幕 → 视觉冲击力优先 → 避免冗长心理描写
- 严格按照JSON格式输出

【输出格式 — 严格JSON】
{{{{
  "title": "标题",
  "genre": "类型",
  "logline": "一句话梗概（Hook）",
  "characters": [
    {{{{ "name": "角色名", "role": "主角/配角/反派", "visual_anchor": "外貌+服装+体型+标志性特征（30字内）" }}}}
  ],
  "scenes": [
    {{{{ "scene_id": 1, "location": "地点", "time": "时间（如：深夜/黄昏/清晨）",
       "mood": "氛围（如：压抑紧张/温暖治愈/剑拔弩张）",
       "summary": "本场一句话概要",
       "dialogues": [
         {{{{ "character": "说话人", "text": "对白内容" }}}}
       ],
       "action_notes": "舞台指示/动作描述"
    }}}}
  ]
}}}}"""

    user_parts = [f"【标题】{title}"]
    if premise:
        user_parts.append(f"【故事核/梗概】{premise}")
    if protagonist:
        user_parts.append(f"【主角设定】{protagonist}")
    if conflict:
        user_parts.append(f"【核心冲突】{conflict}")
    if world_rules:
        user_parts.append(f"【世界观规则】{world_rules}")
    user_parts.append(f"\n请根据以上信息创作一个完整的{genre}漫剧短剧本，{target}。确保：")
    user_parts.append("1. 开场3场内有强钩子抓住观众")
    user_parts.append("2. 角色立体鲜活，每角色有独特的视觉锚定")
    user_parts.append("3. 对白口语化但暗藏信息量")
    user_parts.append("4. 每场结尾留悬念或情绪转折")
    user_parts.append("5. 最后一幕有爆发力收尾或钩子")

    return system_prompt, "\n".join(user_parts)


# ── Interactive collection ──────────────────────────────────────────────────────

def _collect_params_interactive(initial: dict | None = None) -> dict:
    """Interactively collect script creation parameters from user."""
    print("🎬 manju create — AI漫剧剧本创作")
    print("=" * 50)
    print("（按 Enter 使用默认值，Ctrl+C 退出）\n")

    params = dict(initial or {})

    if not params.get("title"):
        params["title"] = input("1. 剧名: ").strip() or "未命名漫剧"

    genres = ["古风", "现代", "科幻", "悬疑", "甜宠", "玄幻", "末日", "都市"]
    if not params.get("genre"):
        print(f"2. 类型 [{'/'.join(genres)}]: ", end="")
        genre = input().strip()
        params["genre"] = genre if genre else "古风"

    if not params.get("premise"):
        print("3. 一句话梗概（故事核，如「重生嫡女不再隐忍，开局手撕渣男」）:")
        params["premise"] = input().strip()

    if not params.get("protagonist"):
        print("4. 主角设定（姓名+性格+外貌关键特征，如「苏锦，18岁，冷艳嫡女，丹凤眼，月白襦裙」）:")
        params["protagonist"] = input().strip()

    if not params.get("conflict"):
        print("5. 核心冲突（主角想要什么？谁/什么在阻碍？）:")
        params["conflict"] = input().strip()

    if "world_rules" not in params:
        print("6. 世界观规则（特殊设定，如「修仙世界/灵力体系/末世丧尸」，无则回车）:")
        params["world_rules"] = input().strip()

    if not params.get("target_duration"):
        print("7. 目标场次 [6-8场]: ", end="")
        target = input().strip()
        params["target_duration"] = target if target else "6-8场"

    return params


# ── Main entry point ────────────────────────────────────────────────────────────

def run_create(
    params: dict | None = None,
    output_dir: str | None = None,
    output_base: str = "",
    interactive: bool = True,
) -> dict | None:
    """Create original script from user-provided parameters.

    Args:
        params: Dict of script parameters, or None for interactive mode.
                Keys: title, genre, premise, protagonist, conflict,
                      world_rules, target_duration
        output_dir: Output directory
        output_base: Base output directory (used if output_dir is None)
        interactive: Run interactive prompts if params is incomplete

    Returns:
        Script dict on success, None on failure.
    """
    # ── Collect params ─────────────────────────────────────────────────────
    if interactive:
        try:
            params = _collect_params_interactive(params)
        except (KeyboardInterrupt, EOFError):
            print("\n⚠ 用户取消")
            return None

    if not params:
        print("❌ 缺少创作参数", file=sys.stderr)
        return None

    # Fill defaults
    params.setdefault("title", "未命名")
    params.setdefault("genre", "古风")
    params.setdefault("target_duration", "6-8场")

    # ── Output directory ───────────────────────────────────────────────────
    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(output_base or os.path.join(os.getcwd(), "manju-output"), today)
    os.makedirs(output_dir, exist_ok=True)

    title = params["title"]
    print(f"\n📝 创作剧本: {title}")
    print(f"   类型: {params['genre']}")
    if params.get("premise"):
        print(f"   梗概: {params['premise']}")
    print(f"   输出: {output_dir}")
    print()

    # ── Generate with LLM ──────────────────────────────────────────────────
    system_prompt, user_prompt = _build_create_prompt(params)

    print("🤖 AI正在创作剧本...")
    response = call_llm(system_prompt, user_prompt, temperature=0.8)

    if not response:
        print("❌ LLM生成失败", file=sys.stderr)
        return None

    script = parse_json_response(response)
    if not script:
        return None

    # ── Add metadata ───────────────────────────────────────────────────────
    script["creation_params"] = {
        k: v for k, v in params.items()
        if v and k != "title"
    }
    script["creation_date"] = datetime.now().isoformat()

    # ── Save JSON ──────────────────────────────────────────────────────────
    safe_title = safe_filename(title, "script")
    json_path = available_path(os.path.join(output_dir, f"{safe_title}_script.json"))
    atomic_write_json(json_path, script)
    print(f"   📄 剧本JSON → {json_path}")

    # ── Summary ────────────────────────────────────────────────────────────
    chars = script.get("characters", [])
    scenes = script.get("scenes", [])
    total_dialogues = sum(len(s.get("dialogues", [])) for s in scenes)
    logline = script.get("logline", "")
    print(f"\n{'═' * 50}")
    print(f"  ✅ 剧本创作完成")
    if logline:
        print(f"  Hook: {logline}")
    print(f"  角色: {len(chars)} 个 — {', '.join(c.get('name', '?') for c in chars)}")
    print(f"  场次: {len(scenes)} 场")
    print(f"  对白: {total_dialogues} 句")
    print(f"  输出: {json_path}")
    print(f"{'═' * 50}")
    print(f"\n下一步: manju storyboard \"{json_path}\"")

    script["_output_path"] = json_path
    return script
