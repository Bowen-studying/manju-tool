"""Offline, explicit import of a legacy CLI storyboard JSON into M6.0."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import uuid
from typing import Any

from manju.pipeline.storyboard_schema import validate_storyboard
from manju.production.locking import ProjectLock
from manju.production.models import (
    M6_DAG_VERSION,
    ProductionError,
    ProductionSnapshot,
    ReasonCode,
    fingerprint,
)
from manju.production.paths import ProjectPaths
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_bytes, atomic_write_json, read_json


LEGACY_IMPORT_MAX_INPUT_BYTES = 4 * 1024 * 1024
LEGACY_IMPORT_MAX_SCENES = 64
LEGACY_IMPORT_MAX_SHOTS = 512
LEGACY_IMPORT_MAX_TEXT_CHARS = 16 * 1024
LEGACY_IMPORT_MAX_TOTAL_TEXT_CHARS = 1_000_000
LEGACY_IMPORT_MAX_NODES = 20_000
LEGACY_IMPORT_MAX_DEPTH = 32


def _error(code: str, message: str) -> ProductionError:
    return ProductionError(code, message)


def _is_link_or_reparse(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_path_chain(path: str, *, require_exists: bool = False) -> str:
    """Reject links/reparse points in every existing component of a path."""
    absolute = os.path.abspath(path)
    current = absolute
    found = False
    while True:
        if os.path.lexists(current):
            found = True
            try:
                value = os.lstat(current)
            except OSError as exc:
                raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "path cannot be inspected") from exc
            if _is_link_or_reparse(value):
                raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "path cannot contain links or reparse points")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if require_exists and not found:
        raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "path does not exist")
    return absolute


def _regular_file(path: str) -> os.stat_result:
    absolute = _validate_path_chain(path, require_exists=True)
    try:
        value = os.lstat(absolute)
    except OSError as exc:
        raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "input file cannot be inspected") from exc
    if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
        raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "input must be a regular file")
    return value


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(getattr(value, "st_size", -1)),
        int(getattr(value, "st_mtime_ns", 0)),
    )


def _directory_identity(path: str) -> tuple[int, int]:
    absolute = _validate_path_chain(path, require_exists=True)
    try:
        value = os.lstat(absolute)
    except OSError as exc:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "directory identity is unavailable") from exc
    if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "directory identity is invalid")
    return int(getattr(value, "st_dev", 0)), int(getattr(value, "st_ino", 0))


def _assert_directory_identity(path: str, expected: tuple[int, int]) -> None:
    if _directory_identity(path) != expected:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "directory changed during import")


def _sync_directory(path: str) -> None:
    """Persist directory-entry changes where the host exposes directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_stable_file(path: str, *, max_bytes: int) -> bytes:
    absolute = os.path.abspath(path)
    before = _regular_file(absolute)
    if before.st_size > max_bytes:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard exceeds the input limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _file_signature(opened) != _file_signature(before):
                raise _error(ReasonCode.SOURCE_HASH_MISMATCH.value, "legacy storyboard changed while reading")
            content = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except ProductionError:
        raise
    except (OSError, ValueError) as exc:
        raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "legacy storyboard cannot be read") from exc
    _validate_path_chain(absolute, require_exists=True)
    if _file_signature(after) != _file_signature(before):
        raise _error(ReasonCode.SOURCE_HASH_MISMATCH.value, "legacy storyboard changed while reading")
    if len(content) > max_bytes:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard exceeds the input limit")
    return content


class _DuplicateKey(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _walk_resources(value: Any) -> tuple[int, int]:
    total_text = 0
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        candidate, depth = stack.pop()
        nodes += 1
        if nodes > LEGACY_IMPORT_MAX_NODES or depth > LEGACY_IMPORT_MAX_DEPTH:
            raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard resource limits exceeded")
        if isinstance(candidate, str):
            length = len(candidate)
            if length > LEGACY_IMPORT_MAX_TEXT_CHARS:
                raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard text field exceeds the limit")
            total_text += length
            if total_text > LEGACY_IMPORT_MAX_TOTAL_TEXT_CHARS:
                raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard text resources exceed the limit")
        elif isinstance(candidate, dict):
            stack.extend((item, depth + 1) for item in candidate.values())
        elif isinstance(candidate, list):
            stack.extend((item, depth + 1) for item in candidate)
    return total_text, nodes


def _parse_legacy_storyboard(content: bytes) -> tuple[dict[str, Any], dict[str, int | str]]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard JSON is invalid") from exc
    if not isinstance(value, dict):
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard root must be an object")
    schema_version = value.get("schema_version")
    if schema_version is not None and schema_version not in {"1", "1.0", "2", "2.0"}:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard schema is unsupported")
    scenes = value.get("scenes")
    if not isinstance(scenes, list) or not scenes or len(scenes) > LEGACY_IMPORT_MAX_SCENES:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard scene count is invalid")
    shot_count = 0
    for scene in scenes:
        if not isinstance(scene, dict) or not isinstance(scene.get("shots"), list) or not scene["shots"]:
            raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard scene shape is invalid")
        for shot in scene["shots"]:
            if not isinstance(shot, dict):
                raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard shot shape is invalid")
            shot_count += 1
    if shot_count < 1 or shot_count > LEGACY_IMPORT_MAX_SHOTS:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard shot count is invalid")
    text_chars, nodes = _walk_resources(value)
    errors = validate_storyboard(value)
    if errors:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "legacy storyboard schema validation failed")
    return value, {
        "schema_version": str(schema_version) if schema_version is not None else "unspecified",
        "scene_count": len(scenes),
        "shot_count": shot_count,
        "text_chars": text_chars,
        "node_count": nodes,
    }


def _ensure_parent_directory(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    _validate_path_chain(parent)
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "output parent cannot be created") from exc
    _validate_path_chain(parent, require_exists=True)
    if not stat.S_ISDIR(os.lstat(parent).st_mode):
        raise _error(ReasonCode.OPERATION_CONTRACT_INVALID.value, "output parent must be a directory")


def _existing_import_result(
    target: str, source_sha256: str, request_fingerprint: str,
) -> ProductionSnapshot | None:
    if not os.path.lexists(target):
        return None
    target = _validate_path_chain(target, require_exists=True)
    if not os.path.isdir(target):
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory must be a directory")
    project_file = os.path.join(target, "project.json")
    if not os.path.lexists(project_file):
        if os.listdir(target):
            raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory must be empty")
        return None
    _regular_file(project_file)
    try:
        project = read_json(project_file)
    except Exception as exc:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "existing project is not a valid import") from exc
    metadata = project.get("import") if isinstance(project, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("kind") != "legacy_storyboard"
        or metadata.get("source_sha256") != source_sha256
        or metadata.get("request_fingerprint") != request_fingerprint
    ):
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory is already occupied")
    try:
        service = ProductionService(project_file)
        snapshot = service.get_status()
        report = service.doctor()
    except ProductionError as exc:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "existing imported project is invalid") from exc
    if report.get("status") != "passed":
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "existing imported project is invalid")
    return snapshot


def _publish_staging(
    staging: str,
    target: str,
    *,
    staging_identity: tuple[int, int],
    parent_identity: tuple[int, int],
    transaction: dict[str, str],
) -> None:
    """Atomically publish a completed staging directory into a fresh target."""
    _ensure_parent_directory(target)
    _assert_directory_identity(os.path.dirname(os.path.abspath(target)), parent_identity)
    _assert_directory_identity(staging, staging_identity)
    if os.path.lexists(target):
        _validate_path_chain(target, require_exists=True)
        if not os.path.isdir(target) or os.listdir(target):
            raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory changed during import")
        try:
            os.rmdir(target)
        except OSError as exc:
            raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory changed during import") from exc
    _assert_directory_identity(os.path.dirname(os.path.abspath(target)), parent_identity)
    _assert_directory_identity(staging, staging_identity)
    try:
        os.replace(staging, target)
    except OSError as exc:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory could not be published") from exc
    _validate_path_chain(target, require_exists=True)
    _assert_directory_identity(target, staging_identity)
    marker = os.path.join(target, ".legacy-import-transaction.json")
    _regular_file(marker)
    if read_json(marker) != transaction:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "published import transaction is invalid")
    _sync_directory(os.path.dirname(target))


def _cleanup_staging(
    staging: str,
    *,
    expected_identity: tuple[int, int],
    expected_transaction: dict[str, str],
) -> None:
    if not os.path.lexists(staging):
        return
    try:
        _assert_directory_identity(staging, expected_identity)
        marker = os.path.join(staging, ".legacy-import-transaction.json")
        _regular_file(marker)
        if read_json(marker) != expected_transaction:
            raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "import cleanup transaction is invalid")
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        else:
            os.unlink(staging)
    except OSError:
        pass


def _import_legacy_storyboard(
    legacy_storyboard: str,
    output_dir: str,
    *,
    voice_script_enabled: bool = False,
    voice_director_enabled: bool = False,
    voice_tts_enabled: bool = False,
    video_prompt_enabled: bool = False,
    visual_enabled: bool = False,
    video_enabled: bool = False,
    video_mode: str = "mock",
    video_model_profile: str = "mock-video-v1",
    video_maximum_amount: str = "0",
    video_provider_profile: str = "async-video",
    video_provider_request: dict[str, Any] | None = None,
    hmac_key_id: str = "manju-local-default",
    _lease_id: str,
    _recovered_lease_id: str = "",
) -> ProductionSnapshot:
    """Import one old storyboard JSON without invoking any Agent or Provider."""
    source_path = _validate_path_chain(legacy_storyboard, require_exists=True)
    source_stat = _regular_file(source_path)
    content = _read_stable_file(source_path, max_bytes=LEGACY_IMPORT_MAX_INPUT_BYTES)
    if _file_signature(_regular_file(source_path)) != _file_signature(source_stat):
        raise _error(ReasonCode.SOURCE_HASH_MISMATCH.value, "legacy storyboard changed during import preflight")
    _storyboard, resource_stats = _parse_legacy_storyboard(content)
    source_sha256 = hashlib.sha256(content).hexdigest()

    if video_enabled and not video_prompt_enabled:
        raise _error(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "video import configuration requires video prompts")
    request_fingerprint = fingerprint({
        "contract_version": M6_DAG_VERSION,
        "voice_script_enabled": bool(voice_script_enabled),
        "voice_director_enabled": bool(voice_director_enabled),
        "voice_tts_enabled": bool(voice_tts_enabled),
        "video_prompt_enabled": bool(video_prompt_enabled),
        "visual_enabled": bool(visual_enabled),
        "video_enabled": bool(video_enabled),
        "video_mode": video_mode,
        "video_model_profile": video_model_profile,
        "video_maximum_amount": str(video_maximum_amount),
        "video_provider_profile": video_provider_profile,
        "video_provider_request": video_provider_request,
        "hmac_key_id": hmac_key_id,
    })
    target = os.path.abspath(output_dir)
    existing = _existing_import_result(target, source_sha256, request_fingerprint)
    if existing is not None:
        return existing
    _ensure_parent_directory(target)
    if os.path.lexists(target):
        _validate_path_chain(target, require_exists=True)
        if not os.path.isdir(target) or os.listdir(target):
            raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "output directory must be empty")

    transaction_id = fingerprint({
        "source_sha256": source_sha256,
        "request_fingerprint": request_fingerprint,
        "target_name": os.path.basename(target),
    })
    staging = f"{target}.m6-import-{_lease_id}"
    _validate_path_chain(staging)
    transaction_marker = os.path.join(staging, ".legacy-import-transaction.json")
    transaction = {
        "schema_version": "legacy-import-transaction-v1",
        "transaction_id": transaction_id,
        "lease_id": _lease_id,
        "source_sha256": source_sha256,
        "request_fingerprint": request_fingerprint,
    }
    if _recovered_lease_id:
        stale = f"{target}.m6-import-{_recovered_lease_id}"
        if os.path.lexists(stale):
            _validate_path_chain(stale, require_exists=True)
            stale_marker = os.path.join(stale, ".legacy-import-transaction.json")
            try:
                _regular_file(stale_marker)
                stale_transaction = read_json(stale_marker)
            except Exception as exc:
                raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "recovered import staging is untrusted") from exc
            expected_stale = {**transaction, "lease_id": _recovered_lease_id}
            if stale_transaction != expected_stale:
                raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "recovered import staging does not match")
            stale_identity = _directory_identity(stale)
            _cleanup_staging(
                stale,
                expected_identity=stale_identity,
                expected_transaction=expected_stale,
            )
    if os.path.lexists(staging):
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "import staging path is occupied")
    try:
        os.mkdir(staging)
    except OSError as exc:
        raise _error(ReasonCode.PROJECT_CONTRACT_CHANGED.value, "import staging directory cannot be created") from exc
    staging_identity = _directory_identity(staging)
    parent_identity = _directory_identity(os.path.dirname(target))
    atomic_write_json(transaction_marker, transaction)
    _assert_directory_identity(staging, staging_identity)

    published = False
    try:
        _assert_directory_identity(staging, staging_identity)
        seed_path = os.path.join(staging, "_legacy_input.json")
        atomic_write_bytes(seed_path, content)
        initialize_project(
            source=seed_path,
            source_type="storyboard",
            output_dir=staging,
            engine="legacy",
            hmac_key_id=hmac_key_id,
            voice_script_enabled=voice_script_enabled,
            voice_director_enabled=voice_director_enabled,
            voice_tts_enabled=voice_tts_enabled,
            video_prompt_enabled=video_prompt_enabled,
            visual_enabled=visual_enabled,
            video_enabled=video_enabled,
            video_mode=video_mode,
            video_model_profile=video_model_profile,
            video_maximum_amount=video_maximum_amount,
            video_provider_profile=video_provider_profile,
            video_provider_request=video_provider_request,
        )
        _assert_directory_identity(staging, staging_identity)
        try:
            os.unlink(seed_path)
        except OSError as exc:
            raise _error(ReasonCode.STAGE_INTEGRITY_FAILED.value, "import seed cleanup failed") from exc

        paths = ProjectPaths(staging)
        project = read_json(paths.project_file)
        if not isinstance(project, dict):
            raise _error(ReasonCode.STAGE_INTEGRITY_FAILED.value, "new project contract is invalid")
        source_copy = os.path.join(staging, str(project.get("source", {}).get("path", "")))
        if not os.path.isfile(source_copy) or sha256_file(source_copy) != source_sha256:
            raise _error(ReasonCode.SOURCE_HASH_MISMATCH.value, "legacy storyboard copy changed during import")

        run_id = f"run_{uuid.uuid4().hex}"
        stage_run_id = f"legacy-import-storyboard-{run_id.removeprefix('run_')}"
        stage_dir = paths.storyboard_dir(run_id, stage_run_id)
        os.makedirs(stage_dir, exist_ok=False)
        artifact_path = os.path.join(stage_dir, "storyboard.json")
        manifest_path = os.path.join(stage_dir, "legacy_import_manifest.json")
        authority_path = os.path.join(stage_dir, "legacy_import_authority.json")
        atomic_write_bytes(artifact_path, content)
        artifact_hash = sha256_file(artifact_path)
        artifact_rel = os.path.relpath(artifact_path, staging).replace(os.sep, "/")
        manifest_rel = os.path.relpath(manifest_path, staging).replace(os.sep, "/")
        authority_rel = os.path.relpath(authority_path, staging).replace(os.sep, "/")
        manifest = {
            "schema_version": "legacy-storyboard-import-manifest-v1",
            "import_contract": M6_DAG_VERSION,
            "verification_status": "unverified",
            "request_fingerprint": request_fingerprint,
            "source": {
                "sha256": source_sha256,
                "bytes": len(content),
            },
            "schema": resource_stats,
            "artifact": {"path": artifact_rel, "sha256": artifact_hash},
        }
        atomic_write_json(manifest_path, manifest)
        manifest_hash = sha256_file(manifest_path)
        authority = {
            "schema_version": "legacy-storyboard-import-authority-v1",
            "import_contract": M6_DAG_VERSION,
            "verification_status": "unverified",
            "request_fingerprint": request_fingerprint,
            "artifact_path": artifact_rel,
            "artifact_sha256": artifact_hash,
            "manifest_path": manifest_rel,
            "manifest_sha256": manifest_hash,
            "source_sha256": source_sha256,
        }
        atomic_write_json(authority_path, authority)
        authority_hash = sha256_file(authority_path)

        project["import"] = {
            "kind": "legacy_storyboard",
            "contract_version": M6_DAG_VERSION,
            "verification_status": "unverified",
            "request_fingerprint": request_fingerprint,
            "source_sha256": source_sha256,
            "source_bytes": len(content),
            "manifest_path": manifest_rel,
            "manifest_sha256": manifest_hash,
            "authority_path": authority_rel,
            "authority_sha256": authority_hash,
        }
        atomic_write_json(paths.project_file, project)

        service = ProductionService(paths.project_file)
        with ProjectLock(service.paths.lock_file):
            project = service.store.load_project()
            service._ensure_project_source_artifact(project)
            project = service.store.load_project()
            contract = service._create_contract(project, run_id)
            contract["legacy_import"] = {
                "manifest_sha256": manifest_hash,
                "authority_sha256": authority_hash,
                "verification_status": "unverified",
                "request_fingerprint": request_fingerprint,
            }
            unsigned_contract = {key: value for key, value in contract.items() if key != "contract_fingerprint"}
            contract["contract_fingerprint"] = fingerprint(unsigned_contract)
            os.makedirs(service.paths.run_dir(run_id), exist_ok=True)
            atomic_write_json(service.paths.contract_file(run_id), contract)
            service.store.events.append(
                "run_created",
                project_id=project["project_id"],
                run_id=run_id,
                payload={
                    "contract_fingerprint": contract["contract_fingerprint"],
                    "dag_version": contract["dag_version"],
                    "stage_sequence": contract["stage_sequence"],
                },
            )
            service.store.events.append("run_started", project_id=project["project_id"], run_id=run_id, payload={})
            service.store.events.append(
                "stage_scheduled",
                project_id=project["project_id"],
                run_id=run_id,
                payload={"stage": "storyboard", "stage_invocation_id": stage_run_id},
            )
            service.store.events.append(
                "stage_run_attached",
                project_id=project["project_id"],
                run_id=run_id,
                payload={"stage": "storyboard", "stage_run_id": stage_run_id},
            )
            terminal_artifact = {
                "logical_id": "storyboard.main",
                "version_id": f"sha256:{artifact_hash}",
                "path": artifact_rel,
            }
            marker = {
                "kind": "legacy_storyboard",
                "verification_status": "unverified",
                "request_fingerprint": request_fingerprint,
                "manifest_path": manifest_rel,
                "manifest_sha256": manifest_hash,
                "authority_path": authority_rel,
                "authority_sha256": authority_hash,
            }
            common = {
                "stage": "storyboard",
                "stage_run_id": stage_run_id,
                "authority_path": authority_rel,
                "authority_hash": authority_hash,
                "authority_files": [
                    {"path": manifest_rel, "sha256": manifest_hash},
                    {"path": authority_rel, "sha256": authority_hash},
                ],
                "artifacts": [terminal_artifact],
                "legacy_import": marker,
            }
            common["produced_artifacts"] = service._produced_artifacts(
                stage="storyboard",
                run_id=run_id,
                contract=contract,
                artifacts=[terminal_artifact],
                events=service.store.events.read(),
            )
            service.store.events.append(
                "stage_completed", project_id=project["project_id"], run_id=run_id, payload=common,
            )
            if contract["stage_sequence"] == ["storyboard"]:
                service.store.events.append("run_completed", project_id=project["project_id"], run_id=run_id, payload={})
            snapshot = service._snapshot_and_project()
            service.get_status()
            if service.doctor().get("status") != "passed":
                raise _error(ReasonCode.STAGE_INTEGRITY_FAILED.value, "imported project failed integrity checks")

        _assert_directory_identity(staging, staging_identity)
        _publish_staging(
            staging, target,
            staging_identity=staging_identity,
            parent_identity=parent_identity,
            transaction=transaction,
        )
        published = True
        return ProductionService(os.path.join(target, "project.json")).get_status()
    finally:
        if not published:
            _cleanup_staging(
                staging,
                expected_identity=staging_identity,
                expected_transaction=transaction,
            )


def import_legacy_storyboard(
    legacy_storyboard: str,
    output_dir: str,
    *,
    voice_script_enabled: bool = False,
    voice_director_enabled: bool = False,
    voice_tts_enabled: bool = False,
    video_prompt_enabled: bool = False,
    visual_enabled: bool = False,
    video_enabled: bool = False,
    video_mode: str = "mock",
    video_model_profile: str = "mock-video-v1",
    video_maximum_amount: str = "0",
    video_provider_profile: str = "async-video",
    video_provider_request: dict[str, Any] | None = None,
    hmac_key_id: str = "manju-local-default",
) -> ProductionSnapshot:
    """Serialize one explicit import per target and recover its deterministic staging."""
    target = os.path.abspath(output_dir)
    _ensure_parent_directory(target)
    parent = os.path.dirname(target)
    parent_identity = _directory_identity(parent)
    lock_path = f"{target}.m6-import.lock"
    with ProjectLock(lock_path) as import_lock:
        _assert_directory_identity(parent, parent_identity)
        recovered_lease_id = str((import_lock.recovered or {}).get("lease_id", ""))
        return _import_legacy_storyboard(
            legacy_storyboard,
            output_dir,
            voice_script_enabled=voice_script_enabled,
            voice_director_enabled=voice_director_enabled,
            voice_tts_enabled=voice_tts_enabled,
            video_prompt_enabled=video_prompt_enabled,
            visual_enabled=visual_enabled,
            video_enabled=video_enabled,
            video_mode=video_mode,
            video_model_profile=video_model_profile,
            video_maximum_amount=video_maximum_amount,
            video_provider_profile=video_provider_profile,
            video_provider_request=video_provider_request,
            hmac_key_id=hmac_key_id,
            _lease_id=import_lock.lease_id,
            _recovered_lease_id=recovered_lease_id,
        )


__all__ = [
    "LEGACY_IMPORT_MAX_INPUT_BYTES",
    "import_legacy_storyboard",
]
