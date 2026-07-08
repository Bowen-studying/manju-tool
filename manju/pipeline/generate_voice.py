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
import subprocess
import sys
import time
import urllib.request
from datetime import datetime


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

    env_keys = dict(os.environ)
    env_file = os.path.join(os.path.expanduser("~"), ".manju.env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() not in env_keys:
                        env_keys[key.strip()] = val.strip()
    except Exception:
        pass

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
    import shutil
    path = shutil.which("edge-tts")
    if path:
        return path
    # Try common venv locations
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


# ── OpenAI-compatible Speech API backend ────────────────────────────────────────

def _speak_api(
    text: str,
    output_path: str,
    voice: str = "alloy",
    speed: float = 1.0,
    cfg: dict | None = None,
) -> bool:
    """Generate speech via OpenAI-compatible /v1/audio/speech endpoint."""
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

    try:
        req = urllib.request.Request(
            f"{cfg['api_base'].rstrip('/')}/v1/audio/speech",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(content)

        size_kb = len(content) / 1024
        return size_kb > 0.5  # TTS output should be >500 bytes
    except Exception as e:
        print(f"   ⚠ 语音API失败: {e}", file=sys.stderr)
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

    safe_name = output_name or re.sub(r'[\\/*?:"<>|]', '_', text[:30]).strip('_')
    output_path = os.path.join(output_dir, f"{safe_name}.mp3")

    # Skip if already exists
    if os.path.exists(output_path) and os.path.getsize(output_path) > 500:
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
        size_kb = os.path.getsize(output_path) / 1024
        print(f"   ✅ {output_path} ({size_kb:.0f}KB)")
        return output_path

    return None


# ── Batch generation from voice scripts ────────────────────────────────────────

def run_batch_speak(
    voice_scripts: list[dict],
    output_dir: str,
) -> int:
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

    success = 0
    for i, vs in enumerate(speakable):
        shot_id = str(vs.get("shot_id", "?"))
        text = vs["text"]
        speed = float(vs.get("speed", 1.0))
        pitch = int(vs.get("pitch", 5))
        volume = int(vs.get("volume", 5))

        output_path = os.path.join(audio_dir, f"shot_{shot_id.replace('.', '_')}.mp3")

        print(f"   🎙️ [{i+1}/{len(speakable)}] 镜头 {shot_id}: "
              f"\"{text[:30]}{'...' if len(text)>30 else ''}\" ... ", end="", flush=True)

        if cfg["backend"] == "api":
            ok = _speak_api(text, output_path, voice="alloy", speed=speed, cfg=cfg)
        else:
            rate_pct = int((speed - 1.0) * 100)
            rate = f"{'+' if rate_pct >= 0 else ''}{rate_pct}%"
            pitch_hz = (pitch - 5) * 4
            pitch_str = f"{'+' if pitch_hz >= 0 else ''}{pitch_hz}Hz"
            vol_pct = int((volume - 5) / 5 * 50)
            vol_str = f"{'+' if vol_pct >= 0 else ''}{vol_pct}%"
            ok = _speak_edge_tts(text, output_path, voice=DEFAULT_EDGE_VOICE,
                                 rate=f"{'+' if rate_pct >= 0 else ''}{rate_pct}%",
                                 pitch=f"{'+' if pitch_hz >= 0 else ''}{pitch_hz}Hz",
                                 volume=f"{'+' if vol_pct >= 0 else ''}{vol_pct}%")

        if ok:
            print("✅")
            success += 1
        else:
            print("❌")

    return success
