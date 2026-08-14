"""Provider-neutral M2.2 transport contracts.

This module deliberately contains no configured provider credentials and never
selects a network endpoint from a project file.  A caller must explicitly
construct a provider with an approved endpoint and artifact-origin allowlist.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from manju.production.models import ProductionError, ReasonCode


def _fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ProviderCapabilities:
    """Safety properties a paid provider must explicitly declare.

    Automatic crash recovery is safe only when submission is idempotent and
    returns a durable async job whose reconciliation is read-only.  A synchronous
    image endpoint that creates an image from ``reconcile()`` is deliberately not
    a ``VisualProvider`` suitable for ProductionRun automation.
    """

    submit_idempotent: bool
    async_job: bool
    reconcile_read_only: bool
    verified_cost: bool

    @property
    def automatic_recovery_safe(self) -> bool:
        return self.submit_idempotent and self.async_job and self.reconcile_read_only


def _origin(value: str, *, allow_insecure: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value)
    allowed_schemes = {"https", "http"} if allow_insecure else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.netloc or parsed.username or parsed.password:
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider URL is invalid")
    return f"{parsed.scheme}://{parsed.netloc}".lower()


@dataclass(frozen=True)
class ProviderObservation:
    """A normalized observation returned from a submitted provider operation."""

    outcome: str
    provider_job_id: str
    result_fingerprint: str = ""
    artifact_bytes: bytes = b""
    artifact_media_type: str = ""
    actual_amount: str = "0"
    currency: str = "USD"
    usage: dict[str, str] | None = None
    cost_status: str = "final"
    cost_source: str = "provider_response"

    def __post_init__(self) -> None:
        if self.outcome not in {"succeeded", "failed", "outcome_unknown"} or not self.provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider observation is invalid")
        if self.cost_status not in {"final", "unknown"} or self.cost_source not in {"provider_response", "provider_ledger", "test_fixture", "unknown"}:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider cost status is invalid")
        if self.cost_status == "final" and (not self.actual_amount.isdigit() or not self.currency):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider amount is invalid")
        if self.cost_status == "unknown" and (self.outcome != "outcome_unknown" or self.actual_amount or self.currency or self.cost_source != "unknown"):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider unknown cost is invalid")
        if self.usage is not None and not all(isinstance(key, str) and isinstance(value, str) for key, value in self.usage.items()):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider usage is invalid")
        if self.outcome == "succeeded":
            if not self.artifact_bytes or not self.artifact_media_type or self.result_fingerprint != _fingerprint(self.artifact_bytes):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider artifact commitment is invalid")
        elif self.artifact_bytes or self.result_fingerprint or self.artifact_media_type:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "non-success observation has an artifact")

    @property
    def settled_usage(self) -> dict[str, str]:
        return {
            **(self.usage or {}),
            "actual_amount": self.actual_amount if self.cost_status == "final" else "unknown",
            "currency": self.currency if self.cost_status == "final" else "unknown",
            "cost_status": self.cost_status,
            "cost_source": self.cost_source,
        }


@runtime_checkable
class VisualProvider(Protocol):
    """Only the adapter may invoke this interface; Service owns authorization."""

    def submit(self, operation_id: str, *, idempotency_key: str, request: dict[str, Any]) -> str: ...

    def reconcile(self, provider_job_id: str) -> ProviderObservation: ...


class VisualProviderRegistry:
    """Process-owned binding from a signed profile name to a provider instance.

    Project files carry only the profile name.  Endpoint, artifact allowlist and
    credentials remain in the process that constructs this registry, so a
    project cannot redirect a paid call by changing its own JSON.
    """

    def __init__(self, providers: Mapping[str, VisualProvider]):
        if not isinstance(providers, Mapping) or not providers:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "visual provider registry is empty")
        normalized: dict[str, VisualProvider] = {}
        for profile, provider in providers.items():
            if not isinstance(profile, str) or not profile or not isinstance(provider, VisualProvider):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "visual provider registry is invalid")
            normalized[profile] = provider
        self._providers = normalized

    def resolve(self, profile: str) -> VisualProvider:
        if not isinstance(profile, str) or not profile or profile not in self._providers:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "signed visual provider profile is unavailable")
        return self._providers[profile]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(request.full_url, code, "provider redirects are not allowed", headers, fp)


class HttpJsonVisualProvider:
    """Generic JSON-over-HTTP provider transport, tested only against local HTTP.

    The remote contract is intentionally small:
    POST ``/operations`` returns ``{"provider_job_id": "..."}`` and
    GET ``/operations/{job_id}`` returns a normalized observation with a
    same-origin or explicitly allowlisted artifact URL.
    """

    capabilities = ProviderCapabilities(True, True, True, True)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        allowed_artifact_origins: tuple[str, ...],
        timeout_seconds: float = 15.0,
        max_artifact_bytes: int = 20 * 1024 * 1024,
        allow_insecure_http: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._base_origin = _origin(self.base_url, allow_insecure=allow_insecure_http)
        self._api_key = api_key
        self._allowed_origins = {_origin(item, allow_insecure=allow_insecure_http) for item in allowed_artifact_origins}
        if not self._api_key or not self._allowed_origins or timeout_seconds <= 0 or max_artifact_bytes < 1:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider configuration is invalid")
        self.timeout_seconds = timeout_seconds
        self.max_artifact_bytes = max_artifact_bytes
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "provider transport failed") from exc
        if not isinstance(value, dict):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider response is not an object")
        return value

    def submit(self, operation_id: str, *, idempotency_key: str, request: dict[str, Any] | None = None) -> str:
        if not operation_id or not idempotency_key:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider idempotency binding is missing")
        if request is None:
            request = {}
        if not isinstance(request, dict):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider request descriptor is invalid")
        payload = {"operation_id": operation_id, **({"request": request} if request else {})}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/operations", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._api_key,
                "Idempotency-Key": idempotency_key,
            },
        )
        value = self._request_json(request)
        job_id = value.get("provider_job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider response has no job id")
        return job_id

    def _fetch_artifact(self, *, url: str, media_type: str, size: int, fingerprint: str) -> bytes:
        if _origin(url, allow_insecure=self._base_origin.startswith("http://")) not in self._allowed_origins:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact origin is not allowlisted")
        # Artifact URLs are frequently pre-signed CDN URLs.  Never forward the
        # provider API credential to a separate download origin.
        request = urllib.request.Request(url)
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get_content_type()
                declared_length = response.headers.get("Content-Length")
                if content_type != media_type or (declared_length and (not declared_length.isdigit() or int(declared_length) != size)):
                    raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact metadata does not match")
                data = response.read(self.max_artifact_bytes + 1)
        except ProductionError:
            raise
        except OSError as exc:
            raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "artifact download failed") from exc
        if len(data) != size or len(data) > self.max_artifact_bytes or _fingerprint(data) != fingerprint:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "artifact bytes do not match commitment")
        return data

    def reconcile(self, provider_job_id: str) -> ProviderObservation:
        if not provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider job id is missing")
        encoded_job = urllib.parse.quote(provider_job_id, safe="")
        request = urllib.request.Request(
            self.base_url + "/operations/" + encoded_job,
            headers={"Authorization": "Bearer " + self._api_key},
        )
        value = self._request_json(request)
        outcome = value.get("outcome")
        if outcome not in {"succeeded", "failed", "outcome_unknown"} or value.get("provider_job_id") != provider_job_id:
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider response job binding is invalid")
        usage = value.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider usage is invalid")
        normalized_usage = {str(key): str(item) for key, item in (usage or {}).items()}
        cost_status = value.get("cost_status", "final")
        if cost_status == "unknown":
            if outcome != "outcome_unknown" or "actual_amount" in value or "currency" in value:
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider unknown cost response is invalid")
            return ProviderObservation(
                outcome=outcome, provider_job_id=provider_job_id, usage=normalized_usage,
                actual_amount="", currency="", cost_status="unknown", cost_source="unknown",
            )
        if cost_status != "final" or not isinstance(value.get("actual_amount"), (str, int)) or not isinstance(value.get("currency"), str):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider final cost response is invalid")
        actual_amount = str(value["actual_amount"])
        currency = value["currency"]
        if outcome == "failed":
            return ProviderObservation(
                outcome="failed", provider_job_id=provider_job_id, actual_amount=actual_amount,
                currency=currency, usage=normalized_usage, cost_source="provider_response",
            )
        required = ("provider_job_id", "result_fingerprint", "artifact_url", "artifact_media_type", "artifact_size")
        if outcome != "succeeded" or any(key not in value for key in required):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider success response is invalid")
        artifact_url = value["artifact_url"]
        media_type = value["artifact_media_type"]
        size = value["artifact_size"]
        fingerprint = value["result_fingerprint"]
        if not isinstance(artifact_url, str) or not isinstance(media_type, str) or not isinstance(size, int) or size < 1 or not isinstance(fingerprint, str):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "provider artifact receipt is invalid")
        return ProviderObservation(
            outcome="succeeded", provider_job_id=provider_job_id, result_fingerprint=fingerprint,
            artifact_bytes=self._fetch_artifact(url=artifact_url, media_type=media_type, size=size, fingerprint=fingerprint),
            artifact_media_type=media_type, actual_amount=actual_amount, currency=currency, usage=normalized_usage,
            cost_source="provider_response",
        )
    capabilities: ProviderCapabilities
