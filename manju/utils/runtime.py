"""Runtime helpers for safe paths, fingerprints, endpoints, and atomic state."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime
from typing import Any


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(value: object, fallback: str = "output", max_length: int = 80) -> str:
    """Return one portable filename component, never a path."""
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    if not text:
        text = fallback
    if text.upper() in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text[:max_length].rstrip(" .") or fallback


def content_fingerprint(*values: object, length: int = 16) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def join_api_url(base: str, endpoint: str) -> str:
    """Join an API root or already-complete endpoint without duplicating /v1."""
    base = (base or "").strip().rstrip("/")
    endpoint = endpoint.strip("/")
    if not base:
        return ""
    normalized = "/" + endpoint
    if base.lower().endswith(normalized.lower()):
        return base
    return f"{base}/{endpoint}"


def file_data_url(path: str) -> str:
    """Encode a local reference file as a data URL for JSON-compatible APIs."""
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def decode_data_url(value: str) -> bytes | None:
    if not isinstance(value, str) or not value.startswith("data:") or "," not in value:
        return None
    header, encoded = value.split(",", 1)
    if ";base64" not in header:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None


def read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def atomic_write_json(path: str, value: Any) -> None:
    """Write JSON atomically so an interruption cannot leave a half file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".manju-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def available_path(path: str) -> str:
    """Return path or a timestamped sibling so an existing deliverable is preserved."""
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = f"{stem}_{suffix}{extension}"
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{stem}_{suffix}_{counter}{extension}"
        counter += 1
    return candidate
