"""manju generate — text/image-to-video via configurable API.

Supports video generation through any compatible API endpoint.
Configure in ~/.manju.env:
  MANJU_VIDEO_API_KEY=sk-...         (required)
  MANJU_VIDEO_API_BASE=https://...    (required)
  MANJU_VIDEO_MODEL=model-name        (optional)
  MANJU_VIDEO_POLL_BASE=https://...   (optional, for async polling)

Or set environment variables with the same names.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

DEFAULT_FRAMES = 121   # ~5s at 24fps
DEFAULT_FPS = 24
DEFAULT_SIZE = "768x512"


# ── Config ─────────────────────────────────────────────────────────────────────

def _get_video_config() -> dict:
    """Read video API config from env or ~/.manju.env.

    Returns dict with keys: api_base, api_key, model, poll_base.
    """
    config = {
        "api_base": "",
        "api_key": "",
        "model": "agnes-video-v2.0",
        "poll_base": "",
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
        ("MANJU_VIDEO_API_BASE", "api_base"),
        ("MANJU_VIDEO_API_KEY", "api_key"),
        ("MANJU_VIDEO_MODEL", "model"),
        ("MANJU_VIDEO_POLL_BASE", "poll_base"),
    ]:
        val = env_keys.get(manju_key, "")
        if val:
            config[config_key] = val

    # Fallback: if no MANJU_VIDEO_API_KEY, try AGNES_API_KEY
    if not config["api_key"]:
        agnes_key = env_keys.get("AGNES_API_KEY", "")
        if agnes_key:
            config["api_key"] = agnes_key
            if not config["api_base"]:
                config["api_base"] = "https://apihub.agnes-ai.com/v1"
            if not config["poll_base"]:
                config["poll_base"] = "https://apihub.agnes-ai.com/agnesapi"

    return config


# ── Validation ─────────────────────────────────────────────────────────────────

# num_frames must be ≤ 441 and satisfy 8n+1
_MIN_FRAMES = 25
_MAX_FRAMES = 441


def _nearest_frames(target: int) -> int:
    """Compute nearest valid frame count (8n+1, clamped to [25, 441])."""
    n = max(3, (target - 1) // 8)
    lower = 8 * n + 1
    upper = min(8 * (n + 1) + 1, _MAX_FRAMES)
    if lower > _MAX_FRAMES:
        return _MAX_FRAMES
    if abs(target - lower) <= abs(target - upper):
        return lower
    return upper


def _validate_size(size: str) -> str:
    """Ensure size is valid (multiples of 64). Returns normalized size or default."""
    parts = size.lower().split("x")
    if len(parts) != 2:
        return DEFAULT_SIZE
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return DEFAULT_SIZE
    w = max(64, (w // 64) * 64)
    h = max(64, (h // 64) * 64)
    return f"{w}x{h}"


# ── API calls ─────────────────────────────────────────────────────────────────

def _create_video(
    prompt: str,
    image_url: str = "",
    num_frames: int = DEFAULT_FRAMES,
    frame_rate: int = DEFAULT_FPS,
    size: str = DEFAULT_SIZE,
    cfg: dict | None = None,
) -> dict | None:
    """Submit a video generation task. Returns response dict or None."""
    if cfg is None:
        cfg = _get_video_config()

    if not cfg["api_key"]:
        print("   ⚠ 视频API密钥未配置 (设置 MANJU_VIDEO_API_KEY)", file=sys.stderr)
        return None
    if not cfg["api_base"]:
        print("   ⚠ 视频API地址未配置 (设置 MANJU_VIDEO_API_BASE)", file=sys.stderr)
        return None

    num_frames = _nearest_frames(num_frames)
    size = _validate_size(size)

    payload = {
        "model": cfg["model"],
        "prompt": prompt,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
        "size": size,
    }

    if image_url:
        payload["image"] = image_url
        mode = "img2video"
    else:
        mode = "txt2video"

    duration = num_frames / frame_rate
    print(f"   🎥 {mode}: {num_frames}帧@{frame_rate}fps ≈ {duration:.1f}s, {size}")

    try:
        req = urllib.request.Request(
            f"{cfg['api_base'].rstrip('/')}/videos",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"   ❌ 提交失败: {e}", file=sys.stderr)
        return None

    if "error" in result:
        err_msg = result["error"]
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", str(err_msg))
        print(f"   ❌ API错误: {err_msg}", file=sys.stderr)
        return None

    return result


def _poll_video(video_id: str, cfg: dict | None = None, max_wait: int = 600) -> str | None:
    """Poll for video completion. Returns download URL or None on timeout/failure."""
    if cfg is None:
        cfg = _get_video_config()

    if not cfg["api_key"]:
        return None

    # Build poll URL: use configured poll_base, or derive from api_base
    poll_base = cfg.get("poll_base", "")
    if poll_base:
        query_url = f"{poll_base.rstrip('/')}?video_id={video_id}"
    else:
        query_url = f"{cfg['api_base'].rstrip('/')}/videos/{video_id}"

    start = time.time()
    interval = 5

    print(f"   ⏳ 等待生成 (video_id={video_id[:12]}...)")
    dots = 0

    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(
                query_url,
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            print(f"\n   ⚠ 查询异常: {e}", file=sys.stderr)
            time.sleep(interval)
            continue

        status = result.get("status", "")
        dots += 1
        if dots % 6 == 0:
            elapsed = int(time.time() - start)
            print(f"   ... {status} ({elapsed}s)")

        if status == "completed":
            url = result.get("url") or result.get("video_url") or ""
            if url:
                elapsed = int(time.time() - start)
                print(f"\n   ✅ 生成完成 ({elapsed}s)")
                return url
            print(f"\n   ⚠ 状态为completed但无URL", file=sys.stderr)
            return None

        if status == "failed":
            err = result.get("error", "unknown")
            print(f"\n   ❌ 生成失败: {err}", file=sys.stderr)
            return None

        time.sleep(interval)

    print(f"\n   ⚠ 超时 ({max_wait}s)，可手动查询 video_id={video_id}")
    return None


def _download_video(url: str, output_path: str) -> bool:
    """Download video from URL to local path."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=300) as resp:
            content = resp.read()
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(content)
        size_mb = len(content) / (1024 * 1024)
        print(f"   📥 下载完成: {output_path} ({size_mb:.1f}MB)")
        return True
    except Exception as e:
        print(f"   ❌ 下载失败: {e}", file=sys.stderr)
        return False


# ── Main entry point ──────────────────────────────────────────────────────────

def run_generate(
    prompt: str,
    image_path: str = "",
    num_frames: int = DEFAULT_FRAMES,
    frame_rate: int = DEFAULT_FPS,
    size: str = DEFAULT_SIZE,
    output_dir: str | None = None,
) -> str | None:
    """Generate a video from text prompt (and optionally a reference image).

    Args:
        prompt: Text prompt describing the video content
        image_path: Image URL for img2video mode (optional)
        num_frames: Frame count (will be adjusted to nearest 8n+1)
        frame_rate: FPS
        size: Resolution like "768x512"
        output_dir: Output directory

    Returns:
        Path to downloaded video file, or None on failure.
    """
    cfg = _get_video_config()
    if not cfg["api_key"]:
        print("❌ 未配置视频API。请在 ~/.manju.env 中设置:", file=sys.stderr)
        print("   MANJU_VIDEO_API_KEY=your-key", file=sys.stderr)
        print("   MANJU_VIDEO_API_BASE=https://your-api.example.com/v1", file=sys.stderr)
        print("   MANJU_VIDEO_MODEL=your-model-name", file=sys.stderr)
        return None

    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(os.getcwd(), "manju-output", today)
    os.makedirs(output_dir, exist_ok=True)

    # Determine image URL
    image_url = ""
    if image_path:
        if image_path.startswith("http://") or image_path.startswith("https://"):
            image_url = image_path
        elif os.path.exists(image_path):
            print("   ⚠ 本地图片需公网URL，当前仅支持txt2video", file=sys.stderr)
            return None
        else:
            print(f"   ⚠ 图片路径无效: {image_path}", file=sys.stderr)
            return None

    # Step 1: Submit
    result = _create_video(prompt, image_url, num_frames, frame_rate, size, cfg)
    if not result:
        return None

    # Extract video_id from response
    video_id = result.get("video_id") or result.get("id") or result.get("task_id") or ""
    if not video_id:
        print("   ⚠ 响应中未找到 video_id", file=sys.stderr)
        print(f"   Response: {json.dumps(result, indent=2)[:500]}")
        return None

    # Step 2: Poll
    url = _poll_video(video_id, cfg)
    if not url:
        # Save video_id for manual recovery
        id_path = os.path.join(output_dir, "video_id.txt")
        poll_base = cfg.get("poll_base", cfg["api_base"])
        with open(id_path, "w") as f:
            f.write(f"video_id={video_id}\nquery_url={poll_base}?video_id={video_id}\n")
        print(f"   📝 video_id 已保存: {id_path}")
        return None

    # Step 3: Download
    safe_prompt = re.sub(r'[\\/*?:"<>|]', '_', prompt[:30])
    output_path = os.path.join(output_dir, f"video_{safe_prompt}.mp4")
    if _download_video(url, output_path):
        return output_path

    return None
