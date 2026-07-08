"""manju-tool CLI — AI 漫剧制作：两种剧本入口 → 分镜 → 配音 → 视频。"""

import json
import os
import sys
from datetime import datetime

import click

from manju.pipeline.adapt import run_adapt
from manju.pipeline.create import run_create
from manju.pipeline.storyboard import run_storyboard
from manju.pipeline.video import run_video
from manju.pipeline.voice import run_voice
from manju.pipeline.generate_video import run_generate
from manju.pipeline.generate_image import run_image, run_batch_from_file
from manju.pipeline.generate_voice import run_speak, run_batch_speak, run_batch_speak_file
from manju.utils.use_guide import write_use_guide

OUTPUT_BASE = os.path.join(os.getcwd(), "manju-output")


@click.group()
def cli():
    """manju-tool: AI 漫剧制作工具 — 从剧本到AI短视频素材。

    两种入口：\n
      manju adapt <小说.txt>  — 小说→剧本\n
      manju create              — AI创作剧本\n
    然后接：storyboard → voice → video → pipeline
    \n
    直接生视频：\n
      manju generate <描述>    — 文字/图片→AI视频"""


# ── 剧本入口 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("-g", "--genre", default="",
              help="类型提示（古风/现代/科幻/悬疑/甜宠...）")
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认 OUTPUT_BASE/<date>/）")
def adapt(file, genre, output_dir):
    """小说→剧本：从小说文本提取角色/场景/对白为结构化剧本。

    FILE: 小说TXT文件路径
    """
    try:
        result = run_adapt(file, output_dir=output_dir, genre=genre)
        if result:
            click.echo(f"\n✅ 剧本适配完成: {len(result.get('scenes', []))} 场")
        else:
            click.echo("\n❌ 剧本适配失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--title", default="", help="剧名")
@click.option("--genre", default="", help="类型（古风/现代/科幻/悬疑...）")
@click.option("--premise", default="", help="一句话梗概（故事核）")
@click.option("--protagonist", default="", help="主角设定（姓名+性格+外貌）")
@click.option("--conflict", default="", help="核心冲突")
@click.option("--world-rules", default="", help="世界观规则（特殊设定）")
@click.option("--scenes", default="", help="目标场次（如：6-8场）")
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认 OUTPUT_BASE/<date>/）")
@click.option("--no-interactive", is_flag=True,
              help="纯命令行模式（不交互，需提供所有参数）")
def create(title, genre, premise, protagonist, conflict, world_rules,
           scenes, output_dir, no_interactive):
    """AI创作剧本：根据用户提供的关键信息生成完整剧本。

    无参数时进入交互模式，逐步引导填写。"""
    try:
        params = {}
        if title:
            params["title"] = title
        if genre:
            params["genre"] = genre
        if premise:
            params["premise"] = premise
        if protagonist:
            params["protagonist"] = protagonist
        if conflict:
            params["conflict"] = conflict
        if world_rules:
            params["world_rules"] = world_rules
        if scenes:
            params["target_duration"] = scenes

        # Non-interactive mode: require at least premise
        if no_interactive and not premise:
            click.echo("❌ 非交互模式需要至少 --premise 参数", err=True)
            sys.exit(1)

        interactive = not no_interactive
        if params:
            interactive = False  # If any params given, skip interactive

        result = run_create(
            params=params if params else None,
            output_dir=output_dir,
            interactive=interactive,
        )
        if result:
            click.echo(f"\n✅ 剧本创作完成: {len(result.get('scenes', []))} 场")
        elif not params and no_interactive:
            click.echo("\n❌ 参数不足或生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 制作命令 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认 OUTPUT_BASE/<date>/storyboard/）")
@click.option("--max-scenes", type=int, default=None,
              help="最多几场戏（默认按字数自动决定）")
@click.option("--image-api/--no-image-api", default=False,
              help="逐镜生图 (需配置生图API)")
def storyboard(file, output_dir, max_scenes, image_api):
    """分镜生成：读取剧本JSON或小说 → LLM生成分镜 → 可选生图。

    FILE: 剧本JSON或改编后小说TXT。
    输出 storyboard.json + storyboard.md。
    """
    try:
        result = run_storyboard(
            file, output_dir=output_dir,
            max_scenes=max_scenes, image_api=image_api,
        )
        if result:
            click.echo(f"\n✅ 分镜完成: {sum(len(s.get('shots', [])) for s in result.get('scenes', []))} 镜")
        else:
            click.echo("\n❌ 分镜生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("storyboard_json", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认与 storyboard.json 同目录）")
def video(storyboard_json, output_dir):
    """视频提示词：读取分镜JSON → 中英双版视频提示词。

    输出 video_prompts.json + video_prompts.md"""
    try:
        result = run_video(storyboard_json, output_dir=output_dir)
        if result is not None:
            click.echo("\n✅ 视频提示词完成")
        else:
            click.echo("\n❌ 生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("storyboard_json", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认与 storyboard.json 同目录）")
@click.option("--speak/--no-speak", default=False,
              help="生成配音音频文件")
def voice(storyboard_json, output_dir, speak):
    """配音脚本：读取分镜JSON → 提取对白 → 情绪推断。

    输出 voice_scripts.json + voice_scripts.pdf。
    加 --speak 同时生成 MP3 音频。"""
    try:
        result = run_voice(storyboard_json, output_dir=output_dir)
        if result is not None:
            click.echo("\n✅ 配音脚本完成")
            if speak:
                vdir = output_dir or os.path.dirname(os.path.abspath(storyboard_json))
                n = run_batch_speak(result, vdir)
                click.echo(f"\n🎙️  配音完成: {n} 个音频")
        else:
            click.echo("\n❌ 生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 视频生成 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("prompt")
@click.option("-i", "--image", default="",
              help="参考图URL（img2video模式）")
@click.option("--frames", type=int, default=121,
              help="帧数 (8n+1, ≤441, 默认121≈5s)")
@click.option("--fps", type=int, default=24,
              help="帧率 (默认24)")
@click.option("--size", default="768x512",
              help="分辨率 (默认768x512)")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
def generate(prompt, image, frames, fps, size, output_dir):
    """生成视频：文本描述 → AI视频（可选参考图）。

    PROMPT: 视频内容描述（中文或英文皆可）

    使用前需在 ~/.manju.env 中配置视频API：
      MANJU_VIDEO_API_KEY=your-key
      MANJU_VIDEO_API_BASE=https://your-api.example.com/v1
    """
    try:
        if not prompt or not prompt.strip():
            click.echo("❌ 请提供视频内容描述", err=True)
            sys.exit(1)

        result = run_generate(
            prompt, image_path=image,
            num_frames=frames, frame_rate=fps,
            size=size, output_dir=output_dir,
        )
        if result:
            click.echo(f"\n✅ 视频已保存: {result}")
        else:
            click.echo("\n⚠ 视频生成未完成（可稍后重试）", err=True)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 图片生成 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("prompt", required=False, default=None)
@click.option("-i", "--image", default="",
              help="参考图URL（img2img模式）")
@click.option("--size", default="1024x1024",
              help="分辨率 (默认1024x1024)")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
@click.option("-n", "--name", default="",
              help="输出文件名（不含扩展名）")
@click.option("--batch", "batch_file", type=click.Path(exists=True), default=None,
              help="批量模式：从文件读取提示词（每行一条，跳过空行/#注释）")
def image(prompt, image, size, output_dir, name, batch_file):
    """生成图片：文本描述 → AI图片（可选参考图）。

    PROMPT: 图片内容描述（中英文皆可）
    使用 --batch 文件路径 切换批量模式。

    使用前需在 ~/.manju.env 中配置生图API：
      MANJU_IMAGE_API_KEY=your-key
      MANJU_IMAGE_API_BASE=https://your-api.example.com/v1
      MANJU_IMAGE_MODEL=your-model-name
    """
    try:
        if batch_file:
            # Batch mode: read prompts from file
            count = run_batch_from_file(batch_file, output_dir=output_dir, size=size)
            if count > 0:
                click.echo(f"\n✅ 批量生图完成: {count} 张")
            else:
                click.echo("\n⚠ 批量生图失败", err=True)
                sys.exit(1)
            return

        # Single prompt mode — validate input
        if not prompt or not prompt.strip():
            click.echo("❌ 请提供提示词描述，或使用 --batch 从文件批量生成", err=True)
            sys.exit(1)

        result = run_image(
            prompt, image_path=image,
            size=size,
            output_dir=output_dir, output_name=name,
        )
        if result:
            click.echo(f"\n✅ 图片已保存: {result}")
        else:
            click.echo("\n⚠ 图片生成失败", err=True)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 配音生成 ──────────────────────────────────────────────────────────────────

@cli.command("speak")
@click.argument("text", required=False, default=None)
@click.option("-v", "--voice", default="xiaoxiao",
              help="音色 (xiaoxiao/yunxi/yunjian/yunyang/xiaoyi/yunxia)")
@click.option("--speed", type=float, default=1.0,
              help="语速 (0.25-4.0, 默认1.0)")
@click.option("--pitch", type=int, default=5,
              help="声调 1-10 (默认5)")
@click.option("--volume", type=int, default=5,
              help="音量 1-10 (默认5)")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
@click.option("-n", "--name", default="",
              help="输出文件名（不含扩展名）")
@click.option("--batch", "batch_file", type=click.Path(exists=True), default=None,
              help="批量模式：从文件读取文本行（每行一条，跳过空行/#注释）")
def speak(text, voice, speed, pitch, volume, output_dir, name, batch_file):
    """文字转语音：文本 → MP3音频。

    零配置即可使用（需 pip install edge-tts）。
    也可在 ~/.manju.env 中配置自选API：
      MANJU_VOICE_API_KEY=sk-...
      MANJU_VOICE_API_BASE=https://...
    """
    try:
        if batch_file:
            # Batch mode: read lines from file
            count = run_batch_speak_file(batch_file, output_dir=output_dir)
            if count > 0:
                click.echo(f"\n✅ 批量配音完成: {count} 个音频")
            else:
                click.echo("\n⚠ 批量配音失败", err=True)
                sys.exit(1)
            return

        # Single text mode — validate input
        if not text or not text.strip():
            click.echo("❌ 请提供要朗读的文本，或使用 --batch 从文件批量配音", err=True)
            sys.exit(1)

        result = run_speak(
            text, voice=voice, speed=speed,
            pitch=pitch, volume=volume,
            output_dir=output_dir, output_name=name,
        )
        if result:
            click.echo(f"\n✅ 音频已保存: {result}")
        else:
            click.echo("\n⚠ 生成失败", err=True)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 全流程 ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--script", "script_path", type=click.Path(exists=True),
              default=None, help="已有剧本JSON（跳过adapt/create）")
@click.option("--novel", type=click.Path(exists=True),
              default=None, help="小说TXT（自动 adapt → storyboard）")
@click.option("--genre", default="", help="类型提示")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
@click.option("--storyboard/--no-storyboard", "do_storyboard",
              default=True, help="生成分镜")
@click.option("--video/--no-video", "do_video",
              default=True, help="生成视频提示词")
@click.option("--voice/--no-voice", "do_voice",
              default=True, help="生成配音脚本")
@click.option("--speak/--no-speak", "do_speak",
              default=False, help="生成配音音频文件（需先启用 --voice）")
@click.option("--image-api/--no-image-api", default=False,
              help="生图")
@click.option("--max-scenes", type=int, default=None,
              help="最多几场戏")
def pipeline(script_path, novel, genre, output_dir, do_storyboard,
             do_video, do_voice, do_speak, image_api, max_scenes):
    """一键全流程：剧本 → 分镜 → 配音 → 视频提示词。

    三种启动方式：
      manju pipeline --script <剧本.json>         # 已有剧本
      manju pipeline --novel <小说.txt>           # 从小说开始
      manju pipeline                               # 交互式创作
    """
    click.echo("=" * 60)
    click.echo("  manju pipeline — AI 漫剧全流程")
    click.echo("=" * 60)

    now = datetime.now()
    today = f"{now.year}.{now.month}.{now.day}"
    out_dir = output_dir or os.path.join(OUTPUT_BASE, today)
    os.makedirs(out_dir, exist_ok=True)

    # ── Step 0: Get script ────────────────────────────────────────────────
    if script_path:
        click.echo(f"\n📄 已有剧本: {script_path}")
        script_file = script_path
    elif novel:
        click.echo(f"\n📖 从小说适配: {novel}")
        result = run_adapt(novel, output_dir=out_dir, genre=genre)
        if not result:
            click.echo("❌ 适配失败", err=True)
            sys.exit(1)
        script_file = os.path.join(out_dir,
                      os.path.splitext(os.path.basename(novel))[0] + "_script.json")
    else:
        click.echo("\n🎬 交互式创作剧本")
        result = run_create(output_dir=out_dir)
        if not result:
            click.echo("❌ 创作取消或失败", err=True)
            sys.exit(1)
        safe_title = result.get("title", "script").replace("/", "_")
        script_file = os.path.join(out_dir, f"{safe_title}_script.json")

    if not os.path.exists(script_file):
        click.echo(f"❌ 剧本文件不存在: {script_file}", err=True)
        sys.exit(1)

    # ── Step 1: Storyboard ────────────────────────────────────────────────
    storyboard_file = None
    if do_storyboard:
        click.echo(f"\n🎬 生成分镜...")
        sb_dir = os.path.join(out_dir, "storyboard")
        result = run_storyboard(script_file, output_dir=sb_dir,
                                max_scenes=max_scenes, image_api=image_api)
        if not result:
            click.echo("❌ 分镜生成失败", err=True)
            sys.exit(1)
        storyboard_file = os.path.join(sb_dir, "storyboard.json")
    else:
        # Find existing storyboard
        sb_dir = os.path.join(out_dir, "storyboard")
        for d in [sb_dir, out_dir]:
            candidate = os.path.join(d, "storyboard.json")
            if os.path.exists(candidate):
                storyboard_file = candidate
                sb_dir = d
                break

    if not storyboard_file or not os.path.exists(storyboard_file):
        click.echo("⚠ 无storyboard.json，跳过后续步骤")
    else:
        # ── Step 2: Voice ─────────────────────────────────────────────────
        if do_voice:
            click.echo(f"\n🎙 生成配音脚本...")
            voice_result = run_voice(storyboard_file, output_dir=out_dir)

            if do_speak and voice_result is not None:
                click.echo(f"\n🎙️  生成配音音频...")
                n = run_batch_speak(voice_result, out_dir)
                click.echo(f"   ✅ 配音完成: {n} 个音频")

        # ── Step 3: Video ─────────────────────────────────────────────────
        if do_video:
            click.echo(f"\n🎥 生成视频提示词...")
            run_video(storyboard_file, output_dir=out_dir)

        # Merge video prompts into storyboard xlsx
        vp_json = os.path.join(out_dir, "video_prompts.json")
        sb_json = os.path.join(sb_dir, "storyboard.json")
        if os.path.exists(vp_json) and os.path.exists(sb_json):
            with open(vp_json) as f: vp = json.load(f)
            with open(sb_json) as f: sb = json.load(f)
            vp_map = {s["shot_id"]: s for s in vp.get("shots", [])}
            for scene in sb.get("scenes", []):
                for shot in scene.get("shots", []):
                    sid = shot.get("shot_id", "")
                    if sid in vp_map:
                        shot["视频提示词_中文"] = vp_map[sid].get("seedance_prompt_cn", "")
                        shot["视频提示词_英文"] = vp_map[sid].get("seedance_prompt_en", "")
            try:
                from manju.utils.formats import write_xlsx
                write_xlsx(sb, os.path.join(sb_dir, "storyboard.xlsx"))
            except Exception:
                pass

    click.echo(f"\n{'═' * 50}")
    click.echo(f"  ✅ 全流程完成")
    click.echo(f"  输出目录: {out_dir}")
    click.echo(f"{'═' * 50}")

    # ── Step 4: Use Guide ─────────────────────────────────────────────────
    sb_dir = os.path.join(out_dir, "storyboard")
    gathered = {}
    xlsx_path = os.path.join(sb_dir, "storyboard.xlsx")
    if os.path.exists(xlsx_path):
        gathered["storyboard_xlsx"] = "storyboard.xlsx"
    voice_pdf_path = os.path.join(out_dir, "voice_scripts.pdf")
    if os.path.exists(voice_pdf_path):
        gathered["voice_pdf"] = "voice_scripts.pdf"
    video_pdf_path = os.path.join(out_dir, "video_prompts.pdf")
    if os.path.exists(video_pdf_path):
        gathered["video_pdf"] = "video_prompts.pdf"
    click.echo(f"\n📋 生成使用指南...")
    write_use_guide(out_dir, gathered)
    guide_pdf = os.path.join(out_dir, "使用指南.pdf")
    guide_docx = os.path.join(out_dir, "使用指南.docx")
    if os.path.exists(guide_pdf):
        click.echo(f"  📋 使用指南.pdf → {guide_pdf}")
    if os.path.exists(guide_docx):
        click.echo(f"  📋 使用指南.docx → {guide_docx}")



def main():
    cli()


if __name__ == "__main__":
    main()
