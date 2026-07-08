"""Shared AI/LLM utilities — config loading, API calls, JSON parsing.

Used by adapt.py, create.py, storyboard.py, and voice.py to avoid
duplicating the same credential-loading and response-parsing logic
across four modules.
"""

import json
import os
import re
import sys
import urllib.request

from manju.utils.config import load_manju_env


# ── Cached AI config ───────────────────────────────────────────────────────────

_AI_CONFIG = None


def get_ai_config():
    """Return (api_url, model, api_key) from environment or ~/.manju.env.

    Priority: LLM_API_KEY > provider-specific keys (DEEPSEEK_API_KEY, GLM_API_KEY).
    When using LLM_API_KEY, LLM_API_BASE and LLM_MODEL configure the endpoint.

    Results are cached after the first successful lookup.
    Returns (None, None, None) if no provider is configured.
    """
    global _AI_CONFIG
    if _AI_CONFIG is not None:
        return _AI_CONFIG

    env_keys = load_manju_env()

    # Priority 1: Generic LLM_API_KEY (takes priority over all provider-specific keys)
    generic_key = env_keys.get("LLM_API_KEY", "")
    if generic_key:
        generic_base = env_keys.get("LLM_API_BASE", "")
        generic_model = env_keys.get("LLM_MODEL", "")
        if generic_base and generic_model:
            _AI_CONFIG = (generic_base, generic_model, generic_key)
            return _AI_CONFIG
        # If LLM_API_KEY is set but base/model are missing, warn and fall through
        if not generic_base:
            print("   ⚠ LLM_API_KEY 已设置但 LLM_API_BASE 未配置", file=sys.stderr)
        if not generic_model:
            print("   ⚠ LLM_API_KEY 已设置但 LLM_MODEL 未配置", file=sys.stderr)

    # Priority 2: Provider-specific keys (backward compatibility)
    providers = [
        ("deepseek", "https://api.deepseek.com/v1/chat/completions",
         "deepseek-chat", "DEEPSEEK_API_KEY"),
        ("glm", "https://open.bigmodel.cn/api/paas/v4/chat/completions",
         "glm-4.5-air", "GLM_API_KEY"),
    ]
    for _name, url, model, key_env in providers:
        key = env_keys.get(key_env, "")
        if key:
            _AI_CONFIG = (url, model, key)
            return _AI_CONFIG

    _AI_CONFIG = (None, None, None)
    return _AI_CONFIG


# ── LLM call ───────────────────────────────────────────────────────────────────

def call_llm(system_prompt: str, user_content: str,
             max_tokens: int = 16000, temperature: float = 0.4) -> str | None:
    """Generic LLM call via urllib. Returns response text or None on failure."""
    api_url, model, api_key = get_ai_config()
    if not api_key or not api_url:
        print("   ⚠ 未配置LLM API "
              "(设置 LLM_API_KEY + LLM_API_BASE + LLM_MODEL, "
              "或 DEEPSEEK_API_KEY, 或 GLM_API_KEY)", file=sys.stderr)
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
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"]
    except urllib.error.URLError as e:
        print(f"   ⚠ LLM 网络错误: {e.reason}", file=sys.stderr)
        return None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        print(f"   ⚠ LLM HTTP {e.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"   ⚠ LLM 调用失败: {e}", file=sys.stderr)
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
