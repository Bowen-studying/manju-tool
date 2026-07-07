"""Shared HTTP utilities — zero external deps (urllib only)."""

import json
import urllib.request
import urllib.error


def http_get_json(url: str, *, headers: dict | None = None, timeout: int = 15):
    """GET request → parsed JSON dict. Returns {} on any error."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError):
        return {}


def http_post_json(url: str, data: dict, *, headers: dict | None = None, timeout: int = 15):
    """POST request with JSON body → parsed JSON dict. Returns {} on any error."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError):
        return {}
