"""Credential-free ProductionRun audit snapshots and verification."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from datetime import datetime, timezone
from typing import Any

from manju.production.events import HmacKeyProvider, sign_payload
from manju.production.models import ProductionError, ReasonCode
from manju.production.store import ProjectStore
from manju.utils.runtime import atomic_write_bytes, atomic_write_json, read_json


AUDIT_SCHEMA_VERSION = "production-audit-m2-8-v1"
_MANIFEST_RECORD = "AUDIT_MANIFEST.json"
_MANIFEST_DOMAIN = "manju-production-audit-manifest-v1"
_FORBIDDEN_NAMES = frozenset({"credentials.json", "secrets.json"})
_FORBIDDEN_KEYS = frozenset({"api_key", "authorization", "password", "secret", "access_token", "bearer_token", "client_secret", "auth_token"})


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_json_metadata(path: str) -> None:
    value = read_json(path)
    if value is None:
        return

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).lower() in _FORBIDDEN_KEYS:
                    raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit source contains credential metadata")
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)


def _is_link_or_reparse(path: str) -> bool:
    value = os.lstat(path)
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _copy_safe_tree(source: str, destination: str, *, allowed: callable, scan_metadata: bool = True) -> None:
    """Copy only declared evidence files and refuse all links/reparse points."""
    source = os.path.abspath(source)
    if _is_link_or_reparse(source):
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit source cannot be a link or reparse point")
    for root, directories, files in os.walk(source, followlinks=False):
        relative_root = os.path.relpath(root, source)
        for name in list(directories):
            path = os.path.join(root, name)
            if _is_link_or_reparse(path) or name in {"__pycache__", ".pytest_cache", ".venv"} or name.startswith(".env"):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit source contains an unsafe directory")
        for name in files:
            path = os.path.join(root, name)
            relative = os.path.normpath(os.path.join(relative_root, name)).replace(os.sep, "/")
            if _is_link_or_reparse(path) or name.lower() in _FORBIDDEN_NAMES or name.startswith(".env") or not allowed(relative):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit source contains an unsafe file")
            if scan_metadata and name.lower().endswith((".json", ".jsonl")):
                _validate_json_metadata(path)
            target = os.path.join(destination, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(path, target)


def _project_evidence_allowed(relative: str, source_relative: str, artifact_paths: frozenset[str] = frozenset()) -> bool:
    parts = relative.split("/")
    if relative in artifact_paths or relative in {"project.json", source_relative.replace(os.sep, "/"), "production/events.jsonl", "production/state.json", "production/artifacts.json"}:
        return True
    if len(parts) == 4 and parts[:3] == ["production", "manual", "dispatches"] and parts[-1].endswith(".json"):
        return True
    return len(parts) >= 4 and parts[:2] == ["production", "runs"] and os.path.splitext(parts[-1])[1].lower() in {".json", ".bin", ".png", ".jpg", ".jpeg", ".webp"}


def _worker_result_allowed(relative: str) -> bool:
    return relative == "manual_result.json" or relative in {"artifact.json", "artifact.bin", "artifact.png", "artifact.jpg", "artifact.jpeg", "artifact.webp"}


def _worker_state_allowed(relative: str) -> bool:
    parts = relative.split("/")
    return len(parts) == 2 and parts[0] == "claims" and parts[1].endswith(".json")


def _write_manifest(destination: str) -> int:
    manifest = os.path.join(destination, "SHA256SUMS.txt")
    entries: list[str] = []
    for root, _directories, files in os.walk(destination):
        for name in files:
            path = os.path.join(root, name)
            if os.path.abspath(path) in {os.path.abspath(manifest), os.path.abspath(os.path.join(destination, _MANIFEST_RECORD))}:
                continue
            relative = os.path.relpath(path, destination).replace(os.sep, "/")
            entries.append(f"{_sha256_file(path)}  {relative}")
    atomic_write_bytes(manifest, ("\n".join(sorted(entries)) + "\n").encode("utf-8"))
    return len(entries)


def export_audit_snapshot(*, project_json: str, destination: str, worker_result_dir: str = "",
                          worker_state_dir: str = "", key_provider: HmacKeyProvider | None = None) -> dict[str, Any]:
    """Copy auditable project facts without copying any HMAC or provider credentials."""
    project_json = os.path.abspath(project_json)
    project_root = os.path.dirname(project_json)
    destination = os.path.abspath(destination)
    if not os.path.isfile(project_json) or os.path.exists(destination):
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit destination must be new and project must exist")
    if os.path.commonpath((project_root, destination)) == project_root:
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit destination cannot be inside the project")
    project = read_json(project_json)
    if not isinstance(project, dict) or not project.get("project_id"):
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit project is invalid")
    key_id = str(project.get("integrity", {}).get("hmac_key_id", ""))
    key = key_provider.get_key(key_id) if key_provider and key_id else None
    if not key:
        raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "audit export requires the external HMAC key")

    os.makedirs(destination)
    source_relative = str((project.get("source") or {}).get("path", ""))
    graph = ProjectStore(project_json, key_provider=key_provider).artifact_graph()
    artifact_paths = frozenset(record.path for record in graph._records.values())
    _copy_safe_tree(project_root, os.path.join(destination, "project"),
                    allowed=lambda relative: _project_evidence_allowed(relative, source_relative, artifact_paths))
    for label, source, allowed in (("worker-result", worker_result_dir, _worker_result_allowed), ("worker-state", worker_state_dir, _worker_state_allowed)):
        if source:
            source = os.path.abspath(source)
            if not os.path.isdir(source):
                raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, f"audit {label} directory is unavailable")
            _copy_safe_tree(source, os.path.join(destination, label), allowed=allowed)
    metadata = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "bundle_type": "evidence_snapshot",
        "hmac_verification": "requires_external_key",
        "project_id": project["project_id"],
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "contains_worker_result": bool(worker_result_dir),
        "contains_worker_state": bool(worker_state_dir),
        "manifest_hmac_key_id": key_id,
    }
    atomic_write_json(os.path.join(destination, "audit.json"), metadata)
    manifest_entries = _write_manifest(destination)
    manifest_path = os.path.join(destination, "SHA256SUMS.txt")
    manifest_record = {"schema_version": AUDIT_SCHEMA_VERSION, "domain": _MANIFEST_DOMAIN,
                       "project_id": project["project_id"], "key_id": key_id,
                       "manifest_sha256": _sha256_file(manifest_path)}
    atomic_write_json(os.path.join(destination, _MANIFEST_RECORD), {
        **manifest_record, "signature": sign_payload(manifest_record, key),
    })
    return {**metadata, "manifest_entries": manifest_entries, "destination": destination}


def verify_audit_snapshot(*, destination: str, key_provider: HmacKeyProvider | None = None,
                          verify_hmac: bool = False) -> dict[str, Any]:
    """Verify snapshot hashes and, only with an external key, its event HMACs."""
    destination = os.path.abspath(destination)
    metadata = read_json(os.path.join(destination, "audit.json"))
    manifest = os.path.join(destination, "SHA256SUMS.txt")
    record = read_json(os.path.join(destination, _MANIFEST_RECORD))
    if not isinstance(metadata, dict) or metadata.get("schema_version") != AUDIT_SCHEMA_VERSION or not isinstance(record, dict) or not os.path.isfile(manifest):
        raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit snapshot is invalid")
    expected: dict[str, str] = {}
    with open(manifest, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n") or line.count("  ") != 1:
                raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, "audit manifest is invalid")
            digest, relative = line.rstrip("\n").split("  ", 1)
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest) or not relative or relative.startswith("/") or ".." in relative.split("/"):
                raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, "audit manifest is invalid")
            expected[relative] = digest
    actual_files: set[str] = set()
    for root, directories, files in os.walk(destination):
        if any(name in {"__pycache__", ".pytest_cache", ".venv"} for name in directories):
            raise ProductionError(ReasonCode.OPERATION_CONTRACT_INVALID.value, "audit snapshot contains runtime cache")
        for name in files:
            relative = os.path.relpath(os.path.join(root, name), destination).replace(os.sep, "/")
            if relative not in {"SHA256SUMS.txt", _MANIFEST_RECORD}:
                actual_files.add(relative)
    if set(expected) != actual_files or any(_sha256_file(os.path.join(destination, relative)) != digest for relative, digest in expected.items()):
        raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, "audit manifest verification failed")
    result: dict[str, Any] = {"schema_version": AUDIT_SCHEMA_VERSION, "bundle_type": "evidence_snapshot",
                              "manifest_valid": True, "hmac_verified": False,
                              "hmac_verification": "requires_external_key", "manifest_entries": len(expected)}
    if verify_hmac:
        if key_provider is None:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "audit HMAC verification requires the external key")
        required_record = {"schema_version": AUDIT_SCHEMA_VERSION, "domain": _MANIFEST_DOMAIN,
                           "project_id": metadata.get("project_id"), "key_id": metadata.get("manifest_hmac_key_id"),
                           "manifest_sha256": _sha256_file(manifest)}
        if set(record) != {*required_record, "signature"} or any(record.get(key) != value for key, value in required_record.items()):
            raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, "audit manifest signature record is invalid")
        key = key_provider.get_key(str(required_record["key_id"]))
        if not key:
            raise ProductionError(ReasonCode.HMAC_KEY_UNAVAILABLE.value, "audit manifest signing key is unavailable")
        if record.get("signature") != sign_payload(required_record, key):
            raise ProductionError(ReasonCode.SENSITIVE_EVENT_SIGNATURE_INVALID.value, "audit manifest signature is invalid")
        project_json = os.path.join(destination, "project", "project.json")
        store = ProjectStore(project_json, key_provider=key_provider)
        project = store.load_project()
        store.validate_source(project)
        events = store.events.read()
        snapshot = store.snapshot()
        if snapshot.run_id:
            store.validate_contract(project, snapshot.run_id)
        result.update({"hmac_verified": True, "event_count": len(events), "project_id": project["project_id"]})
    return result
