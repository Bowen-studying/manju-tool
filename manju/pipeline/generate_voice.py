"""manju speak — text-to-speech via configurable API.

Supports any OpenAI-compatible Audio/Speech API, plus edge-tts as a
zero-setup default. Configure in ~/.manju.env:

  MANJU_VOICE_API_KEY=sk-...        (optional — if set, uses HTTP API)
  MANJU_VOICE_API_BASE=https://...   (optional)
  MANJU_VOICE_MODEL=model-name       (optional, e.g. tts-1)

When MANJU_VOICE_API_KEY is NOT set, falls back to edge-tts CLI
(pip install edge-tts). No registration, no key, unlimited free.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
import time
from datetime import datetime

from manju.utils.config import load_manju_env
from manju.utils.runtime import atomic_write_json, content_fingerprint, join_api_url, read_json, safe_filename


# ── Config ─────────────────────────────────────────────────────────────────────

def _get_voice_config() -> dict:
    """Read voice API config from env or ~/.manju.env.

    Returns dict with: api_base, api_key, model, backend (edge-tts or api).
    """
    config = {
        "api_base": "",
        "api_key": "",
        "model": "tts-1",
        "backend": "edge-tts",  # default
    }

    env_keys = load_manju_env()

    for manju_key, config_key in [
        ("MANJU_VOICE_API_BASE", "api_base"),
        ("MANJU_VOICE_API_KEY", "api_key"),
        ("MANJU_VOICE_MODEL", "model"),
    ]:
        val = env_keys.get(manju_key, "")
        if val:
            config[config_key] = val

    if config["api_key"] and config["api_base"]:
        config["backend"] = "api"

    return config


# ── Edge-TTS backend (zero-setup default) ──────────────────────────────────────

# Voice map: manju character archetypes → Edge TTS Chinese voices
EDGE_VOICE_MAP = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # warm female
    "yunxi":    "zh-CN-YunxiNeural",      # lively male
    "yunjian":  "zh-CN-YunjianNeural",    # passionate male
    "yunyang":  "zh-CN-YunyangNeural",    # professional male
    "xiaoyi":   "zh-CN-XiaoyiNeural",     # lively female
    "yunxia":   "zh-CN-YunxiaNeural",     # cute male
}

DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


def _find_edge_tts() -> str | None:
    """Find edge-tts executable. Returns path or None."""
    path = shutil.which("edge-tts")
    if path:
        return path
    # Try common locations
    candidates = [
        os.path.expanduser("~/.local/bin/edge-tts"),
        "/usr/local/bin/edge-tts",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _speak_edge_tts(
    text: str,
    output_path: str,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: str = "+0%",
    pitch: str = "+0Hz",
    volume: str = "+0%",
) -> bool:
    """Generate speech via edge-tts CLI. Returns True on success."""
    edge_tts = _find_edge_tts()
    if not edge_tts:
        print("   ⚠ edge-tts 未安装。安装: pip install edge-tts", file=sys.stderr)
        print("   或配置 MANJU_VOICE_API_KEY 使用自选API", file=sys.stderr)
        return False

    # Resolve voice alias
    if voice in EDGE_VOICE_MAP:
        voice = EDGE_VOICE_MAP[voice]
    elif not voice.startswith("zh-"):
        voice = EDGE_VOICE_MAP.get(voice, DEFAULT_EDGE_VOICE)

    cmd = [
        edge_tts,
        "--voice", voice,
        "--text", text,
        f"--rate={rate}",
        f"--pitch={pitch}",
        f"--volume={volume}",
        "--write-media", output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.isfile(output_path):
            return True
        if result.stderr:
            # edge-tts writes progress to stderr, only fail if no file
            if not os.path.isfile(output_path):
                print(f"   ⚠ edge-tts: {result.stderr.strip()[-200:]}", file=sys.stderr)
                return False
            return True
        return False
    except FileNotFoundError:
        print("   ⚠ edge-tts 未安装", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("   ⚠ TTS 超时", file=sys.stderr)
        return False
    except Exception as e:
        print(f"   ⚠ TTS 失败: {e}", file=sys.stderr)
        return False


# ── API backend ────────────────────────────────────────────────────────────────

def _speak_api(
    text: str,
    output_path: str,
    voice: str = "alloy",
    speed: float = 1.0,
    cfg: dict | None = None,
) -> bool:
    """Generate speech via /v1/audio/speech endpoint."""
    if cfg is None:
        cfg = _get_voice_config()

    if not cfg["api_key"] or not cfg["api_base"]:
        print("   ⚠ 语音API未配置 (设置 MANJU_VOICE_API_KEY + MANJU_VOICE_API_BASE)", file=sys.stderr)
        return False

    payload = {
        "model": cfg["model"],
        "input": text,
        "voice": voice,
        "speed": speed,
        "response_format": "mp3",
    }

    req = urllib.request.Request(
            join_api_url(cfg["api_base"], "audio/speech"),
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
        )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()
            if len(content) <= 500:
                print("   ⚠ 语音API返回内容过短", file=sys.stderr)
                return False
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(content)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            print(f"   ⚠ 语音API HTTP {e.code}: {body}", file=sys.stderr)
            if e.code != 429 and e.code < 500:
                return False
        except urllib.error.URLError as e:
            print(f"   ⚠ 语音API网络错误: {e.reason}", file=sys.stderr)
        except OSError as e:
            print(f"   ⚠ 语音API失败: {e}", file=sys.stderr)
            return False
        if attempt < 2:
            time.sleep(2 ** attempt)
    return False


# ── Unified speak entry ────────────────────────────────────────────────────────

def run_speak(
    text: str,
    voice: str = "xiaoxiao",
    speed: float = 1.0,
    pitch: int = 5,
    volume: int = 5,
    output_dir: str | None = None,
    output_name: str = "",
) -> str | None:
    """Generate speech audio from text.

    Args:
        text: Text to speak
        voice: Voice name (for edge-tts: xiaoxiao/yunxi/etc; for API: alloy/echo/etc)
        speed: Speech rate (0.25-4.0, 1.0 = normal)
        pitch: Pitch level 1-10 (only used by edge-tts backend)
        volume: Volume level 1-10 (only used by edge-tts backend)
        output_dir: Output directory
        output_name: Output filename (without extension)

    Returns:
        Path to MP3 file, or None on failure.
    """
    cfg = _get_voice_config()

    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(os.getcwd(), "manju-output", today, "voice")
    os.makedirs(output_dir, exist_ok=True)

    speed = max(0.25, min(float(speed), 4.0))
    pitch = max(1, min(int(pitch), 10))
    volume = max(1, min(int(volume), 10))
    safe_name = safe_filename(output_name or text[:30], "speech")
    output_path = os.path.join(output_dir, f"{safe_name}.mp3")
    fingerprint = content_fingerprint(text, voice, speed, pitch, volume, cfg.get("backend"), cfg.get("model"))
    cache_path = f"{output_path}.manju.json"
    cache = read_json(cache_path)
    if (cache and cache.get("fingerprint") == fingerprint
            and os.path.exists(output_path) and os.path.getsize(output_path) > 500):
        print(f"   ⏭ 已存在: {output_path}")
        return output_path

    if cfg["backend"] == "api":
        print(f"   🎙️ API TTS: {cfg['model']} | voice={voice} | speed={speed}")
        ok = _speak_api(text, output_path, voice=voice, speed=speed, cfg=cfg)
    else:
        # Convert manju params to edge-tts format
        rate_pct = int((speed - 1.0) * 100)
        rate = f"{'+' if rate_pct >= 0 else ''}{rate_pct}%"
        pitch_hz = (pitch - 5) * 4
        pitch_str = f"{'+' if pitch_hz >= 0 else ''}{pitch_hz}Hz"
        vol_pct = int((volume - 5) / 5 * 50)
        vol_str = f"{'+' if vol_pct >= 0 else ''}{vol_pct}%"

        print(f"   🎙️ TTS: {voice} | speed={speed} | pitch={pitch} | vol={volume}")
        preview = text[:60] + ("..." if len(text) > 60 else "")
        print(f"   📝 {preview}")

        ok = _speak_edge_tts(text, output_path, voice=voice, rate=rate, pitch=pitch_str, volume=vol_str)

    if ok:
        atomic_write_json(cache_path, {"fingerprint": fingerprint, "text": text,
                                      "voice": voice, "speed": speed,
                                      "pitch": pitch, "volume": volume})
        size_kb = os.path.getsize(output_path) / 1024
        print(f"   ✅ {output_path} ({size_kb:.0f}KB)")
        return output_path

    return None


# ── Batch generation from voice scripts ────────────────────────────────────────

def run_batch_speak(
    voice_scripts: list[dict],
    output_dir: str,
    return_paths: bool = False,
) -> int | dict[str, str]:
    """Generate speech audio for a batch of voice script entries.

    Args:
        voice_scripts: List from voice.py output, each with:
            shot_id, text, speed, pitch, volume, character
        output_dir: Directory to save MP3 files

    Returns:
        Number of successfully generated audio files.
    """
    cfg = _get_voice_config()

    audio_dir = os.path.join(output_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    # Filter: only process lines with actual text (not silent shots)
    speakable = [
        s for s in voice_scripts
        if s.get("text", "").strip() and s.get("text", "").strip() not in ("（无对白）", "（无有效台词）")
    ]
    silent = len(voice_scripts) - len(speakable)

    if not speakable:
        print("   ⚠ 无有效台词可配音")
        return 0

    print(f"\n🎙️  开始配音：共 {len(speakable)} 句（跳过 {silent} 个无声镜头）")
    mode = "API" if cfg["backend"] == "api" else "edge-tts"
    print(f"   后端: {mode}")

    generated: dict[str, str] = {}
    for i, vs in enumerate(speakable):
        shot_id = str(vs.get("shot_id", "?"))
        text = vs["text"]
        speed = float(vs.get("speed", 1.0))
        pitch = int(vs.get("pitch", 5))
        volume = int(vs.get("volume", 5))

        output_path = os.path.join(audio_dir, f"shot_{safe_filename(shot_id, 'unknown')}.mp3")
        selected_voice = (vs.get("voice_api") or "alloy") if cfg["backend"] == "api" \
            else (vs.get("voice_edge") or DEFAULT_EDGE_VOICE)
        backend_voice = str(selected_voice)
        fingerprint = content_fingerprint(text, backend_voice, speed, pitch, volume,
                                          cfg["backend"], cfg.get("model"))
        cache_path = f"{output_path}.manju.json"
        cache = read_json(cache_path)
        if (cache and cache.get("fingerprint") == fingerprint
                and os.path.isfile(output_path) and os.path.getsize(output_path) > 500):
            print("⏭")
            generated[shot_id] = output_path
            continue

        print(f"   🎙️ [{i+1}/{len(speakable)}] 镜头 {shot_id}: "
              f"\"{text[:30]}{'...' if len(text)>30 else ''}\" ... ", end="", flush=True)

        if cfg["backend"] == "api":
            ok = _speak_api(text, output_path, voice=backend_voice or "alloy", speed=speed, cfg=cfg)
        else:
            ok = _speak_edge_tts(text, output_path, voice=backend_voice,
                                 rate=f"{'+' if int((speed - 1.0) * 100) >= 0 else ''}{int((speed - 1.0) * 100)}%",
                                 pitch=f"{'+' if (pitch - 5) * 4 >= 0 else ''}{(pitch - 5) * 4}Hz",
                                 volume=f"{'+' if int((volume - 5) / 5 * 50) >= 0 else ''}{int((volume - 5) / 5 * 50)}%")

        if ok:
            print("✅")
            generated[shot_id] = output_path
            atomic_write_json(cache_path, {"fingerprint": fingerprint, "text": text,
                                          "voice": backend_voice})
        else:
            print("❌")

    return generated if return_paths else len(generated)


# ── Batch generation from file ─────────────────────────────────────────────────

def run_batch_speak_file(
    file_path: str,
    output_dir: str | None = None,
) -> int:
    """Generate speech audio for lines read from a file (one per line).

    Process all lines sequentially. Blank lines and #-comments are skipped.

    Args:
        file_path: Path to a text file with one line of text per line
        output_dir: Directory to save MP3 files

    Returns:
        Number of successfully generated audio files.
    """
    if not os.path.isfile(file_path):
        print(f"❌ 文件不存在: {file_path}", file=sys.stderr)
        return 0

    # Read and parse lines
    lines = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                lines.append(stripped)
    except Exception as e:
        print(f"❌ 读取文件失败: {e}", file=sys.stderr)
        return 0

    if not lines:
        print("❌ 文件中没有有效的文本行", file=sys.stderr)
        return 0

    cfg = _get_voice_config()

    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(os.getcwd(), "manju-output", today, "voice")
    os.makedirs(output_dir, exist_ok=True)

    mode = "API" if cfg["backend"] == "api" else "edge-tts"
    print(f"\n🎙️  批量配音：从文件读取 {len(lines)} 行")
    print(f"   后端: {mode}")

    success = 0
    for i, text in enumerate(lines, start=1):
        safe_name = f"{i:03d}_{safe_filename(text[:30], 'speech')}_{content_fingerprint(text, length=8)}"
        output_path = os.path.join(output_dir, f"{safe_name}.mp3")

        print(f"   🎙️ [{i}/{len(lines)}] \"{text[:40]}{'...' if len(text)>40 else ''}\" ... ",
              end="", flush=True)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 500:
            print("⏭ (已存在)")
            success += 1
            continue

        if cfg["backend"] == "api":
            ok = _speak_api(text, output_path, voice="alloy", speed=1.0, cfg=cfg)
        else:
            ok = _speak_edge_tts(text, output_path, voice=DEFAULT_EDGE_VOICE,
                                 rate="+0%", pitch="+0Hz", volume="+0%")

        if ok:
            print("✅")
            success += 1
        else:
            print("❌")

    return success


def count_batch_lines(file_path: str) -> int:
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip() and not line.lstrip().startswith("#"))
    except (OSError, UnicodeError):
        return 0
