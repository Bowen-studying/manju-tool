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
from manju.pipeline.generate_image import (
    count_batch_lines as count_image_batch_lines,
    run_image, run_batch_from_file,
)
from manju.pipeline.generate_voice import (
    count_batch_lines as count_voice_batch_lines,
    run_speak, run_batch_speak, run_batch_speak_file,
)
from manju.utils.use_guide import write_use_guide
from manju.utils.runtime import atomic_write_json, safe_filename

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
              help="目标场景数（1-8，默认按字数自动决定）")
@click.option("--image-api/--no-image-api", default=False,
              help="逐镜生图 (需配置生图API)")
@click.option("--resume/--no-resume", default=True,
              help="从相同源文件的已完成阶段续跑")
def storyboard(file, output_dir, max_scenes, image_api, resume):
    """分镜生成：读取剧本JSON或小说 → LLM生成分镜 → 可选生图。

    FILE: 剧本JSON或改编后小说TXT。
    输出 v2 storyboard.json + storyboard.md + storyboard.xlsx，
    并在 stages/ 保留各生成阶段产物。
    """
    try:
        result = run_storyboard(
            file, output_dir=output_dir,
            max_scenes=max_scenes, image_api=image_api, resume=resume,
            strict_exports=True,
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
        result = run_video(storyboard_json, output_dir=output_dir, strict_exports=True)
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
        result = run_voice(storyboard_json, output_dir=output_dir, strict_exports=True)
        if result is not None:
            click.echo("\n✅ 配音脚本完成")
            if speak:
                vdir = output_dir or os.path.dirname(os.path.abspath(storyboard_json))
                paths = run_batch_speak(result, vdir, return_paths=True)
                expected = sum(1 for line in result if line.get("text") not in ("（无对白）", "（无有效台词）"))
                with open(storyboard_json, encoding="utf-8") as handle:
                    state = json.load(handle)
                base = os.path.dirname(os.path.abspath(storyboard_json))
                for scene in state.get("scenes", []):
                    for shot in scene.get("shots", []):
                        shot_id = str(shot.get("shot_id", ""))
                        if shot_id in paths:
                            shot.setdefault("assets", {})["voice"] = os.path.relpath(paths[shot_id], base)
                            shot.setdefault("status", {})["voice"] = "completed"
                atomic_write_json(storyboard_json, state)
                if len(paths) != expected:
                    raise click.ClickException(f"配音未完全成功: {len(paths)}/{expected}")
                click.echo(f"\n🎙️  配音完成: {len(paths)} 个音频")
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
            raise click.ClickException("视频生成失败")
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
            total = count_image_batch_lines(batch_file)
            if total > 0 and count == total:
                click.echo(f"\n✅ 批量生图完成: {count} 张")
            else:
                raise click.ClickException(f"批量生图未完全成功: {count}/{total}")
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
            raise click.ClickException("图片生成失败")
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
            total = count_voice_batch_lines(batch_file)
            if total > 0 and count == total:
                click.echo(f"\n✅ 批量配音完成: {count} 个音频")
            else:
                raise click.ClickException(f"批量配音未完全成功: {count}/{total}")
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
            raise click.ClickException("配音生成失败")
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
@click.option("--storyboard-json", type=click.Path(exists=True), default=None,
              help="已有分镜JSON（直接进入配音/视频阶段）")
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
@click.option("--render-videos/--no-render-videos", default=False,
              help="按镜头调用视频API生成视频素材（可能产生费用）")
@click.option("--resume/--no-resume", default=True,
              help="续跑相同输入的分镜阶段与素材缓存")
@click.option("--max-scenes", type=int, default=None,
              help="目标场景数（1-8）")
def pipeline(script_path, storyboard_json, novel, genre, output_dir, do_storyboard,
             do_video, do_voice, do_speak, image_api, render_videos, resume, max_scenes):
    """一键全流程：剧本 → 分镜 → 配音 → 视频提示词。

    三种启动方式：
      manju pipeline --script <剧本.json>         # 已有剧本
      manju pipeline --novel <小说.txt>           # 从小说开始
      manju pipeline                               # 交互式创作
    """
    click.echo("=" * 60)
    if do_speak and not do_voice:
        raise click.UsageError("--speak 需要同时启用 --voice")
    if render_videos and not do_video:
        raise click.UsageError("--render-videos 需要同时启用 --video")
    click.echo("  manju pipeline — AI 漫剧全流程")
    click.echo("=" * 60)

    now = datetime.now()
    today = now.strftime("%Y.%m.%d_%H%M%S")
    out_dir = output_dir or os.path.join(OUTPUT_BASE, today)
    os.makedirs(out_dir, exist_ok=True)

    # ── Step 0: Get script ────────────────────────────────────────────────
    if storyboard_json:
        click.echo(f"\n🎬 已有分镜: {storyboard_json}")
        script_file = ""
    elif script_path:
        click.echo(f"\n📄 已有剧本: {script_path}")
        script_file = script_path
    elif novel:
        click.echo(f"\n📖 从小说适配: {novel}")
        result = run_adapt(novel, output_dir=out_dir, genre=genre)
        if not result:
            click.echo("❌ 适配失败", err=True)
            sys.exit(1)
        script_file = result.get("_output_path") or os.path.join(
            out_dir, os.path.splitext(os.path.basename(novel))[0] + "_script.json")
    else:
        click.echo("\n🎬 交互式创作剧本")
        result = run_create(output_dir=out_dir)
        if not result:
            click.echo("❌ 创作取消或失败", err=True)
            sys.exit(1)
        script_file = result.get("_output_path", "")

    if not storyboard_json and not os.path.exists(script_file):
        click.echo(f"❌ 剧本文件不存在: {script_file}", err=True)
        sys.exit(1)

    # ── Step 1: Storyboard ────────────────────────────────────────────────
    storyboard_file = storyboard_json
    if storyboard_json:
        sb_dir = os.path.dirname(os.path.abspath(storyboard_json))
    elif do_storyboard:
        click.echo(f"\n🎬 生成分镜...")
        sb_dir = os.path.join(out_dir, "storyboard")
        result = run_storyboard(script_file, output_dir=sb_dir,
                                max_scenes=max_scenes, image_api=image_api,
                                resume=resume, strict_exports=True)
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
        raise click.ClickException("没有可用的 storyboard.json；请启用分镜生成或使用 --storyboard-json")
    else:
        # ── Step 2: Voice ─────────────────────────────────────────────────
        if do_voice:
            click.echo(f"\n🎙 生成配音脚本...")
            voice_result = run_voice(storyboard_file, output_dir=out_dir, strict_exports=True)
            if voice_result is None:
                click.echo("❌ 配音脚本生成失败", err=True)
                raise click.ClickException("全流程在配音阶段停止")

            if do_speak and voice_result is not None:
                click.echo(f"\n🎙️  生成配音音频...")
                audio_paths = run_batch_speak(voice_result, out_dir, return_paths=True)
                expected = sum(1 for line in voice_result if line.get("text") not in ("（无对白）", "（无有效台词）"))
                if len(audio_paths) != expected:
                    raise click.ClickException(f"配音未完全成功: {len(audio_paths)}/{expected}")
                click.echo(f"   ✅ 配音完成: {len(audio_paths)} 个音频")
                with open(storyboard_file, encoding="utf-8") as handle:
                    storyboard_state = json.load(handle)
                for scene in storyboard_state.get("scenes", []):
                    for shot in scene.get("shots", []):
                        shot_id = str(shot.get("shot_id", ""))
                        if shot_id in audio_paths:
                            shot.setdefault("assets", {})["voice"] = os.path.relpath(audio_paths[shot_id], sb_dir)
                            shot.setdefault("status", {})["voice"] = "completed"
                atomic_write_json(storyboard_file, storyboard_state)

        # ── Step 3: Video ─────────────────────────────────────────────────
        if do_video:
            click.echo(f"\n🎥 生成视频提示词...")
            video_prompts = run_video(storyboard_file, output_dir=out_dir, strict_exports=True)
            if video_prompts is None:
                click.echo("❌ 视频提示词生成失败", err=True)
                raise click.ClickException("全流程在视频提示词阶段停止")

            if render_videos:
                with open(storyboard_file, encoding="utf-8") as handle:
                    storyboard_state = json.load(handle)
                shot_map = {str(shot.get("shot_id", "")): shot
                            for scene in storyboard_state.get("scenes", [])
                            for shot in scene.get("shots", [])}
                rendered = 0
                for prompt in video_prompts:
                    shot_id = str(prompt.get("shot_id", ""))
                    shot = shot_map.get(shot_id, {})
                    image_rel = shot.get("assets", {}).get("image", "")
                    image_path = os.path.join(sb_dir, image_rel) if image_rel else ""
                    result_path = run_generate(
                        prompt.get("video_prompt_en") or prompt.get("video_prompt_cn", ""),
                        image_path=image_path, output_dir=os.path.join(out_dir, "videos"),
                        output_name=f"shot_{safe_filename(shot_id, 'unknown')}",
                        num_frames=max(25, int(float(shot.get("duration_seconds", 3)) * 24)),
                    )
                    if result_path:
                        rendered += 1
                        shot.setdefault("assets", {})["video"] = os.path.relpath(result_path, sb_dir)
                        shot.setdefault("status", {})["video"] = "completed"
                    else:
                        shot.setdefault("status", {})["video"] = "failed"
                atomic_write_json(storyboard_file, storyboard_state)
                if rendered != len(video_prompts):
                    raise click.ClickException(f"逐镜视频未完全成功: {rendered}/{len(video_prompts)}")

        # Merge video prompts into storyboard xlsx
        vp_json = os.path.join(out_dir, "video_prompts.json")
        sb_json = storyboard_file
        if os.path.exists(vp_json) and os.path.exists(sb_json):
            with open(vp_json, encoding="utf-8") as f: vp = json.load(f)
            with open(sb_json, encoding="utf-8") as f: sb = json.load(f)
            vp_map = {s["shot_id"]: s for s in vp.get("shots", [])}
            for scene in sb.get("scenes", []):
                for shot in scene.get("shots", []):
                    sid = shot.get("shot_id", "")
                    if sid in vp_map:
                        shot.setdefault("prompts", {})["video_cn"] = vp_map[sid].get("video_prompt_cn", "")
                        shot.setdefault("prompts", {})["video_en"] = vp_map[sid].get("video_prompt_en", "")
            atomic_write_json(sb_json, sb)
            try:
                from manju.utils.formats import write_xlsx
                write_xlsx(sb, os.path.join(sb_dir, "storyboard.xlsx"))
            except Exception as exc:
                raise click.ClickException(f"更新分镜Excel失败: {exc}") from exc

    click.echo(f"\n{'═' * 50}")
    click.echo(f"  ✅ 全流程完成")
    click.echo(f"  输出目录: {out_dir}")
    click.echo(f"{'═' * 50}")

    # ── Step 4: Use Guide ─────────────────────────────────────────────────
    sb_dir = os.path.dirname(os.path.abspath(storyboard_file)) if storyboard_file else os.path.join(out_dir, "storyboard")
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
    guide_result = write_use_guide(out_dir, gathered)
    if not guide_result.get("pdf") or not guide_result.get("docx"):
        raise click.ClickException("使用指南未完整生成，请检查 weasyprint/python-docx")
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
