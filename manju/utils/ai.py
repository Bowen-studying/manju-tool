"""Shared AI/LLM utilities — config loading, API calls, JSON parsing.

Used by adapt.py, create.py, storyboard.py, and voice.py to avoid
duplicating the same credential-loading and response-parsing logic
across four modules.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

from manju.utils.config import load_manju_env
from manju.utils.runtime import join_api_url


# ── Cached AI config ───────────────────────────────────────────────────────────

_AI_CONFIG = None


def reset_ai_config() -> None:
    """Clear cached credentials for long-running processes and tests."""
    global _AI_CONFIG
    _AI_CONFIG = None


def get_ai_config():
    """Return (api_url, model, api_key) from environment or ~/.manju.env.

    LLM_API_KEY, LLM_API_BASE and LLM_MODEL configure a neutral,
    OpenAI-compatible endpoint.

    Results are cached after the first successful lookup.
    Returns (None, None, None) if no provider is configured.
    """
    global _AI_CONFIG
    if _AI_CONFIG is not None:
        return _AI_CONFIG

    env_keys = load_manju_env()

    generic_key = env_keys.get("LLM_API_KEY", "")
    if generic_key:
        generic_base = env_keys.get("LLM_API_BASE", "")
        generic_model = env_keys.get("LLM_MODEL", "")
        if generic_base and generic_model:
            _AI_CONFIG = (join_api_url(generic_base, "chat/completions"), generic_model, generic_key)
            return _AI_CONFIG
        # A partial generic configuration is not usable.
        if not generic_base:
            print("   ⚠ LLM_API_KEY 已设置但 LLM_API_BASE 未配置", file=sys.stderr)
        if not generic_model:
            print("   ⚠ LLM_API_KEY 已设置但 LLM_MODEL 未配置", file=sys.stderr)

    # Do not cache a missing configuration; notebooks/services may set env later.
    return (None, None, None)


# ── LLM call ───────────────────────────────────────────────────────────────────

def _extract_llm_text(result: dict) -> str | None:
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [item.get("text", "") for item in content if isinstance(item, dict)]
            return "".join(parts) or None
    output_text = result.get("output_text")
    if isinstance(output_text, str):
        return output_text
    return None


def call_llm(system_prompt: str, user_content: str,
             max_tokens: int = 16000, temperature: float = 0.4,
             retries: int = 2, timeout: int = 180) -> str | None:
    """Generic LLM call via urllib. Returns response text or None on failure."""
    api_url, model, api_key = get_ai_config()
    if not api_key or not api_url:
        print("   ⚠ 未配置LLM API "
              "(设置 LLM_API_KEY + LLM_API_BASE + LLM_MODEL)", file=sys.stderr)
        return None

    if not model:
        print("   ⚠ LLM model 未配置 (设置 LLM_MODEL)", file=sys.stderr)
        return None

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()

    req = urllib.request.Request(api_url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
            text = _extract_llm_text(result)
            if text:
                return text
            print("   ⚠ LLM 响应缺少文本内容", file=sys.stderr)
            return None
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode(errors="replace")[:500]
            except Exception:
                body = ""
            retryable = e.code == 429 or e.code >= 500
            print(f"   ⚠ LLM HTTP {e.code}: {body}", file=sys.stderr)
            if not retryable or attempt >= retries:
                return None
        except urllib.error.URLError as e:
            print(f"   ⚠ LLM 网络错误: {e.reason}", file=sys.stderr)
            if attempt >= retries:
                return None
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            print(f"   ⚠ LLM 调用失败: {e}", file=sys.stderr)
            if attempt >= retries:
                return None
        wait = 2 ** attempt
        print(f"   ↻ {wait}s 后重试 ({attempt + 1}/{retries})")
        time.sleep(wait)
    return None


# ── JSON parsing ───────────────────────────────────────────────────────────────

def parse_json_response(response_text: str) -> dict | None:
    """Extract JSON dict from LLM response, handling ```json``` code blocks.

    Attempts, in order:
    1. Extract from ```json ... ``` fenced block
    2. Parse raw text as JSON
    3. Find outermost { ... } brace pair and parse that

    Returns the parsed dict, or None if all attempts fail.
    """
    if not response_text:
        return None

    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```",
                           response_text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1).strip()
    else:
        json_str = response_text.strip()

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        brace_match = re.search(r"\{.*\}", json_str, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

    print("   ⚠ 无法解析LLM响应为JSON", file=sys.stderr)
    return None
