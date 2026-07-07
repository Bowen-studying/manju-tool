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


# ── Cached AI config ───────────────────────────────────────────────────────────

_AI_CONFIG = None


def get_ai_config():
    """Return (api_url, model, api_key) from environment or ~/.manju.env.

    Results are cached after the first successful lookup.
    Returns (None, None, None) if no provider is configured.
    """
    global _AI_CONFIG
    if _AI_CONFIG is not None:
        return _AI_CONFIG

    env_file = os.path.join(os.path.expanduser("~"), ".manju.env")
    providers = [
        ("deepseek", "https://api.deepseek.com/v1/chat/completions",
         "deepseek-chat", "DEEPSEEK_API_KEY"),
        ("glm", "https://open.bigmodel.cn/api/paas/v4/chat/completions",
         "glm-4.5-air", "GLM_API_KEY"),
    ]
    env_keys = {}
    try:
        with open(env_file) as f:
            for line in f:
                for _, _, _, key_env in providers:
                    if line.startswith(f"{key_env}="):
                        env_keys[key_env] = line.split("=", 1)[1].strip()
    except Exception:
        pass
    for _, _, _, key_env in providers:
        if key_env not in env_keys:
            v = os.environ.get(key_env, "")
            if v:
                env_keys[key_env] = v
    for _, url, model, key_env in providers:
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
        print("   ⚠ No AI provider configured "
              "(set DEEPSEEK_API_KEY or GLM_API_KEY)", file=sys.stderr)
        return None

    payload = json.dumps({
        "model": model or "",
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
    except Exception as e:
        print(f"   ⚠ LLM call failed: {e}", file=sys.stderr)
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

    print("   ⚠ Failed to parse LLM response as JSON", file=sys.stderr)
    return None
