"""Process-owned production visual Provider profile resolution.

Profiles are supplied by deployment environment, never by a project contract.
The contract records only the selected profile name; endpoint, allowlist and
credential name remain outside the project directory.
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
from typing import Any, Mapping

from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import HttpJsonVisualProvider, VisualProviderRegistry


PROFILE_CONFIG_ENV = "MANJU_PRODUCTION_VISUAL_PROFILES_JSON"


def _invalid(message: str) -> ProductionError:
    return ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, message)


def _profiles_from_environment(environ: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    source = os.environ if environ is None else environ
    raw = source.get(PROFILE_CONFIG_ENV, "")
    if not raw:
        raise _invalid("visual provider profile configuration is unavailable")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid("visual provider profile configuration is invalid") from exc
    if not isinstance(value, dict) or not value:
        raise _invalid("visual provider profile configuration is invalid")
    profiles: dict[str, dict[str, Any]] = {}
    for name, config in value.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            raise _invalid("visual provider profile configuration is invalid")
        profiles[name] = dict(config)
    return profiles


def is_manual_sync_profile(required_profile: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """Read only the deployment declaration; never reads a provider credential."""
    if not isinstance(required_profile, str) or not required_profile:
        return False
    return _profiles_from_environment(environ).get(required_profile, {}).get("mode") == "manual_sync"


def resolve_manual_sync_profile(*, required_profile: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve the worker-only OpenAI-compatible synchronous transport profile.

    This deliberately does not construct a provider for the automatic runner.
    The API key is read only by the separately invoked worker process.
    """
    source = os.environ if environ is None else environ
    config = _profiles_from_environment(source).get(required_profile)
    allowed = {"mode", "base_url", "api_key_env", "timeout_seconds", "max_artifact_bytes"}
    if not isinstance(config, dict) or config.get("mode") != "manual_sync" or set(config) - allowed:
        raise _invalid("manual_sync provider profile is invalid")
    base_url, key_name = config.get("base_url"), config.get("api_key_env")
    parsed = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
    if (
        not isinstance(base_url, str) or parsed is None or parsed.scheme != "https" or not parsed.netloc
        or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
        or not isinstance(key_name, str) or not key_name
    ):
        raise _invalid("manual_sync provider profile is incomplete")
    timeout = config.get("timeout_seconds", 60.0)
    maximum = config.get("max_artifact_bytes", 20 * 1024 * 1024)
    if (
        not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120
        or not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 100 * 1024 * 1024
    ):
        raise _invalid("manual_sync provider limits are invalid")
    key = source.get(key_name, "")
    if not isinstance(key, str) or not key:
        raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "manual_sync provider credential is unavailable")
    return {"base_url": base_url.rstrip("/"), "api_key": key, "timeout_seconds": float(timeout), "max_artifact_bytes": maximum}


def resolve_visual_provider_registry(*, required_profile: str, environ: Mapping[str, str] | None = None) -> VisualProviderRegistry:
    """Build exactly the selected safe async profile from process configuration.

    A `manual_sync` declaration is deliberately rejected: a synchronous paid
    endpoint has no durable upstream job to reconcile, so this automatic runner
    may not dispatch it. Its use requires a separate human-operated process.
    """
    if not isinstance(required_profile, str) or not required_profile or required_profile == "mock":
        raise _invalid("a non-mock visual provider profile is required")
    source = os.environ if environ is None else environ
    config = _profiles_from_environment(source).get(required_profile)
    if config is None:
        raise _invalid("signed visual provider profile is unavailable")
    mode = config.get("mode")
    if mode == "manual_sync":
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "manual_sync providers cannot be dispatched by automatic ProductionRun")
    allowed = {"mode", "base_url", "api_key_env", "allowed_artifact_origins", "timeout_seconds", "max_artifact_bytes"}
    if mode != "async_http" or set(config) - allowed:
        raise _invalid("visual provider profile mode is invalid")
    base_url, key_name, origins = config.get("base_url"), config.get("api_key_env"), config.get("allowed_artifact_origins")
    if (
        not isinstance(base_url, str) or not isinstance(key_name, str) or not key_name
        or not isinstance(origins, list) or not origins or not all(isinstance(item, str) for item in origins)
    ):
        raise _invalid("visual provider profile is incomplete")
    api_key = source.get(key_name, "")
    if not isinstance(api_key, str) or not api_key:
        raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "visual provider credential is unavailable")
    timeout = config.get("timeout_seconds", 15.0)
    maximum = config.get("max_artifact_bytes", 20 * 1024 * 1024)
    if (
        not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 60
        or not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 100 * 1024 * 1024
    ):
        raise _invalid("visual provider profile limits are invalid")
    provider = HttpJsonVisualProvider(
        base_url=base_url, api_key=api_key, allowed_artifact_origins=tuple(origins),
        timeout_seconds=float(timeout), max_artifact_bytes=maximum,
    )
    return VisualProviderRegistry({required_profile: provider})
