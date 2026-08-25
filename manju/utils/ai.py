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


_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(?P<prefix>['\"]?(?:api[_-]?key|access[_-]?key|secret|token|"
    r"access[_-]?token|refresh[_-]?token|password|passwd|authorization|"
    r"signature|sig)['\"]?\s*[:=]\s*['\"]?)(?P<value>[^'\"\s,;&}]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;\]}\)]+")
_SIGNED_QUERY_RE = re.compile(
    r"(?i)([?&](?:sig|signature|x-amz-signature|x-goog-signature|"
    r"token|access_token|api_key|apikey)=)[^&#\s]+"
)


def redact_sensitive_text(text: object, secrets: tuple[object, ...] = ()) -> str:
    """Redact credentials from provider errors before they reach stderr/artifacts."""
    value = str(text)
    for secret in secrets:
        candidate = str(secret or "")
        if candidate:
            value = value.replace(candidate, "[REDACTED]")
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    value = _SIGNED_QUERY_RE.sub(r"\1[REDACTED]", value)
    value = _SENSITIVE_FIELD_RE.sub(r"\g<prefix>[REDACTED]", value)
    return value


class LLMCallError(RuntimeError):
    """Base class for provider outcomes that must not be represented by None."""

    kind = "provider_error"
    dispatched = False

    def __init__(self, message: str, *, detail: object = ""):
        self.detail = redact_sensitive_text(detail or message)
        super().__init__(redact_sensitive_text(message))


class LLMRequestNotDispatched(LLMCallError):
    """No provider request was sent, normally because configuration is absent."""

    kind = "not_dispatched"
    dispatched = False


class LLMResponseEmpty(LLMCallError):
    """The provider returned a successful HTTP response without usable text."""

    kind = "empty_response"
    dispatched = True


class LLMOutcomeUnknown(LLMCallError):
    """The request handoff happened, but its final provider outcome is unknown."""

    kind = "outcome_unknown"
    dispatched = True


class LLMHTTPError(LLMCallError):
    """The provider returned a known non-success HTTP status."""

    kind = "http_error"
    dispatched = True


class LLMResponseInvalid(LLMCallError):
    """The provider completed an HTTP response that was not usable JSON."""

    kind = "invalid_response"
    dispatched = True


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
        first = choices[0] if isinstance(choices[0], dict) else {}
        content = first.get("message", {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("text") is not None
            ]
            return "".join(parts)
    output_text = result.get("output_text")
    if isinstance(output_text, str):
        return output_text
    return None


def call_llm(system_prompt: str, user_content: str,
             max_tokens: int = 16000, temperature: float = 0.4,
             retries: int = 2, timeout: int = 180) -> str | None:
    """Call an OpenAI-compatible endpoint with typed provider outcomes.

    ``None`` is retained only in the return annotation for compatibility with
    older callers; configured calls either return non-empty text or raise a
    typed ``LLMCallError``.  In particular, an empty successful response is
    not interchangeable with a missing configuration or an uncertain request.
    """
    api_url, model, api_key = get_ai_config()
    if not api_key or not api_url:
        message = (
            "未配置LLM API (设置 LLM_API_KEY + LLM_API_BASE + LLM_MODEL)"
        )
        print(f"   ⚠ {message}", file=sys.stderr)
        raise LLMRequestNotDispatched(message)

    if not model:
        message = "LLM model 未配置 (设置 LLM_MODEL)"
        print(f"   ⚠ {message}", file=sys.stderr)
        raise LLMRequestNotDispatched(message)

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
    retries = max(0, int(retries))
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode(errors="replace")
            try:
                result = json.loads(body)
            except (json.JSONDecodeError, TypeError, UnicodeError) as exc:
                message = "LLM returned an invalid JSON response"
                safe_detail = redact_sensitive_text(body[:500], (api_key,))
                print(f"   ⚠ {message}: {safe_detail}", file=sys.stderr)
                raise LLMResponseInvalid(message, detail=safe_detail) from exc
            text = _extract_llm_text(result)
            if isinstance(text, str) and text.strip():
                return text
            message = "LLM 响应缺少文本内容"
            print(f"   ⚠ {message}", file=sys.stderr)
            raise LLMResponseEmpty(message)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode(errors="replace")[:500]
            except Exception:
                body = ""
            safe_body = redact_sensitive_text(body, (api_key,))
            retryable = e.code == 429 or e.code >= 500
            print(f"   ⚠ LLM HTTP {e.code}: {safe_body}", file=sys.stderr)
            if not retryable or attempt >= retries:
                message = f"LLM HTTP {e.code}"
                raise LLMHTTPError(message, detail=safe_body) from e
        except urllib.error.URLError as e:
            safe_reason = redact_sensitive_text(e.reason, (api_key,))
            print(f"   ⚠ LLM 网络错误: {safe_reason}", file=sys.stderr)
            if attempt >= retries:
                message = "LLM request outcome is unknown after network error"
                raise LLMOutcomeUnknown(message, detail=safe_reason) from e
        except (TimeoutError, OSError) as e:
            safe_error = redact_sensitive_text(e, (api_key,))
            print(f"   ⚠ LLM 调用失败: {safe_error}", file=sys.stderr)
            if attempt >= retries:
                message = "LLM request outcome is unknown after transport error"
                raise LLMOutcomeUnknown(message, detail=safe_error) from e
        wait = 2 ** attempt
        print(f"   ↻ {wait}s 后重试 ({attempt + 1}/{retries})")
        time.sleep(wait)
    raise LLMOutcomeUnknown("LLM request outcome is unknown")


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
