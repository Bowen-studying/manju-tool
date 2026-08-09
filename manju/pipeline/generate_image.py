"""Text/image-to-image generation with OpenAI-compatible and JSON APIs."""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from manju.utils.config import load_manju_env
from manju.utils.runtime import (
    atomic_write_json,
    content_fingerprint,
    decode_data_url,
    file_data_url,
    join_api_url,
    read_json,
    safe_filename,
)

DEFAULT_SIZE = "1024x1024"


def _bounded_env_int(env: dict, key: str, default: int,
                     minimum: int, maximum: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _get_image_config() -> dict:
    env = load_manju_env()
    return {
        "api_base": env.get("MANJU_IMAGE_API_BASE", ""),
        "api_key": env.get("MANJU_IMAGE_API_KEY", ""),
        "model": env.get("MANJU_IMAGE_MODEL", ""),
        "timeout_seconds": _bounded_env_int(
            env, "MANJU_IMAGE_TIMEOUT_SECONDS", 300, 30, 1800,
        ),
        "download_timeout_seconds": _bounded_env_int(
            env, "MANJU_IMAGE_DOWNLOAD_TIMEOUT_SECONDS", 120, 15, 600,
        ),
    }


def _validate_size(size: str) -> str:
    parts = str(size).lower().split("x")
    if len(parts) != 2:
        return DEFAULT_SIZE
    try:
        width, height = (int(part) for part in parts)
    except ValueError:
        return DEFAULT_SIZE
    width = max(64, (width // 16) * 16)
    height = max(64, (height // 16) * 16)
    return f"{width}x{height}"


def _extract_image_reference(result: dict) -> str | None:
    candidates = result.get("data")
    if not isinstance(candidates, list):
        candidates = [result]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            return url
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            return f"data:image/png;base64,{encoded}"
    return None


def _request_json(url: str, payload: dict, api_key: str, timeout: int = 300,
                  retries: int = 2) -> dict | None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            print(f"   ❌ 生图API HTTP {exc.code}: {body}", file=sys.stderr)
            if exc.code != 429 and exc.code < 500:
                return None
        except urllib.error.URLError as exc:
            print(f"   ❌ 生图网络错误: {exc.reason}", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"   ❌ 生图响应错误: {exc}", file=sys.stderr)
            return None
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def _generate_txt2img(prompt: str, size: str = DEFAULT_SIZE, model: str = "",
                      api_base: str = "", api_key: str = "",
                      timeout: int = 300) -> str | None:
    if not api_key or not api_base:
        print("   ⚠ 生图API配置不完整", file=sys.stderr)
        return None
    result = _request_json(
        join_api_url(api_base, "images/generations"),
        {"model": model, "prompt": prompt, "size": _validate_size(size), "n": 1},
        api_key, timeout=timeout,
    )
    if not result:
        return None
    if result.get("error"):
        print(f"   ❌ API错误: {result['error']}", file=sys.stderr)
        return None
    reference = _extract_image_reference(result)
    if not reference:
        print("   ⚠ 响应中未找到 url 或 b64_json", file=sys.stderr)
    return reference


def _generate_img2img(prompt: str, ref_url: str, size: str = DEFAULT_SIZE,
                      model: str = "", api_base: str = "", api_key: str = "",
                      timeout: int = 300) -> str | None:
    """JSON img2img for providers accepting URL/data-URL references."""
    if not api_key or not api_base:
        return None
    result = _request_json(
        join_api_url(api_base, "images/generations"),
        {"model": model, "prompt": prompt, "size": _validate_size(size),
         "n": 1, "image": ref_url},
        api_key, timeout=timeout,
    )
    return _extract_image_reference(result or {})


def _multipart_body(fields: dict[str, str], file_path: str) -> tuple[bytes, str]:
    boundary = f"manju-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"), b"\r\n",
        ])
    filename = safe_filename(os.path.basename(file_path), "reference.png")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
    ])
    with open(file_path, "rb") as handle:
        chunks.append(handle.read())
    chunks.extend([b"\r\n", f"--{boundary}--\r\n".encode()])
    return b"".join(chunks), boundary


def _generate_img2img_local(prompt: str, file_path: str, size: str, model: str,
                            api_base: str, api_key: str, retries: int = 2,
                            timeout: int = 300) -> str | None:
    fields = {"prompt": prompt, "size": _validate_size(size), "n": "1"}
    if model:
        fields["model"] = model
    body, boundary = _multipart_body(fields, file_path)
    request = urllib.request.Request(
        join_api_url(api_base, "images/edits"), data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _extract_image_reference(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            print(f"   ❌ 图生图API HTTP {exc.code}: {detail}", file=sys.stderr)
            if exc.code != 429 and exc.code < 500:
                return None
        except urllib.error.URLError as exc:
            print(f"   ❌ 图生图网络错误: {exc.reason}", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"   ❌ 图生图失败: {exc}", file=sys.stderr)
            return None
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def _download_image(reference: str, output_path: str, max_retries: int = 3,
                    overwrite: bool = False, timeout: int = 120) -> bool:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if not overwrite and os.path.isfile(output_path) and os.path.getsize(output_path) >= 512:
        return True
    embedded = decode_data_url(reference)
    if embedded is not None:
        with open(output_path, "wb") as handle:
            handle.write(embedded)
        return bool(embedded)
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(reference), timeout=timeout) as response:
                content = response.read()
            if not content:
                raise OSError("empty image response")
            with open(output_path, "wb") as handle:
                handle.write(content)
            return True
        except urllib.error.HTTPError as exc:
            print(f"   ❌ 图片下载 HTTP {exc.code}", file=sys.stderr)
            if exc.code != 429 and exc.code < 500:
                return False
        except (urllib.error.URLError, OSError) as exc:
            print(f"   ⚠ 图片下载失败: {exc}", file=sys.stderr)
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    return False


def _cache_path(output_path: str) -> str:
    return f"{output_path}.manju.json"


def _cache_matches(output_path: str, fingerprint: str) -> bool:
    metadata = read_json(_cache_path(output_path))
    return (os.path.isfile(output_path) and os.path.getsize(output_path) >= 512
            and metadata is not None and metadata.get("fingerprint") == fingerprint)


def _record_cache(output_path: str, fingerprint: str, **metadata: object) -> None:
    atomic_write_json(_cache_path(output_path), {"fingerprint": fingerprint, **metadata})


def get_image_provider_capabilities() -> dict:
    """Return a small, serializable capability description without credentials."""
    env = load_manju_env()
    mode = str(env.get("MANJU_IMAGE_REFERENCE_MODE", "single")).strip().lower()
    if mode not in {"single", "multi"}:
        mode = "single"
    try:
        maximum = int(env.get("MANJU_IMAGE_MAX_REFERENCES", "1" if mode == "single" else "8"))
    except (TypeError, ValueError):
        maximum = 1 if mode == "single" else 8
    return {
        "reference_mode": mode,
        "max_references": max(1, maximum),
        "multi_reference_field": str(env.get("MANJU_IMAGE_MULTI_REFERENCE_FIELD", "images")),
    }


def generate_image_with_references(
    prompt: str,
    output_path: str,
    reference_paths: list[str] | None = None,
    size: str = DEFAULT_SIZE,
) -> str | None:
    """Generate an exact output path through the configured provider adapter.

    The default OpenAI-compatible adapter supports one local reference via
    ``images/edits``. Providers declaring ``MANJU_IMAGE_REFERENCE_MODE=multi``
    receive data URLs in the configurable JSON reference field.
    """
    cfg = _get_image_config()
    if not cfg["api_key"] or not cfg["api_base"]:
        print("❌ 未配置生图API", file=sys.stderr)
        return None
    references = [os.path.abspath(path) for path in (reference_paths or []) if os.path.isfile(path)]
    capabilities = get_image_provider_capabilities()
    normalized_size = _validate_size(size)
    fingerprint = content_fingerprint(
        prompt,
        [(path, os.path.getsize(path), int(os.path.getmtime(path))) for path in references],
        normalized_size,
        cfg["model"],
        capabilities,
    )
    if _cache_matches(output_path, fingerprint):
        return output_path

    generated: str | None
    if references and capabilities["reference_mode"] == "multi":
        limited = references[:capabilities["max_references"]]
        payload = {
            "model": cfg["model"], "prompt": prompt,
            "size": normalized_size, "n": 1,
            capabilities["multi_reference_field"]: [file_data_url(path) for path in limited],
        }
        generated = _extract_image_reference(_request_json(
            join_api_url(cfg["api_base"], "images/generations"), payload, cfg["api_key"],
            timeout=cfg.get("timeout_seconds", 300),
        ) or {})
    elif references:
        generated = _generate_img2img_local(
            prompt, references[0], normalized_size,
            cfg["model"], cfg["api_base"], cfg["api_key"],
            timeout=cfg.get("timeout_seconds", 300),
        )
    else:
        generated = _generate_txt2img(
            prompt, normalized_size, cfg["model"], cfg["api_base"], cfg["api_key"],
            timeout=cfg.get("timeout_seconds", 300),
        )
    if not generated or not _download_image(
        generated, output_path, overwrite=True, timeout=cfg.get("download_timeout_seconds", 120)
    ):
        return None
    _record_cache(
        output_path, fingerprint, prompt=prompt,
        references=[os.path.basename(path) for path in references],
        model=cfg["model"], size=normalized_size, capabilities=capabilities,
    )
    return output_path


def run_image(prompt: str, image_path: str = "", size: str = DEFAULT_SIZE,
              output_dir: str | None = None, output_name: str = "") -> str | None:
    cfg = _get_image_config()
    if not cfg["api_key"] or not cfg["api_base"]:
        print("❌ 未配置生图API", file=sys.stderr)
        return None
    now = datetime.now()
    output_dir = output_dir or os.path.join(
        os.getcwd(), "manju-output", f"{now.year}.{now.month}.{now.day}", "images")
    os.makedirs(output_dir, exist_ok=True)
    normalized_size = _validate_size(size)
    fingerprint = content_fingerprint(prompt, image_path, normalized_size, cfg["model"])
    name = safe_filename(output_name or prompt[:40], "image")
    output_path = os.path.join(output_dir, f"{name}.png")
    if _cache_matches(output_path, fingerprint):
        print(f"   ⏭ 内容未变化: {output_path}")
        return output_path

    if image_path and os.path.isfile(image_path):
        reference = _generate_img2img_local(
            prompt, image_path, normalized_size, cfg["model"], cfg["api_base"], cfg["api_key"],
            timeout=cfg.get("timeout_seconds", 300))
    elif image_path.startswith(("http://", "https://", "data:")):
        reference = _generate_img2img(
            prompt, image_path, normalized_size, cfg["model"], cfg["api_base"], cfg["api_key"],
            timeout=cfg.get("timeout_seconds", 300))
    elif image_path:
        print(f"❌ 参考图片不存在: {image_path}", file=sys.stderr)
        return None
    else:
        reference = _generate_txt2img(
            prompt, normalized_size, cfg["model"], cfg["api_base"], cfg["api_key"],
            timeout=cfg.get("timeout_seconds", 300))
    if not reference or not _download_image(
        reference, output_path, overwrite=True, timeout=cfg.get("download_timeout_seconds", 120)
    ):
        return None
    _record_cache(output_path, fingerprint, prompt=prompt, reference=image_path,
                  model=cfg["model"], size=normalized_size)
    print(f"   ✅ 已保存: {output_path}")
    return output_path


def run_batch_images(prompts: list[dict], output_dir: str,
                     size: str = DEFAULT_SIZE) -> int:
    """Generate one reference per scene/group, then edit shots within that group."""
    cfg = _get_image_config()
    if not cfg["api_key"] or not cfg["api_base"] or not prompts:
        return 0
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    normalized_size = _validate_size(size)

    def target(item: dict) -> tuple[str, str]:
        filename = safe_filename(
            item.get("output_filename") or f"shot_{item.get('shot_id', 'unknown')}.png",
            "shot.png", 120,
        )
        if not filename.lower().endswith(".png"):
            filename += ".png"
        fingerprint = content_fingerprint(item.get("prompt", ""), normalized_size, cfg["model"])
        return os.path.join(images_dir, filename), fingerprint

    groups: dict[str, list[dict]] = {}
    for item in prompts:
        groups.setdefault(str(item.get("reference_group", "default")), []).append(item)

    def generate_group(items: list[dict]) -> int:
        first = items[0]
        first_path, first_fp = target(first)
        reference: str | None = None
        if _cache_matches(first_path, first_fp):
            success = 1
        else:
            reference = _generate_txt2img(
                first["prompt"], normalized_size, cfg["model"], cfg["api_base"], cfg["api_key"],
                timeout=cfg.get("timeout_seconds", 300))
            success = int(bool(reference and _download_image(
                reference, first_path, overwrite=True,
                timeout=cfg.get("download_timeout_seconds", 120),
            )))
            if success:
                _record_cache(first_path, first_fp, prompt=first["prompt"])
        if not success:
            print(f"❌ 分组 {first.get('reference_group', 'default')} 参考图失败", file=sys.stderr)
            return 0
        if len(items) == 1:
            return 1
        reference_lock = threading.Lock()

        def worker(item: dict) -> bool:
            path, fingerprint = target(item)
            if _cache_matches(path, fingerprint):
                return True
            generated = _generate_img2img_local(
                item["prompt"], first_path, normalized_size, cfg["model"],
                cfg["api_base"], cfg["api_key"], timeout=cfg.get("timeout_seconds", 300),
            )
            if not generated:
                nonlocal reference
                with reference_lock:
                    if not reference:
                        reference = _generate_txt2img(
                            first["prompt"], normalized_size, cfg["model"],
                            cfg["api_base"], cfg["api_key"], timeout=cfg.get("timeout_seconds", 300))
                if reference:
                    generated = _generate_img2img(
                        item["prompt"], reference, normalized_size, cfg["model"],
                        cfg["api_base"], cfg["api_key"], timeout=cfg.get("timeout_seconds", 300),
                    )
            if generated and _download_image(
                generated, path, overwrite=True,
                timeout=cfg.get("download_timeout_seconds", 120),
            ):
                _record_cache(path, fingerprint, prompt=item["prompt"])
                return True
            return False

        with ThreadPoolExecutor(max_workers=min(4, len(items) - 1)) as executor:
            futures = [executor.submit(worker, item) for item in items[1:]]
            success += sum(1 for future in as_completed(futures) if future.result())
        return success

    return sum(generate_group(items) for items in groups.values())


def _read_batch_lines(file_path: str) -> list[str]:
    with open(file_path, "r", encoding="utf-8") as handle:
        return [line.strip() for line in handle
                if line.strip() and not line.lstrip().startswith("#")]


def count_batch_lines(file_path: str) -> int:
    try:
        return len(_read_batch_lines(file_path))
    except OSError:
        return 0


def run_batch_from_file(file_path: str, output_dir: str | None = None,
                        size: str = DEFAULT_SIZE) -> int:
    try:
        lines = _read_batch_lines(file_path)
    except (OSError, UnicodeError) as exc:
        print(f"❌ 读取文件失败: {exc}", file=sys.stderr)
        return 0
    if not lines:
        return 0
    output_dir = output_dir or os.path.join(os.getcwd(), "manju-output", "images")
    prompts = [{
        "shot_id": str(index),
        "prompt": prompt,
        "output_filename": f"{index:03d}_{safe_filename(prompt[:30], 'image')}_{content_fingerprint(prompt, length=8)}.png",
    } for index, prompt in enumerate(lines, 1)]
    return run_batch_images(prompts, output_dir, size)
