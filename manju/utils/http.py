"""Shared HTTP utilities — zero external deps (urllib only).

For internal use by generate_*.py and other pipeline modules.
"""

import json
import sys
import urllib.request
import urllib.error


def http_get_json(url: str, *, headers: dict | None = None, timeout: int = 15) -> dict:
    """GET request → parsed JSON dict. Returns {} on any error.

    Errors are logged to stderr but never raised — callers that need to
    distinguish empty responses from failures should handle HTTP at a
    lower level.
    """
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"   ⚠ HTTP GET {url}: {e.code}", file=sys.stderr)
        return {}
    except urllib.error.URLError as e:
        print(f"   ⚠ HTTP GET {url}: {e.reason}", file=sys.stderr)
        return {}
    except (json.JSONDecodeError, OSError, Exception) as e:
        print(f"   ⚠ HTTP GET {url}: {e}", file=sys.stderr)
        return {}


def http_post_json(url: str, data: dict, *, headers: dict | None = None, timeout: int = 15) -> dict:
    """POST request with JSON body → parsed JSON dict. Returns {} on any error.

    Errors are logged to stderr but never raised.
    """
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        print(f"   ⚠ HTTP POST {url}: {e.code} {body}", file=sys.stderr)
        return {}
    except urllib.error.URLError as e:
        print(f"   ⚠ HTTP POST {url}: {e.reason}", file=sys.stderr)
        return {}
    except (json.JSONDecodeError, OSError, Exception) as e:
        print(f"   ⚠ HTTP POST {url}: {e}", file=sys.stderr)
        return {}
