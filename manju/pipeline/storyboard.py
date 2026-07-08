"""Storyboard generation — LLM-driven shot breakdown with optional image generation."""

import json
import os
import re
import sys
from datetime import datetime

from manju.utils.ai import call_llm, parse_json_response
from manju.utils.formats import write_xlsx
from manju.pipeline.generate_image import run_batch_images


# ── Utility functions ───────────────────────────────────────────────────────────

def _count_chinese(text: str) -> int:
    """Count Chinese characters in text."""
    return sum(1 for c in text if '一' <= c <= '鿿')


def _extract_title(file_path: str) -> str:
    """Extract a clean title from filename, stripping known suffixes."""
    name = os.path.splitext(os.path.basename(file_path))[0]
    for suffix in ["_script", "_storyboard", "_adapt", "_renamed"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name or "未命名"


def _clean_text(text: str) -> str:
    """Basic text normalization: normalize whitespace, strip markers."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


# ── Word count → scene count mapping ────────────────────────────────────────────

def _scenes_by_word_count(word_count: int) -> int:
    """Determine scene count from Chinese character count."""
    if word_count <= 2000:
        return 3
    elif word_count <= 6000:
        return 4
    else:
        return 6


# ── Storyboard prompt ───────────────────────────────────────────────────────────

def _build_storyboard_prompt(text: str, title: str, word_count: int, max_scenes: int) -> tuple[str, str]:
    """Build system and user prompts for storyboard generation.

    Integrates roles: screenwriter, director, art director, prompt engineer.
    Injects 6-part methodology: 5-element prompts, character consistency,
    exaggerated expressions, scene templates, composition + emotion, color + emotion.
    """
    system_prompt = f"""你是顶尖的影视分镜导演，同时精通AI生图提示词工程。请根据以下小说生成专业分镜脚本。

【你的角色】
你集成了四种专业能力：
1. **编剧** — 提取核心情节、角色、场景、情感弧线
2. **导演** — 用影像翻译情感，节奏即王权，视点意识贯彻全片
3. **美术指导** — 确定统一的视觉风格锚定词，全片基调一致
4. **AI提示词工程师** — 按照方法论编写每镜的生图/生视频提示词

【导演铁律】
- **用影像翻译情感**：不要用旁白解释人物心理，要用构图、光影、色彩让观众感受到情绪
- **节奏即王权**：每个镜头的时长、景别变化要形成音乐般的节奏感，紧张处快切，抒情处长镜
- **视点意识**：明确每个镜头是谁在看（角色主观/客观/上帝视角），视点就是态度

【场景数量要求】
本文约{word_count}字，请生成恰好{max_scenes}个关键场景。
每个场景包含2-5个镜头（短场景2-3镜，高潮场景可到5镜）。

【视觉风格锚定词】
必须为全片确定一个统一的视觉风格，贯穿所有镜头。例如：
- "shot on Kodak Portra 800, muted color palette, soft natural light"
- "cinematic chiaroscuro, deep shadows, golden hour rim light"
- "anime cel-shaded, vibrant colors, clean linework, dreamy bokeh"

═══════════════════════════════════════════════════════
【生图提示词方法论 — 核心铁律】
═══════════════════════════════════════════════════════

一、五要素结构化要求（每镜 image_prompt_cn 必须严格包含以下5个要素，缺一不可）：

(1) 【主体】按公式描述：年龄+性别+发型+眼型+服装+身份
    - 示例："20岁女性，及腰黑长直发，丹凤眼，身着绯红锦缎宫装，身份为亡国公主"
    - 禁止模糊描述如"一个女孩"、"一个人"

(2) 【场景】按公式描述：时间/光线+地点+核心元素+氛围
    - 示例："黄昏金色斜阳下，破败宫殿废墟，断柱残垣间野花丛生，悲凉而庄严"
    - 必须包含时段（清晨/正午/黄昏/深夜）和光线来源方向

(3) 【细节】按公式描述：动作+表情+姿态+环境互动+特效
    - 动作需具体（"右手握剑柄，剑身半出鞘"非"拿着剑"）
    - 表情见下方【表情外放方法论】

(4) 【风格】全片统一风格锚定词，每镜固定复用（如"中国古风CG渲染，水墨笔触融合金碧山水"）

(5) 【质量】每镜末尾必须追加："2K分辨率，高细节纹理，电影级体积光，次表面散射，景深虚化"

二、人物一致性 — 角色视觉锚定描述：

- 为每个主要角色生成一份"角色视觉锚定描述"，包含：外貌+服装+体型+标志性特征
- 在所有涉及该角色的镜头提示词中，固定复用此锚定描述
- 格式：【[角色名]: 锚定描述】，如：
  【[苏锦]: 18岁女性，垂鬟分髾髻，桃花眼眼角微红，身量纤弱但脊背挺直，身着月白交领襦裙外罩银灰大氅，右腕一道淡粉旧伤疤】

- 每个镜头的 image_prompt_cn 中，凡出现该角色，必须原样嵌入此锚定描述

三、表情外放 — 漫剧竖屏专用公式：

漫剧竖屏小屏幕，微表情根本看不见！必须夸张直给。
每镜表情描述使用公式：【[情绪词]】+【[五官动作分解]】+【[表演修饰]】+【[氛围加持]】

- 五官动作分解示例：瞳孔骤然放大、眉毛猛然扬起、嘴角剧烈上扬露出牙齿、鼻翼翕张
- 表演修饰示例：戏剧化舞台式表演、角色面向镜头打破第四面墙、夸张漫画式表情
- 氛围加持示例：背景炸裂特效线条、情绪可视化光晕、速度线/集中线
- 复合表情示例："【震惊转暴怒】+【瞳孔先放大后紧缩+眉毛从扬起转深锁+嘴角抽搐】+【戏剧化变脸表演】+【背后炸裂红色闪电特效】"

四、场景母版法：

- 为每个场景生成一份"场景母版"，包含：环境特征+光影系统+色彩系统+细节记忆点
- 同场景中所有镜头的提示词必须复用该场景母版的核心描述
- 示例：
  场景母版：【冷宫废墟】破败宫殿群/黄昏金色斜阳从破窗射入形成光柱/琥珀色暖光+冷蓝阴影对比/墙角蛛网+地面碎裂铜镜反光

五、构图指令 — 每镜必须含构图方式及情感定位：

- 三分法：右下=被困窒息感 / 左上=希望自由感 / 下=压抑沉重 / 上=仰望崇高
- 对称式：庄严权威 / 诡异不安 / 宿命感
- 框架式：窥视偷窥感 / 囚禁束缚感 / 被保护的安全感
- 引导线：纵深递进感 / 命运指引感 / 视线聚焦
- 对角线：动荡不安 / 力量对抗 / 速度紧张
- 留白：孤独寂寥 / 意境深远 / 内心空洞

六、色彩情绪 — 每镜必须含色调说明及情绪意图：

- 暖色调（橙/金/红）：温暖/激情/愤怒/危险/欲望
- 冷色调（蓝/青/紫）：孤独/悲伤/恐惧/神秘/冷静
- 中性色调（灰/白/米）：压抑/空虚/平和/纯粹/疏离

═══════════════════════════════════════════════════════

【输出格式 — 严格的JSON】
请仅输出以下JSON结构，不要任何解释文字，不要markdown代码块外的内容：

{{
  "title": "故事标题",
  "style_anchor": "全片统一的视觉风格锚定词（中英混合描述，如：中国古风CG渲染，水墨笔触融合金碧山水，浅景深电影镜头）",
  "characters": [
    {{
      "name": "角色名",
      "anchor_description": "角色视觉锚定描述（外貌+服装+体型+标志性特征）"
    }}
  ],
  "scenes": [
    {{
      "scene_id": 1,
      "scene_heading": "INT./EXT. 地点 - 时间",
      "visual_mood": "本场氛围关键词（如：压抑孤独、温暖治愈、剑拔弩张）",
      "scene_template": "场景母版（环境特征+光影系统+色彩系统+细节记忆点）",
      "shots": [
        {{
          "shot_id": "1.1",
          "shot_type": "景别（大远景/远景/全景/中景/近景/特写/大特写）",
          "composition": "构图方式（三分法/对称/引导线/框架/对角线/留白）",
          "composition_emotion": "构图对应的情感定位（如：右下三分=被困窒息感）",
          "camera_movement": "运动描述+机位（推/拉/摇/移/跟/升/降/手持/固定+俯/仰/平）",
          "duration": "建议时长（如：3s, 5s, 8s）",
          "visual_description": "画面内容（视觉化写作，用镜头语言描述而非文学描写）",
          "dialogue_narration": "对白或画外音（无则写空字符串）",
          "sound_music": "音效/音乐建议",
          "color_tone": "色调（暖/冷/中性）+ 情绪意图（如：冷蓝调→孤独悲伤）",
          "image_prompt_cn": "中文生图提示词（严格遵循五要素结构+角色锚定+表情外放+场景母版+构图+色彩）",
          "image_prompt_en": "English image prompt for AI generation (follow the same 5-element structure)",
          "video_prompt": "生视频提示词（描述镜头内运动或空字符串，如：camera slowly pushes in, hair blowing in wind）"
        }}
      ]
    }}
  ]
}}"""

    user_prompt = f"""请为以下小说生成分镜脚本。

【小说标题】{title}
【字数】约{word_count}字
【要求场景数】{max_scenes}场

【小说正文】
{text}

请严格按照JSON格式输出分镜脚本。严格执行上述六条方法论：
1. 每镜 image_prompt_cn 必须包含五要素（主体/场景/细节/风格/质量）
2. 为主要角色生成 visual_anchor 锚定描述并在所有镜头中复用
3. 表情必须夸张外放，使用【情绪词】+【五官分解】+【表演修饰】+【氛围加持】公式
4. 为每个场景生成 scene_template 场景母版
5. 每镜必须标注 composition_emotion 构图情感定位
6. 每镜必须标注 color_tone 色调+情绪意图

确保每个镜头的 image_prompt_cn 和 image_prompt_en 都内嵌了方法论要求的全部要素。"""

    return system_prompt, user_prompt


# ── Markdown output ─────────────────────────────────────────────────────────────

def _generate_markdown(storyboard: dict) -> str:
    """Generate human-readable Markdown from storyboard JSON."""
    lines = []
    title = storyboard.get("title", "未命名")
    style_anchor = storyboard.get("style_anchor", "")

    lines.append(f"# 📋 分镜脚本 — {title}")
    lines.append("")
    lines.append(f"**视觉风格**：{style_anchor}")
    lines.append("")

    # Characters section
    characters = storyboard.get("characters", [])
    if characters:
        lines.append("## 👥 角色视觉锚定")
        lines.append("")
        for char in characters:
            name = char.get("name", "?")
            anchor = char.get("anchor_description", "")
            lines.append(f"- **{name}**：{anchor}")
        lines.append("")

    scenes = storyboard.get("scenes", [])
    for scene in scenes:
        sid = scene.get("scene_id", "?")
        heading = scene.get("scene_heading", "")
        mood = scene.get("visual_mood", "")
        scene_template = scene.get("scene_template", "")

        lines.append(f"## 🎬 场景 {sid}：{heading}")
        lines.append(f"**氛围**：{mood}")
        if scene_template:
            lines.append(f"**场景母版**：{scene_template}")
        lines.append("")

        shots = scene.get("shots", [])
        for shot in shots:
            shot_id = shot.get("shot_id", "?")
            shot_type = shot.get("shot_type", "")
            composition = shot.get("composition", "")
            composition_emotion = shot.get("composition_emotion", "")
            camera = shot.get("camera_movement", "")
            duration = shot.get("duration", "")
            visual = shot.get("visual_description", "")
            dialogue = shot.get("dialogue_narration", "")
            sound = shot.get("sound_music", "")
            color_tone = shot.get("color_tone", "")
            prompt_cn = shot.get("image_prompt_cn", "")
            prompt_en = shot.get("image_prompt_en", "")
            video_prompt = shot.get("video_prompt", "")

            lines.append(f"### 镜头 {shot_id}")
            lines.append("")
            lines.append(f"| 项目 | 内容 |")
            lines.append(f"|------|------|")
            lines.append(f"| **景别** | {shot_type} |")
            lines.append(f"| **构图** | {composition} |")
            if composition_emotion:
                lines.append(f"| **构图情感** | {composition_emotion} |")
            lines.append(f"| **运动/机位** | {camera} |")
            if color_tone:
                lines.append(f"| **色调/情绪** | {color_tone} |")
            lines.append(f"| **时长** | {duration} |")
            lines.append("")
            lines.append(f"**画面内容**：")
            lines.append(f"> {visual}")
            lines.append("")

            if dialogue:
                lines.append(f"**对白/画外音**：")
                lines.append(f"> {dialogue}")
                lines.append("")

            if sound:
                lines.append(f"**音效/音乐**：{sound}")
                lines.append("")

            lines.append(f"**中文生图提示词**：")
            lines.append(f"```")
            lines.append(f"{prompt_cn}")
            lines.append(f"```")
            lines.append("")

            lines.append(f"**English Image Prompt**：")
            lines.append(f"```")
            lines.append(f"{prompt_en}")
            lines.append(f"```")
            lines.append("")

            if video_prompt:
                lines.append(f"**生视频提示词**：")
                lines.append(f"```")
                lines.append(f"{video_prompt}")
                lines.append(f"```")
                lines.append("")

            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ── Image generation (optional) ──────────────────────────────────────────────────

def _generate_images_from_storyboard(storyboard: dict, output_dir: str) -> int:
    """Generate images for all shots using configured image API.

    Strategy: first shot txt2img as reference → remaining img2img in parallel.

    Returns number of successfully generated images.
    """
    # Collect all shots with prompts
    shots_info = []
    for scene in storyboard.get("scenes", []):
        for shot in scene.get("shots", []):
            shot_id = shot.get("shot_id", "")
            # Use English prompt for better image generation quality
            prompt = shot.get("image_prompt_en", shot.get("image_prompt_cn", ""))
            if not prompt or not shot_id:
                continue
            shots_info.append({
                "shot_id": shot_id,
                "prompt": prompt,
                "output_filename": f"shot_{shot_id.replace('.', '_')}.png",
            })

    if not shots_info:
        print("   ⚠️ No shots with image prompts found")
        return 0

    return run_batch_images(shots_info, output_dir)


# ── Main entry point ────────────────────────────────────────────────────────────

def run_storyboard(
    file_path: str,
    output_dir: str | None = None,
    max_scenes: int | None = None,
    image_api: bool = False,
    output_base: str = "",
) -> dict | None:
    """Generate storyboard from a script JSON or novel text file.

    Args:
        file_path: Path to script JSON (.json) or novel text (.txt/.docx)
        output_dir: Output directory (default: OUTPUT_BASE/<date>/storyboard/)
        max_scenes: Max scenes override (default: auto-determined by word count)
        image_api: If True, call local image gen gateway (localhost:8787)
        output_base: Base output directory for auto-pathing

    Returns:
        Storyboard dict on success, None on failure.
    """
    # ── Read input ────────────────────────────────────────────────────────────
    try:
        if file_path.endswith(".docx"):
            import zipfile
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(file_path) as z:
                doc_xml = z.read("word/document.xml")
            root = ET.fromstring(doc_xml)
            paragraphs = []
            for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                texts = []
                for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
                    if t.text:
                        texts.append(t.text)
                if texts:
                    paragraphs.append("".join(texts))
            raw_text = "\n".join(paragraphs)
        else:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_text = f.read()
    except Exception as e:
        print(f"❌ 读取文件失败: {e}", file=sys.stderr)
        return None

    if not raw_text or not raw_text.strip():
        print("❌ 文件内容为空", file=sys.stderr)
        return None

    title = _extract_title(file_path)
    word_count = _count_chinese(raw_text)

    # ── Determine scene count ─────────────────────────────────────────────────
    if max_scenes is None:
        max_scenes = _scenes_by_word_count(word_count)
    max_scenes = max(3, min(max_scenes, 8))

    # ── Set up output directory ───────────────────────────────────────────────
    now = datetime.now()
    today = f"{now.year}.{now.month}.{now.day}"
    if output_dir:
        storyboard_dir = output_dir
    else:
        storyboard_dir = os.path.join(output_base, today, "storyboard")
    os.makedirs(storyboard_dir, exist_ok=True)

    print(f"📖 文章: {title}")
    print(f"   字数: {word_count} → 目标场景: {max_scenes} 场")
    print(f"   输出: {storyboard_dir}")

    # ── Clean text for LLM ────────────────────────────────────────────────────
    cleaned = _clean_text(raw_text)

    # ── Call LLM for storyboard ───────────────────────────────────────────────
    print(f"\n🎬 生成分镜脚本中... (this may take 30-120s)")
    sys.stdout.flush()

    system_prompt, user_prompt = _build_storyboard_prompt(cleaned, title, word_count, max_scenes)
    response = call_llm(system_prompt, user_prompt, max_tokens=16000)

    if not response:
        print("❌ LLM 分镜生成失败", file=sys.stderr)
        return None

    storyboard = parse_json_response(response)
    if not storyboard:
        print("❌ 解析分镜JSON失败", file=sys.stderr)
        debug_path = os.path.join(storyboard_dir, "storyboard_raw.txt")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(response)
        print(f"   💾 原始响应已保存: {debug_path}")
        return None

    # ── Validate storyboard structure ─────────────────────────────────────────
    storyboard.setdefault("title", title)
    storyboard.setdefault("style_anchor", "")
    storyboard.setdefault("characters", [])

    if "scenes" not in storyboard:
        print("❌ 分镜JSON缺少 'scenes' 字段", file=sys.stderr)
        return None

    # Ensure all fields exist on each shot
    default_shot_fields = {
        "shot_id": "?",
        "shot_type": "",
        "composition": "",
        "composition_emotion": "",
        "camera_movement": "",
        "duration": "",
        "visual_description": "",
        "dialogue_narration": "",
        "sound_music": "",
        "color_tone": "",
        "image_prompt_cn": "",
        "image_prompt_en": "",
        "video_prompt": "",
    }
    for scene in storyboard["scenes"]:
        scene.setdefault("scene_id", 0)
        scene.setdefault("scene_heading", "")
        scene.setdefault("visual_mood", "")
        scene.setdefault("scene_template", "")
        for shot in scene.get("shots", []):
            for key, default in default_shot_fields.items():
                shot.setdefault(key, default)

    total_scenes = len(storyboard["scenes"])
    total_shots = sum(len(s.get("shots", [])) for s in storyboard["scenes"])
    print(f"   ✅ 生成 {total_scenes} 场戏, {total_shots} 个镜头")

    # ── Save JSON ──────────────────────────────────────────────────────────────
    json_path = os.path.join(storyboard_dir, "storyboard.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(storyboard, f, ensure_ascii=False, indent=2)

    # ── Save Excel ────────────────────────────────────────────────────────────
    xlsx_path = os.path.join(storyboard_dir, "storyboard.xlsx")
    try:
        write_xlsx(storyboard, xlsx_path)
        print(f"   📊 storyboard.xlsx → {xlsx_path}")
    except Exception as e:
        print(f"   ⚠ Excel: {e}")

    # ── Save Markdown ─────────────────────────────────────────────────────────
    md_path = os.path.join(storyboard_dir, "storyboard.md")
    md_content = _generate_markdown(storyboard)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"   📝 storyboard.md  → {md_path}")

    # ── Optional: Generate images ─────────────────────────────────────────────
    if image_api:
        img_count = _generate_images_from_storyboard(storyboard, storyboard_dir)
        print(f"   🖼️  生图完成: {img_count}/{total_shots} 张")

        # Update JSON with image paths after generation
        for scene in storyboard["scenes"]:
            for shot in scene.get("shots", []):
                if "_image_path" in shot:
                    rel_path = os.path.relpath(shot["_image_path"], storyboard_dir)
                    shot["image_path"] = rel_path
                    del shot["_image_path"]

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(storyboard, f, ensure_ascii=False, indent=2)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═' * 50}")
    print(f"  ✅ 分镜生成完成")
    print(f"  输出目录: {storyboard_dir}")
    print(f"  场景: {total_scenes} 场 | 镜头: {total_shots} 个")
    style_preview = storyboard.get("style_anchor", "N/A")[:60]
    print(f"  视觉风格: {style_preview}...")
    print(f"{'═' * 50}")

    return storyboard
