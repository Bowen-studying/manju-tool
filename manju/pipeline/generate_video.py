"""Text/image-to-video generation with resumable polling and safe caching."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from manju.utils.config import load_manju_env
from manju.utils.runtime import (
    atomic_write_json,
    content_fingerprint,
    file_data_url,
    join_api_url,
    read_json,
    safe_filename,
)

DEFAULT_FRAMES = 121
DEFAULT_FPS = 24
DEFAULT_SIZE = "768x512"
_MIN_FRAMES = 25
_MAX_FRAMES = 441


def _get_video_config() -> dict:
    env = load_manju_env()
    return {
        "api_base": env.get("MANJU_VIDEO_API_BASE", ""),
        "api_key": env.get("MANJU_VIDEO_API_KEY", ""),
        "model": env.get("MANJU_VIDEO_MODEL", ""),
        "poll_base": env.get("MANJU_VIDEO_POLL_BASE", ""),
        "max_wait": int(env.get("MANJU_VIDEO_MAX_WAIT", "600") or 600),
    }


def _nearest_frames(target: int) -> int:
    target = max(_MIN_FRAMES, min(int(target), _MAX_FRAMES))
    lower = max(_MIN_FRAMES, ((target - 1) // 8) * 8 + 1)
    upper = min(_MAX_FRAMES, lower + 8)
    return lower if abs(target - lower) <= abs(target - upper) else upper


def _validate_size(size: str) -> str:
    try:
        width, height = (int(value) for value in str(size).lower().split("x", 1))
    except (ValueError, TypeError):
        return DEFAULT_SIZE
    return f"{max(64, width // 64 * 64)}x{max(64, height // 64 * 64)}"


def _request_json(request: urllib.request.Request, timeout: int, retries: int = 2) -> dict | None:
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            return result if isinstance(result, dict) else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            print(f"   ❌ 视频API HTTP {exc.code}: {detail}", file=sys.stderr)
            if exc.code != 429 and exc.code < 500:
                return None
        except urllib.error.URLError as exc:
            print(f"   ⚠ 视频API网络错误: {exc.reason}", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"   ⚠ 视频API响应错误: {exc}", file=sys.stderr)
            return None
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def _find_url(result: dict) -> str:
    for key in ("url", "video_url", "download_url"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("output", "data", "result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            found = _find_url(nested)
            if found:
                return found
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    found = _find_url(item)
                    if found:
                        return found
    return ""


def _find_id(result: dict) -> str:
    for key in ("video_id", "task_id", "id"):
        value = result.get(key)
        if value is not None and str(value):
            return str(value)
    for key in ("data", "result", "output"):
        nested = result.get(key)
        if isinstance(nested, dict):
            found = _find_id(nested)
            if found:
                return found
    return ""


def _create_video(prompt: str, image_url: str = "", num_frames: int = DEFAULT_FRAMES,
                  frame_rate: int = DEFAULT_FPS, size: str = DEFAULT_SIZE,
                  cfg: dict | None = None) -> dict | None:
    cfg = cfg or _get_video_config()
    if not cfg.get("api_key") or not cfg.get("api_base"):
        print("❌ 视频API配置不完整", file=sys.stderr)
        return None
    payload = {
        "model": cfg.get("model", ""), "prompt": prompt,
        "num_frames": _nearest_frames(num_frames), "frame_rate": max(1, int(frame_rate)),
        "size": _validate_size(size),
    }
    if image_url:
        payload["image"] = image_url
    request = urllib.request.Request(
        join_api_url(cfg["api_base"], "videos"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
    )
    result = _request_json(request, timeout=60)
    if result and result.get("error"):
        print(f"   ❌ 视频API错误: {result['error']}", file=sys.stderr)
        return None
    return result


def _poll_url(video_id: str, cfg: dict) -> str:
    poll_base = cfg.get("poll_base", "")
    if poll_base:
        if "{video_id}" in poll_base:
            return poll_base.replace("{video_id}", video_id)
        separator = "&" if "?" in poll_base else "?"
        return f"{poll_base.rstrip('/')}{separator}video_id={video_id}"
    return join_api_url(cfg["api_base"], f"videos/{video_id}")


def _poll_video(video_id: str, cfg: dict | None = None, max_wait: int | None = None) -> str | None:
    cfg = cfg or _get_video_config()
    max_wait = int(max_wait if max_wait is not None else cfg.get("max_wait", 600))
    query_url = _poll_url(video_id, cfg)
    started = time.monotonic()
    interval = 3
    consecutive_errors = 0
    success_states = {"completed", "succeeded", "success", "done", "finished"}
    failure_states = {"failed", "error", "cancelled", "canceled", "expired"}
    while time.monotonic() - started < max_wait:
        request = urllib.request.Request(
            query_url, headers={"Authorization": f"Bearer {cfg['api_key']}"})
        result = _request_json(request, timeout=15, retries=0)
        if result is None:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                print("   ❌ 连续查询失败，停止轮询", file=sys.stderr)
                return None
        else:
            consecutive_errors = 0
            url = _find_url(result)
            status = str(result.get("status") or result.get("state") or "").lower()
            if url and (not status or status in success_states):
                return url
            if status in success_states:
                print("   ❌ 任务完成但响应缺少视频URL", file=sys.stderr)
                return None
            if status in failure_states:
                print(f"   ❌ 视频任务失败: {result.get('error') or status}", file=sys.stderr)
                return None
        time.sleep(interval)
        interval = min(10, interval + 1)
    print(f"   ⚠ 视频生成超时 ({max_wait}s)，任务ID={video_id}", file=sys.stderr)
    return None


def _download_video(url: str, output_path: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=300) as response:
            content = response.read()
        if not content:
            return False
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as handle:
            handle.write(content)
        return True
    except urllib.error.HTTPError as exc:
        print(f"   ❌ 视频下载 HTTP {exc.code}", file=sys.stderr)
    except (urllib.error.URLError, OSError) as exc:
        print(f"   ❌ 视频下载失败: {exc}", file=sys.stderr)
    return False


def run_generate(prompt: str, image_path: str = "", num_frames: int = DEFAULT_FRAMES,
                 frame_rate: int = DEFAULT_FPS, size: str = DEFAULT_SIZE,
                 output_dir: str | None = None, output_name: str = "") -> str | None:
    cfg = _get_video_config()
    if not cfg.get("api_key") or not cfg.get("api_base"):
        print("❌ 未配置视频API", file=sys.stderr)
        return None
    output_dir = output_dir or os.path.join(os.getcwd(), "manju-output", "videos")
    os.makedirs(output_dir, exist_ok=True)
    normalized_size = _validate_size(size)
    image_identity = ""
    if image_path and os.path.isfile(image_path):
        image_identity = content_fingerprint(os.path.getsize(image_path), os.path.getmtime(image_path))
        image_url = file_data_url(image_path)
    elif image_path.startswith(("http://", "https://", "data:")):
        image_identity = image_path
        image_url = image_path
    elif image_path:
        print(f"❌ 参考图片不存在: {image_path}", file=sys.stderr)
        return None
    else:
        image_url = ""
    fingerprint = content_fingerprint(
        prompt, image_identity, _nearest_frames(num_frames), frame_rate, normalized_size, cfg.get("model"))
    filename = safe_filename(output_name or f"video_{prompt[:30]}", "video") + ".mp4"
    output_path = os.path.join(output_dir, filename)
    metadata_path = f"{output_path}.manju.json"
    metadata = read_json(metadata_path)
    if (metadata and metadata.get("fingerprint") == fingerprint
            and os.path.isfile(output_path) and os.path.getsize(output_path) > 1024):
        print(f"   ⏭ 内容未变化: {output_path}")
        return output_path

    result = _create_video(prompt, image_url, num_frames, frame_rate, normalized_size, cfg)
    if not result:
        return None
    url = _find_url(result)
    video_id = _find_id(result)
    if not url and video_id:
        url = _poll_video(video_id, cfg) or ""
    if not url:
        recovery = {
            "video_id": video_id,
            "query_url": _poll_url(video_id, cfg) if video_id else "",
            "fingerprint": fingerprint,
            "prompt": prompt,
        }
        atomic_write_json(os.path.join(output_dir, f"video_recovery_{fingerprint}.json"), recovery)
        return None
    if not _download_video(url, output_path):
        return None
    atomic_write_json(metadata_path, {
        "fingerprint": fingerprint, "prompt": prompt, "reference": image_path,
        "model": cfg.get("model", ""), "task_id": video_id,
    })
    print(f"   ✅ 视频已保存: {output_path}")
    return output_path
