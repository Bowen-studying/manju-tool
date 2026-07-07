"""manju generate — text/image-to-video via Agnes AI API.

Supports:
  - txt2video: text prompt → AI-generated video
  - img2video: reference image + prompt → AI-generated video

Uses Agnes Video v2.0 (free tier, async polling).
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

AGNES_BASE = "https://apihub.agnes-ai.com/v1"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_agnes_key() -> str | None:
    """Get Agnes API key from environment."""
    key = os.environ.get("AGNES_API_KEY", "")
    if key:
        return key
    env_file = os.path.join(os.path.expanduser("~"), ".manju.env")
    try:
        with open(env_file) as f:
            for line in f:
                if line.startswith("AGNES_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


# ── Video constraints ─────────────────────────────────────────────────────────

# num_frames must be ≤ 441 and satisfy 8n+1
DEFAULT_FRAMES = 121   # ~5s at 24fps
DEFAULT_FPS = 24
DEFAULT_SIZE = "768x512"

# Pre-compute valid frame range for validation
_MIN_FRAMES = 25
_MAX_FRAMES = 441


def _nearest_frames(target: int) -> int:
    """Compute nearest valid frame count (8n+1, clamped to [25, 441])."""
    n = max(3, (target - 1) // 8)  # n≥3 → frames≥25
    lower = 8 * n + 1
    upper = min(8 * (n + 1) + 1, _MAX_FRAMES)
    if lower > _MAX_FRAMES:
        return _MAX_FRAMES
    if abs(target - lower) <= abs(target - upper):
        return lower
    return upper


def _validate_size(size: str) -> str:
    """Ensure size is valid (multiples of 64). Returns normalized size or raises."""
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
    api_key: str = "",
) -> dict | None:
    """Submit a video generation task to Agnes. Returns response dict or None."""
    key = api_key or _get_agnes_key()
    if not key:
        print("   ⚠ AGNES_API_KEY not set", file=sys.stderr)
        return None

    num_frames = _nearest_frames(num_frames)
    size = _validate_size(size)

    payload = {
        "model": "agnes-video-v2.0",
        "prompt": prompt,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
        "size": size,
    }

    # Image-to-video: add image parameter
    if image_url:
        payload["image"] = image_url
        mode = "img2video"
    else:
        mode = "txt2video"

    duration = num_frames / frame_rate
    print(f"   🎥 {mode}: {num_frames}帧@{frame_rate}fps ≈ {duration:.1f}s, {size}")

    try:
        req = urllib.request.Request(
            f"{AGNES_BASE}/videos",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"   ❌ Submission failed: {e}", file=sys.stderr)
        return None

    # Check for immediate error
    if "error" in result:
        print(f"   ❌ API error: {result['error']}", file=sys.stderr)
        return None

    return result


def _poll_video(video_id: str, api_key: str = "", max_wait: int = 600) -> str | None:
    """Poll for video completion. Returns download URL or None on timeout/failure."""
    key = api_key or _get_agnes_key()
    if not key:
        return None

    query_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
    start = time.time()
    interval = 5

    print(f"   ⏳ 等待生成 (video_id={video_id[:12]}...)")
    dots = 0

    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(
                query_url,
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            print(f"\n   ⚠ Poll error: {e}", file=sys.stderr)
            time.sleep(interval)
            continue

        status = result.get("status", "")
        dots += 1
        if dots % 6 == 0:
            print(f"   ... {status} ({int(time.time()-start)}s)")

        if status == "completed":
            url = result.get("url") or result.get("video_url") or ""
            if url:
                print(f"\n   ✅ 生成完成 ({int(time.time()-start)}s)")
                return url
            print(f"\n   ⚠ status=completed but no URL", file=sys.stderr)
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
    api_key: str = "",
) -> str | None:
    """Generate a video from text prompt (and optionally a reference image).

    Args:
        prompt: Text prompt describing the video content
        image_path: Local image path for img2video mode (optional)
        num_frames: Frame count (will be adjusted to nearest 8n+1)
        frame_rate: FPS
        size: Resolution like "768x512"
        output_dir: Output directory
        api_key: Agnes API key (uses env if empty)

    Returns:
        Path to downloaded video file, or None on failure.
    """
    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(os.getcwd(), "manju-output", today)
    os.makedirs(output_dir, exist_ok=True)

    # Handle image URL
    image_url = ""
    if image_path:
        if image_path.startswith("http://") or image_path.startswith("https://"):
            image_url = image_path
        elif os.path.exists(image_path):
            # For local files, we'd need to upload. For now, skip.
            print("   ⚠ 本地图片需先上传到可访问URL，暂不支持直接传文件路径", file=sys.stderr)
            return None
        else:
            print(f"   ⚠ 图片路径无效: {image_path}", file=sys.stderr)
            return None

    # Step 1: Submit
    result = _create_video(prompt, image_url, num_frames, frame_rate, size, api_key)
    if not result:
        return None

    # Extract video_id (Agnes uses 'task_id' in submit response, 'video_id' for query)
    video_id = result.get("video_id") or result.get("id") or result.get("task_id") or ""
    if not video_id:
        print("   ⚠ No video_id in response", file=sys.stderr)
        print(f"   Response: {json.dumps(result, indent=2)[:500]}")
        return None

    # Step 2: Poll
    url = _poll_video(video_id, api_key)
    if not url:
        # Save video_id for manual recovery
        id_path = os.path.join(output_dir, "video_id.txt")
        with open(id_path, "w") as f:
            f.write(f"video_id={video_id}\nquery_url=https://apihub.agnes-ai.com/agnesapi?video_id={video_id}\n")
        print(f"   📝 video_id 已保存到 {id_path}")
        return None

    # Step 3: Download
    safe_prompt = re.sub(r'[\\/*?:"<>|]', '_', prompt[:30])
    output_path = os.path.join(output_dir, f"video_{safe_prompt}.mp4")
    if _download_video(url, output_path):
        return output_path

    return None
