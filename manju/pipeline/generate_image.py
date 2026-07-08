"""manju image — text-to-image and image-to-image via configurable API.

Supports any OpenAI-compatible Images API endpoint.
Configure in ~/.manju.env:
  MANJU_IMAGE_API_KEY=sk-...        (required)
  MANJU_IMAGE_API_BASE=https://...   (required)
  MANJU_IMAGE_MODEL=model-name       (optional, default depends on endpoint)

Or set environment variables with the same names.
"""

import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

DEFAULT_SIZE = "1024x1024"


# ── Config ─────────────────────────────────────────────────────────────────────

def _get_image_config() -> dict:
    """Read image API config from env or ~/.manju.env.

    Returns dict with keys: api_base, api_key, model.
    All values may be empty strings if not configured.
    """
    config = {
        "api_base": "",
        "api_key": "",
        "model": "agnes-image-2.1-flash",
    }

    # Collect env keys from both os.environ and ~/.manju.env
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

    # Read image-specific config
    for manju_key, config_key in [
        ("MANJU_IMAGE_API_BASE", "api_base"),
        ("MANJU_IMAGE_API_KEY", "api_key"),
        ("MANJU_IMAGE_MODEL", "model"),
    ]:
        val = env_keys.get(manju_key, "")
        if val:
            config[config_key] = val

    # Fallback: if no MANJU_IMAGE_API_KEY but AGNES_API_KEY exists, use that
    if not config["api_key"]:
        agnes_key = env_keys.get("AGNES_API_KEY", "")
        if agnes_key:
            config["api_key"] = agnes_key
            # Also set default base if not configured
            if not config["api_base"]:
                config["api_base"] = "https://apihub.agnes-ai.com/v1"

    return config


# ── Validation ─────────────────────────────────────────────────────────────────

def _validate_size(size: str) -> str:
    """Ensure size is valid (multiples of 16). Returns normalized size or default."""
    parts = size.lower().split("x")
    if len(parts) != 2:
        return DEFAULT_SIZE
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return DEFAULT_SIZE
    w = max(64, (w // 16) * 16)
    h = max(64, (h // 16) * 16)
    return f"{w}x{h}"


# ── API calls ──────────────────────────────────────────────────────────────────

def _generate_txt2img(
    prompt: str,
    size: str = DEFAULT_SIZE,
    model: str = "",
    api_base: str = "",
    api_key: str = "",
) -> str | None:
    """Submit a txt2img request. Returns image URL or None."""
    if not api_key:
        print("   ⚠ 生图API密钥未配置 (设置 MANJU_IMAGE_API_KEY)", file=sys.stderr)
        return None
    if not api_base:
        print("   ⚠ 生图API地址未配置 (设置 MANJU_IMAGE_API_BASE)", file=sys.stderr)
        return None

    size = _validate_size(size)

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
    }

    try:
        req = urllib.request.Request(
            f"{api_base.rstrip('/')}/images/generations",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"   ❌ 生图请求失败: {e}", file=sys.stderr)
        return None

    if "error" in result:
        err_msg = result["error"]
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", str(err_msg))
        print(f"   ❌ API错误: {err_msg}", file=sys.stderr)
        return None

    # Extract URL — standard OpenAI format: data[0].url
    data = result.get("data", [])
    if data and isinstance(data, list):
        url = data[0].get("url", "")
        if url:
            return url
    url = result.get("url", "")
    if url:
        return url

    print(f"   ⚠ 响应中未找到图片URL", file=sys.stderr)
    return None


def _generate_img2img(
    prompt: str,
    ref_url: str,
    size: str = DEFAULT_SIZE,
    model: str = "",
    api_base: str = "",
    api_key: str = "",
) -> str | None:
    """Submit an img2img request. Returns image URL or None."""
    if not api_key:
        print("   ⚠ 生图API密钥未配置 (设置 MANJU_IMAGE_API_KEY)", file=sys.stderr)
        return None

    size = _validate_size(size)

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "image": ref_url,
    }

    try:
        req = urllib.request.Request(
            f"{api_base.rstrip('/')}/images/generations",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"   ❌ 图生图请求失败: {e}", file=sys.stderr)
        return None

    if "error" in result:
        err_msg = result["error"]
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("message", str(err_msg))
        print(f"   ❌ API错误: {err_msg}", file=sys.stderr)
        return None

    data = result.get("data", [])
    if data and isinstance(data, list):
        url = data[0].get("url", "")
        if url:
            return url
    url = result.get("url", "")
    if url:
        return url

    return None


# ── Download ───────────────────────────────────────────────────────────────────

def _download_image(url: str, output_path: str, max_retries: int = 3) -> bool:
    """Download image from URL to local path with retries."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Skip if already exists and has reasonable size
    if os.path.exists(output_path) and os.path.getsize(output_path) >= 10240:
        size_kb = os.path.getsize(output_path) / 1024
        print(f"   ⏭ 已存在: {output_path} ({size_kb:.0f}KB)")
        return True

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=120) as resp:
                content = resp.read()
            with open(output_path, "wb") as f:
                f.write(content)
            size_kb = len(content) / 1024
            print(f"   ✅ 已保存: {output_path} ({size_kb:.0f}KB)")
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"   ⚠ 下载重试 {attempt+1}/{max_retries} ({wait}s): {e}", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"   ❌ 下载失败: {e}", file=sys.stderr)

    return False


# ── Main entry point ───────────────────────────────────────────────────────────

def run_image(
    prompt: str,
    image_path: str = "",
    size: str = DEFAULT_SIZE,
    output_dir: str | None = None,
    output_name: str = "",
) -> str | None:
    """Generate an image from text prompt (and optionally a reference image).

    Args:
        prompt: Text prompt describing the image
        image_path: Local image path or URL for img2img mode (optional)
        size: Resolution like "1024x1024" (must be 16x multiples)
        output_dir: Output directory
        output_name: Output filename (without extension)

    Returns:
        Path to downloaded image file, or None on failure.
    """
    cfg = _get_image_config()
    if not cfg["api_key"]:
        print("❌ 未配置生图API。请在 ~/.manju.env 中设置:", file=sys.stderr)
        print("   MANJU_IMAGE_API_KEY=your-key", file=sys.stderr)
        print("   MANJU_IMAGE_API_BASE=https://your-api.example.com/v1", file=sys.stderr)
        print("   MANJU_IMAGE_MODEL=your-model-name", file=sys.stderr)
        return None

    if output_dir is None:
        now = datetime.now()
        today = f"{now.year}.{now.month}.{now.day}"
        output_dir = os.path.join(os.getcwd(), "manju-output", today, "images")
    os.makedirs(output_dir, exist_ok=True)

    # Determine mode
    ref_url = ""
    if image_path:
        if image_path.startswith("http://") or image_path.startswith("https://"):
            ref_url = image_path
            mode = "img2img"
        elif os.path.exists(image_path):
            print("   ⚠ 本地图片需公网URL才能做图生图，当前仅支持txt2img", file=sys.stderr)
            mode = "txt2img"
        else:
            print(f"   ⚠ 图片路径无效: {image_path}", file=sys.stderr)
            mode = "txt2img"
    else:
        mode = "txt2img"

    size = _validate_size(size)
    print(f"   🎨 {mode}: {size} | model={cfg['model']}")
    preview = prompt[:100] + ("..." if len(prompt) > 100 else "")
    print(f"   📝 {preview}")

    # Generate
    if mode == "img2img":
        url = _generate_img2img(prompt, ref_url, size, cfg["model"], cfg["api_base"], cfg["api_key"])
    else:
        url = _generate_txt2img(prompt, size, cfg["model"], cfg["api_base"], cfg["api_key"])

    if not url:
        return None

    # Download
    safe_name = output_name or re.sub(r'[\\/*?:"<>|]', '_', prompt[:40]).strip('_')
    output_path = os.path.join(output_dir, f"{safe_name}.png")
    if _download_image(url, output_path):
        return output_path

    # Save URL for manual download
    url_file = os.path.join(output_dir, f"{safe_name}_url.txt")
    with open(url_file, "w") as f:
        f.write(f"{url}\n")
    print(f"   📝 图片URL已保存: {url_file}")

    return None


# ── Batch generation for storyboard ────────────────────────────────────────────

def run_batch_images(
    prompts: list[dict],
    output_dir: str,
    size: str = DEFAULT_SIZE,
) -> int:
    """Generate images for a batch of prompts (storyboard mode).

    Strategy:
      1. First shot: txt2img, saved as reference
      2. Remaining shots: img2img using first shot as reference, in parallel

    Args:
        prompts: List of dicts with keys: shot_id, prompt, output_filename
        output_dir: Directory to save images
        size: Image resolution

    Returns:
        Number of successfully generated images.
    """
    cfg = _get_image_config()
    if not cfg["api_key"]:
        print("   ⚠ 生图API未配置，跳过", file=sys.stderr)
        return 0

    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    if not prompts:
        print("   ⚠ 无生图提示词")
        return 0

    print(f"\n🖼️  开始生图：共 {len(prompts)} 个镜头")
    print(f"   模型: {cfg['model']} | 尺寸: {size}")
    print(f"   策略: 第1张 txt2img → 其余并行 img2img")

    success_count = 0

    # Step 1: First shot — txt2img
    first = prompts[0]
    first_path = os.path.join(
        images_dir,
        first.get("output_filename", f"shot_{first['shot_id']}.png"),
    )

    print(f"   📸 镜头 {first['shot_id']} — txt2img ... ", end="", flush=True)
    ref_url = _generate_txt2img(first["prompt"], size, cfg["model"], cfg["api_base"], cfg["api_key"])
    if ref_url and _download_image(ref_url, first_path):
        success_count += 1
    else:
        print("❌ 参考图失败，中止")
        return 0

    if len(prompts) <= 1:
        return success_count

    # Step 2: Remaining shots — parallel img2img
    remaining = prompts[1:]
    print(f"   🎞️  剩余 {len(remaining)} 镜 — 并行 img2img (参考: 镜头 {first['shot_id']})")

    def _worker(shot_info):
        path = os.path.join(
            images_dir,
            shot_info.get("output_filename", f"shot_{shot_info['shot_id']}.png"),
        )
        if os.path.exists(path) and os.path.getsize(path) >= 10240:
            return shot_info["shot_id"], True, "skip"

        url = _generate_img2img(shot_info["prompt"], ref_url, size, cfg["model"], cfg["api_base"], cfg["api_key"])
        if url and _download_image(url, path):
            return shot_info["shot_id"], True, "ok"
        return shot_info["shot_id"], False, "failed"

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_worker, s): s for s in remaining}
        for future in as_completed(futures):
            try:
                shot_id, ok, reason = future.result()
                if ok:
                    print(f"   📸 镜头 {shot_id} ✅ ({reason})")
                    success_count += 1
                else:
                    print(f"   📸 镜头 {shot_id} ❌")
            except Exception as e:
                shot = futures[future]
                print(f"   📸 镜头 {shot['shot_id']} ❌ ({e})")

    return success_count
