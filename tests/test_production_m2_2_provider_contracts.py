import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from manju.production.models import ProductionError, ReasonCode
from manju.production.providers import HttpJsonVisualProvider, ProviderCapabilities, ProviderObservation


class _ProviderFixtureHandler(BaseHTTPRequestHandler):
    artifact = b'{"provider":"fixture","image":"bytes"}'
    mode = "success"
    submitted_headers = {}
    submitted_body = {}
    artifact_headers = {}

    def log_message(self, *_args):
        pass

    def _send_json(self, value):
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path != "/operations":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        type(self).submitted_headers = dict(self.headers)
        type(self).submitted_body = json.loads(self.rfile.read(size).decode("utf-8"))
        self._send_json({"provider_job_id": "job-1"})

    def do_GET(self):
        base = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        if self.path == "/operations/job-1":
            if type(self).mode == "failed-cost":
                self._send_json({
                    "outcome": "failed", "provider_job_id": "job-1", "actual_amount": "9", "currency": "EUR", "usage": {"units": "4"},
                })
                return
            if type(self).mode == "unknown-cost":
                self._send_json({"outcome": "outcome_unknown", "provider_job_id": "job-1", "cost_status": "unknown", "usage": {"units": "4"}})
                return
            if type(self).mode == "wrong-job":
                self._send_json({"outcome": "failed", "provider_job_id": "different-job", "actual_amount": "9", "currency": "EUR", "usage": {"units": "4"}})
                return
            if type(self).mode == "foreign-origin":
                self._send_json({
                    "outcome": "succeeded", "provider_job_id": "job-1", "result_fingerprint": "sha256:" + hashlib.sha256(type(self).artifact).hexdigest(),
                    "artifact_url": "http://example.invalid/artifact", "artifact_media_type": "application/octet-stream", "artifact_size": len(type(self).artifact),
                })
                return
            fingerprint = "sha256:" + hashlib.sha256(type(self).artifact).hexdigest()
            if type(self).mode == "bad-hash":
                fingerprint = "sha256:" + "0" * 64
            self._send_json({
                "outcome": "succeeded", "provider_job_id": "job-1", "result_fingerprint": fingerprint,
                "artifact_url": base + "/artifact", "artifact_media_type": "application/octet-stream", "artifact_size": len(type(self).artifact),
                "actual_amount": "7", "currency": "USD", "usage": {"units":  "3"},
            })
            return
        if self.path == "/artifact":
            data = type(self).artifact
            type(self).artifact_headers = dict(self.headers)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)


@pytest.fixture
def local_provider_server():
    _ProviderFixtureHandler.mode = "success"
    _ProviderFixtureHandler.submitted_headers = {}
    _ProviderFixtureHandler.submitted_body = {}
    _ProviderFixtureHandler.artifact_headers = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://{server.server_address[0]}:{server.server_address[1]}"
        yield origin
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _provider(origin):
    return HttpJsonVisualProvider(
        base_url=origin, api_key="local-test-key", allowed_artifact_origins=(origin,), timeout_seconds=2,
        allow_insecure_http=True,
    )


def test_http_provider_contract_preserves_idempotency_and_signed_artifact_receipt(local_provider_server):
    provider = _provider(local_provider_server)
    assert provider.capabilities == ProviderCapabilities(True, True, True, True)
    assert provider.submit("operation-1", idempotency_key="operation-1") == "job-1"
    observation = provider.reconcile("job-1")
    assert _ProviderFixtureHandler.submitted_body == {"operation_id": "operation-1"}
    assert _ProviderFixtureHandler.submitted_headers["Idempotency-Key"] == "operation-1"
    assert "Authorization" not in _ProviderFixtureHandler.artifact_headers
    assert observation.artifact_bytes == _ProviderFixtureHandler.artifact
    assert observation.result_fingerprint == "sha256:" + hashlib.sha256(observation.artifact_bytes).hexdigest()
    assert observation.settled_usage == {"units": "3", "actual_amount": "7", "currency": "USD", "cost_status": "final", "cost_source": "provider_response"}


@pytest.mark.parametrize("mode", ["foreign-origin", "bad-hash"])
def test_http_provider_rejects_untrusted_or_mismatched_artifact_receipts(local_provider_server, mode):
    _ProviderFixtureHandler.mode = mode
    provider = _provider(local_provider_server)
    with pytest.raises(ProductionError) as exc:
        provider.reconcile("job-1")
    assert exc.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_http_provider_binds_failed_costs_and_makes_unknown_cost_explicit(local_provider_server):
    provider = _provider(local_provider_server)
    _ProviderFixtureHandler.mode = "failed-cost"
    failed = provider.reconcile("job-1")
    assert failed.provider_job_id == "job-1"
    assert failed.settled_usage == {"units": "4", "actual_amount": "9", "currency": "EUR", "cost_status": "final", "cost_source": "provider_response"}
    _ProviderFixtureHandler.mode = "unknown-cost"
    unknown = provider.reconcile("job-1")
    assert unknown.settled_usage == {"units": "4", "actual_amount": "unknown", "currency": "unknown", "cost_status": "unknown", "cost_source": "unknown"}


def test_http_provider_rejects_a_response_for_a_different_job(local_provider_server):
    _ProviderFixtureHandler.mode = "wrong-job"
    with pytest.raises(ProductionError) as exc:
        _provider(local_provider_server).reconcile("job-1")
    assert exc.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_provider_contract_rejects_missing_idempotency_or_uncommitted_success(local_provider_server):
    provider = _provider(local_provider_server)
    with pytest.raises(ProductionError) as exc:
        provider.submit("operation-1", idempotency_key="")
    assert exc.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value


def test_http_provider_requires_https_unless_a_test_explicitly_allows_local_http(local_provider_server):
    with pytest.raises(ProductionError) as exc:
        HttpJsonVisualProvider(
            base_url=local_provider_server, api_key="local-test-key", allowed_artifact_origins=(local_provider_server,),
        )
    assert exc.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value
    with pytest.raises(ProductionError) as exc:
        ProviderObservation(
            outcome="succeeded", provider_job_id="job-1", result_fingerprint="sha256:" + "0" * 64,
            artifact_bytes=b"artifact", artifact_media_type="application/octet-stream",
        )
    assert exc.value.code == ReasonCode.OPERATION_CONTRACT_INVALID.value
