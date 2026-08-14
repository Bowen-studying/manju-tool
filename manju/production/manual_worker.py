"""Separate, deliberately one-shot worker for a synchronous provider.

The worker accepts a signed dispatch file.  Its default fixture executor never
uses the network; real transports must be supplied by a controlled deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Any

from manju.production.events import sign_payload
from manju.production.manual_operations import ManualDispatchPackage, ManualResultPackage, sha256_file
from manju.production.models import ProductionError, ReasonCode, utc_now
from manju.utils.runtime import atomic_write_bytes, atomic_write_json, read_json


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(request.full_url, code, "provider redirects are not allowed", headers, fp)


def _load_key(key_id: str) -> bytes:
    configured = os.environ.get("MANJU_PRODUCTION_HMAC_KEY", "")
    if not configured:
        raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "worker signing key is unavailable")
    return configured.encode("utf-8")


def _claim_path(state_dir: str, token: str) -> str:
    return os.path.join(os.path.abspath(state_dir), "claims", hashlib.sha256(token.encode("utf-8")).hexdigest() + ".json")


def claim_dispatch(dispatch: ManualDispatchPackage, *, state_dir: str) -> str:
    """Atomically create a durable local claim. Existing claims are never reused."""
    path = _claim_path(state_dir, dispatch.claim_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    value = {"schema_version": "1", "package_id": dispatch.package_id, "operation_id": dispatch.operation_id,
             "claim_token": dispatch.claim_token, "state": "claimed", "claimed_at": utc_now(), "pid": os.getpid()}
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "dispatch was already claimed on this worker") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return path


def _mark_started(path: str) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict) or value.get("state") != "claimed":
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "worker claim is not eligible for dispatch")
    value["state"] = "dispatch_started"
    value["started_at"] = utc_now()
    atomic_write_json(path, value)
    return value


def _mark_result_written(path: str, *, result: ManualResultPackage, result_path: str) -> None:
    """Record the locally signed result without making a completed claim reusable."""
    value = read_json(path)
    if not isinstance(value, dict) or value.get("state") != "dispatch_started":
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "worker claim cannot record a result")
    value.update({
        "state": "result_written", "finished_at": result.finished_at, "outcome": result.outcome,
        "result_sha256": sha256_file(result_path), "result_file": os.path.basename(result_path),
    })
    atomic_write_json(path, value)


def inspect_claim(dispatch: ManualDispatchPackage, *, state_dir: str) -> dict[str, str]:
    """Return a credential-free worker DTO; inspection never enables another dispatch."""
    path = _claim_path(state_dir, dispatch.claim_token)
    if not os.path.isfile(path):
        return {"schema_version": "1", "operation_id": dispatch.operation_id, "state": "unclaimed",
                "next_action": "execute_once"}
    value = read_json(path)
    if not isinstance(value, dict) or value.get("package_id") != dispatch.package_id or value.get("claim_token") != dispatch.claim_token:
        return {"schema_version": "1", "operation_id": dispatch.operation_id, "state": "invalid",
                "next_action": "do_not_retry"}
    state = value.get("state")
    if state == "result_written":
        next_action = "import_result"
    elif state == "dispatch_started":
        next_action = "reconcile_provider"
    else:
        next_action = "do_not_retry"
    return {key: str(value[key]) for key in ("operation_id", "state", "claimed_at", "started_at", "finished_at", "outcome", "result_sha256", "result_file") if key in value} | {"schema_version": "1", "next_action": next_action}


def execute_fixture(dispatch: ManualDispatchPackage, *, state_dir: str, output_dir: str, outcome: str = "succeeded") -> tuple[ManualResultPackage, str]:
    """Offline test executor. It makes zero network calls and emits a signed package."""
    key = _load_key(dispatch.key_id)
    dispatch.verify(key)
    claim = claim_dispatch(dispatch, state_dir=state_dir)
    started = _mark_started(claim)
    started_at = str(started["started_at"])
    os.makedirs(output_dir, exist_ok=True)
    response = json.dumps({"fixture": True, "operation_id": dispatch.operation_id, "outcome": outcome}, sort_keys=True).encode("utf-8")
    raw_hash = hashlib.sha256(response).hexdigest()
    if outcome == "succeeded":
        artifact_name = "artifact.json"
        artifact = os.path.join(output_dir, artifact_name)
        atomic_write_bytes(artifact, response)
        result = ManualResultPackage(dispatch.sha256(), dispatch.package_id, dispatch.operation_id, dispatch.claim_token,
                                     started_at, utc_now(), "succeeded", artifact_name, sha256_file(artifact), "application/json",
                                     len(response), raw_hash, socket.gethostname() or "worker", dispatch.key_id).sign(key)
    elif outcome in {"failed", "outcome_unknown"}:
        result = ManualResultPackage(dispatch.sha256(), dispatch.package_id, dispatch.operation_id, dispatch.claim_token,
                                     started_at, utc_now(), outcome, raw_response_sha256=raw_hash,
                                     worker_id=socket.gethostname() or "worker", key_id=dispatch.key_id).sign(key)
    else:
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "fixture outcome is invalid")
    result_path = os.path.join(output_dir, "manual_result.json")
    atomic_write_json(result_path, result.to_dict())
    _mark_result_written(claim, result=result, result_path=result_path)
    return result, claim


def execute_http_once(dispatch: ManualDispatchPackage, *, state_dir: str, output_dir: str, confirmation: str) -> tuple[ManualResultPackage, str]:
    """Perform exactly one explicit OpenAI-compatible ``/images/generations`` POST.

    Any exception after the durable ``dispatch_started`` transition intentionally
    produces no retryable result: the operator must reconcile the provider
    account before deciding what happened.
    """
    if confirmation != "EXECUTE-ONCE":
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "real manual dispatch requires EXECUTE-ONCE confirmation")
    from manju.production.runtime_profiles import resolve_manual_sync_profile

    key = _load_key(dispatch.key_id)
    dispatch.verify(key)
    profile = resolve_manual_sync_profile(required_profile=dispatch.provider_profile)
    claim = claim_dispatch(dispatch, state_dir=state_dir)
    started = _mark_started(claim)
    started_at = str(started["started_at"])
    body = json.dumps(dispatch.provider_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(profile["base_url"] + "/images/generations", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + profile["api_key"]})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=profile["timeout_seconds"]) as response:
            raw = response.read(profile["max_artifact_bytes"] + 1)
    except OSError as exc:
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "manual synchronous transport outcome is unknown; do not retry") from exc
    if len(raw) > profile["max_artifact_bytes"]:
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "manual provider response exceeded limit; do not retry")
    raw_hash = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw.decode("utf-8"))
        item = (value.get("data") or [])[0] if isinstance(value, dict) else {}
        encoded = item.get("b64_json") if isinstance(item, dict) else ""
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("provider response lacks b64_json")
        import base64
        artifact_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "manual provider response cannot be safely imported; do not retry") from exc
    if not artifact_bytes or len(artifact_bytes) > profile["max_artifact_bytes"]:
        raise ProductionError(ReasonCode.OPERATION_OUTCOME_UNKNOWN.value, "manual provider artifact is invalid; do not retry")
    os.makedirs(output_dir, exist_ok=True)
    artifact_name = "artifact.png"
    artifact = os.path.join(output_dir, artifact_name)
    atomic_write_bytes(artifact, artifact_bytes)
    result = ManualResultPackage(dispatch.sha256(), dispatch.package_id, dispatch.operation_id, dispatch.claim_token,
                                 started_at, utc_now(), "succeeded", artifact_name, sha256_file(artifact), "image/png",
                                 len(artifact_bytes), raw_hash, socket.gethostname() or "worker", dispatch.key_id).sign(key)
    result_path = os.path.join(output_dir, "manual_result.json")
    atomic_write_json(result_path, result.to_dict())
    _mark_result_written(claim, result=result, result_path=result_path)
    return result, claim


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manju manual_sync one-shot fixture worker")
    parser.add_argument("dispatch", help="signed manual dispatch JSON")
    parser.add_argument("--state-dir", required=True, help="controlled worker-local claim directory")
    parser.add_argument("--output-dir", required=True, help="empty result package directory")
    parser.add_argument("--fixture-outcome", choices=("succeeded", "failed", "outcome_unknown"), default="succeeded")
    parser.add_argument("--execute-http-once", action="store_true", help="perform one real HTTPS POST; requires confirmation")
    parser.add_argument("--confirm", default="", help="must be EXECUTE-ONCE when --execute-http-once is used")
    parser.add_argument("--inspect-claim", action="store_true", help="read the durable local claim without dispatching")
    args = parser.parse_args(argv)
    try:
        value = read_json(args.dispatch)
        dispatch = ManualDispatchPackage.from_dict(value)
        if args.inspect_claim:
            dispatch.verify(_load_key(dispatch.key_id))
            print(json.dumps(inspect_claim(dispatch, state_dir=args.state_dir), ensure_ascii=False, sort_keys=True))
            return
        if args.execute_http_once:
            result, claim = execute_http_once(dispatch, state_dir=args.state_dir, output_dir=args.output_dir, confirmation=args.confirm)
        else:
            result, claim = execute_fixture(dispatch, state_dir=args.state_dir, output_dir=args.output_dir, outcome=args.fixture_outcome)
        print(json.dumps({"result": result.to_dict(), "claim_path": claim}, ensure_ascii=False, sort_keys=True))
    except ProductionError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        raise SystemExit(exc.exit_code)


if __name__ == "__main__":
    main()
