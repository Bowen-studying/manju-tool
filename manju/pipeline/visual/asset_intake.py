"""Offline evidence and approval records for externally prepared image assets.

This module does not fetch, generate, or edit an asset.  It creates a
technical inspection record, then binds an explicit human promotion to the
exact inspected file with an HMAC supplied by the launcher.  The planner uses
that record to distinguish a provider asset from a manually labelled formal
registry entry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Sequence

from PIL import Image

from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json


ASSET_INTAKE_SCHEMA_VERSION = 1
ASSET_PROMOTION_SIGNING_KEY_ENV = "MANJU_ASSET_PROMOTION_KEY"
MIN_PROMOTION_SIGNING_KEY_BYTES = 32
_SOURCE_KINDS = frozenset({"provider", "local", "fixture"})
_MAX_DERIVATION_ROOT_ENTRIES = 10_000


class AssetIntakeError(ValueError):
    """Raised when an asset cannot be inspected or promoted safely."""


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AssetIntakeError(f"{field} is required")
    return text


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unsigned(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "integrity_signature"}


def _signature(value: dict, key: str) -> str:
    payload = json.dumps(_unsigned(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _promotion_signing_key(*, purpose: str) -> str:
    key = os.environ.get(ASSET_PROMOTION_SIGNING_KEY_ENV, "")
    if not key:
        raise AssetIntakeError(f"{ASSET_PROMOTION_SIGNING_KEY_ENV} is required for {purpose}")
    if len(key.encode("utf-8")) < MIN_PROMOTION_SIGNING_KEY_BYTES:
        raise AssetIntakeError(
            f"{ASSET_PROMOTION_SIGNING_KEY_ENV} must contain at least {MIN_PROMOTION_SIGNING_KEY_BYTES} UTF-8 bytes"
        )
    return key


def _signing_key_id(key: str) -> str:
    """Return an audit-safe identifier; never persist or display the secret."""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _sha256_text(value: object, field: str) -> str:
    text = _required_text(value, field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise AssetIntakeError(f"{field} must be a SHA-256 hex digest")
    return text


def _technical_requirements(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AssetIntakeError("technical_requirements must be an object")
    allowed = {"requires_alpha", "minimum_alpha_coverage", "maximum_logical_dimension"}
    unknown = set(value) - allowed
    if unknown:
        raise AssetIntakeError("technical_requirements contains unsupported fields")
    result: dict = {}
    if "requires_alpha" in value:
        if not isinstance(value["requires_alpha"], bool):
            raise AssetIntakeError("technical_requirements.requires_alpha must be a boolean")
        result["requires_alpha"] = value["requires_alpha"]
    if "minimum_alpha_coverage" in value:
        try:
            coverage = float(value["minimum_alpha_coverage"])
        except (TypeError, ValueError) as exc:
            raise AssetIntakeError("technical_requirements.minimum_alpha_coverage must be a number") from exc
        if not 0 <= coverage <= 1:
            raise AssetIntakeError("technical_requirements.minimum_alpha_coverage must be between 0 and 1")
        result["minimum_alpha_coverage"] = coverage
    if "maximum_logical_dimension" in value:
        dimension = value["maximum_logical_dimension"]
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
            raise AssetIntakeError("technical_requirements.maximum_logical_dimension must be a positive integer")
        result["maximum_logical_dimension"] = dimension
    return result


def _inside_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _normalise_derivation_roots(roots: Sequence[str] | None) -> tuple[str, ...]:
    if roots is None:
        return ()
    if isinstance(roots, str):
        raise AssetIntakeError("derivation_roots must be a sequence of directories")
    result: list[str] = []
    for index, value in enumerate(roots):
        root = os.path.realpath(os.path.abspath(_required_text(value, f"derivation_roots[{index}]")))
        if not os.path.isdir(root):
            raise AssetIntakeError(f"derivation_roots[{index}] is not a directory")
        if root not in result:
            result.append(root)
    return tuple(result)


def _find_parent_in_roots(parent_sha256: str, roots: Sequence[str]) -> str | None:
    """Find an exact parent copy without searching outside declared roots."""
    visited_entries = 0
    matches: list[str] = []
    pending = list(reversed(roots))
    scanned: set[str] = set()
    while pending:
        current_real = pending.pop()
        if current_real in scanned:
            continue
        scanned.add(current_real)
        root = next(root for root in roots if _inside_root(current_real, root))
        try:
            with os.scandir(current_real) as entries:
                for entry in entries:
                    visited_entries += 1
                    if visited_entries > _MAX_DERIVATION_ROOT_ENTRIES:
                        raise AssetIntakeError(
                            "derivation parent search exceeds the declared-root entry limit; preserve the recorded parent path"
                        )
                    candidate = os.path.realpath(entry.path)
                    if not _inside_root(candidate, root):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(candidate)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if _file_sha256(candidate) == parent_sha256:
                        matches.append(candidate)
        except OSError as exc:
            raise AssetIntakeError(f"derivation parent search cannot read declared asset root: {exc}") from exc
    return min(matches, key=os.path.normcase) if matches else None


def _resolve_derivation_parent(parent_path: str, parent_sha256: str, roots: Sequence[str] | None) -> str:
    recorded_path = os.path.abspath(parent_path)
    recorded = os.path.realpath(recorded_path)
    if os.path.normcase(recorded) != os.path.normcase(recorded_path):
        raise AssetIntakeError("derivation parent_image_path resolves through a symlink or junction")
    if os.path.isfile(recorded_path):
        if _file_sha256(recorded_path) == parent_sha256:
            return recorded_path
        raise AssetIntakeError("derivation parent_image_sha256 does not match parent_image_path")
    relocated = _find_parent_in_roots(parent_sha256, _normalise_derivation_roots(roots))
    if relocated:
        return relocated
    raise AssetIntakeError("derivation parent_image_path is unavailable and no matching parent exists in declared asset_roots")


def _derivation(
    value: object,
    *,
    output_sha256: str,
    derivation_roots: Sequence[str] | None = None,
) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AssetIntakeError("derivation must be an object")
    required = {"parent_image_path", "parent_image_sha256", "operation", "operation_version", "parameter_fingerprint", "output_image_sha256"}
    if set(value) - (required | {"mask_sha256"}) or not required.issubset(value):
        raise AssetIntakeError("derivation must contain the required lineage fields")
    recorded_parent_path = _required_text(value["parent_image_path"], "derivation.parent_image_path")
    parent_sha256 = _sha256_text(value["parent_image_sha256"], "derivation.parent_image_sha256")
    parent_path = _resolve_derivation_parent(recorded_parent_path, parent_sha256, derivation_roots)
    result = {
        "parent_image_path": parent_path,
        "parent_image_sha256": parent_sha256,
        "operation": _required_text(value["operation"], "derivation.operation"),
        "operation_version": _required_text(value["operation_version"], "derivation.operation_version"),
        "parameter_fingerprint": _sha256_text(value["parameter_fingerprint"], "derivation.parameter_fingerprint"),
        "output_image_sha256": _sha256_text(value["output_image_sha256"], "derivation.output_image_sha256"),
    }
    if "mask_sha256" in value and str(value["mask_sha256"] or "").strip():
        result["mask_sha256"] = _sha256_text(value["mask_sha256"], "derivation.mask_sha256")
    if result["output_image_sha256"] != output_sha256:
        raise AssetIntakeError("derivation output_image_sha256 does not match the inspected asset")
    return result


def _new_file(path: str, label: str) -> str:
    resolved = os.path.abspath(path)
    if os.path.exists(resolved):
        raise AssetIntakeError(f"{label} must be a new file")
    return resolved


def _candidate_without_fingerprint(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "fingerprint"}


def _validate_candidate(candidate: object) -> dict:
    """Recompute every technical fact before a human may promote it."""
    if not isinstance(candidate, dict):
        raise AssetIntakeError("candidate_path must contain a technically_inspected asset report")
    if type(candidate.get("schema_version")) is not int or candidate["schema_version"] != ASSET_INTAKE_SCHEMA_VERSION:
        raise AssetIntakeError("asset inspection schema_version is invalid")
    if candidate.get("status") != "technically_inspected":
        raise AssetIntakeError("candidate_path must contain a technically_inspected asset report")
    fingerprint = str(candidate.get("fingerprint", ""))
    expected_fingerprint = content_fingerprint(_candidate_without_fingerprint(candidate), length=64)
    if not fingerprint or not hmac.compare_digest(fingerprint, expected_fingerprint):
        raise AssetIntakeError("asset inspection fingerprint check failed")
    asset = candidate.get("asset")
    if not isinstance(asset, dict):
        raise AssetIntakeError("asset inspection report is invalid")
    _required_text(asset.get("asset_id"), "asset.asset_id")
    _required_text(asset.get("revision"), "asset.revision")
    _required_text(asset.get("asset_type"), "asset.asset_type")
    source_kind = _required_text(asset.get("source_kind"), "asset.source_kind").lower()
    if source_kind not in _SOURCE_KINDS:
        raise AssetIntakeError("asset.source_kind is invalid")
    image_path = os.path.realpath(os.path.abspath(_required_text(asset.get("image_path"), "asset.image_path")))
    if not os.path.isfile(image_path):
        raise AssetIntakeError("asset image no longer matches the technical inspection")
    try:
        with Image.open(image_path) as image:
            image.load()
            width, height = image.size
            has_alpha = "A" in image.getbands()
            alpha = image.convert("RGBA").getchannel("A")
    except (OSError, ValueError) as exc:
        raise AssetIntakeError(f"asset image no longer matches the technical inspection: {exc}") from exc
    alpha_bytes = alpha.tobytes()
    expected = {
        "image_path": image_path,
        "image_sha256": _file_sha256(image_path),
        "width": width,
        "height": height,
        "image_mode": "RGBA",
        "has_alpha": has_alpha,
        "has_transparent_pixels": sum(1 for value in alpha_bytes if value) < width * height,
        "nontransparent_pixel_count": sum(1 for value in alpha_bytes if value),
    }
    if any(asset.get(key) != value for key, value in expected.items()):
        raise AssetIntakeError("asset inspection technical facts do not match the current image")
    requirements = _technical_requirements(asset.get("technical_requirements"))
    alpha_coverage = expected["nontransparent_pixel_count"] / (width * height)
    if requirements.get("requires_alpha") and not has_alpha:
        raise AssetIntakeError("asset inspection does not satisfy requires_alpha")
    if alpha_coverage < requirements.get("minimum_alpha_coverage", 0.0):
        raise AssetIntakeError("asset inspection does not satisfy minimum_alpha_coverage")
    if max(width, height) > requirements.get("maximum_logical_dimension", max(width, height)):
        raise AssetIntakeError("asset inspection exceeds maximum_logical_dimension")
    _derivation(candidate.get("derivation"), output_sha256=expected["image_sha256"])
    return asset


def inspect_asset_candidate(
    image_path: str,
    output_path: str,
    *,
    asset_id: str,
    revision: str,
    asset_type: str,
    source_kind: str = "provider",
    technical_requirements: dict | None = None,
    derivation: dict | None = None,
) -> dict:
    """Write an immutable technical report for an existing local image file."""
    source = os.path.realpath(os.path.abspath(_required_text(image_path, "image_path")))
    if not os.path.isfile(source):
        raise AssetIntakeError("image_path does not exist")
    destination = _new_file(output_path, "asset inspection output")
    kind = _required_text(source_kind, "source_kind").lower()
    if kind not in _SOURCE_KINDS:
        raise AssetIntakeError("source_kind must be provider, local, or fixture")
    try:
        with Image.open(source) as image:
            image.load()
            width, height = image.size
            has_alpha = "A" in image.getbands()
            rgba = image.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise AssetIntakeError(f"image_path is not a readable image: {exc}") from exc
    if width < 1 or height < 1:
        raise AssetIntakeError("image_path has empty dimensions")
    alpha = rgba.getchannel("A")
    alpha_bytes = alpha.tobytes()
    nontransparent = sum(1 for value in alpha_bytes if value)
    image_sha256 = _file_sha256(source)
    requirements = _technical_requirements(technical_requirements)
    alpha_coverage = nontransparent / (width * height)
    if requirements.get("requires_alpha") and not has_alpha:
        raise AssetIntakeError("image_path does not satisfy requires_alpha")
    if alpha_coverage < requirements.get("minimum_alpha_coverage", 0.0):
        raise AssetIntakeError("image_path does not satisfy minimum_alpha_coverage")
    if max(width, height) > requirements.get("maximum_logical_dimension", max(width, height)):
        raise AssetIntakeError("image_path exceeds maximum_logical_dimension")
    report = {
        "schema_version": ASSET_INTAKE_SCHEMA_VERSION,
        "status": "technically_inspected",
        "asset": {
            "asset_id": _required_text(asset_id, "asset_id"),
            "revision": _required_text(revision, "revision"),
            "asset_type": _required_text(asset_type, "asset_type").lower(),
            "source_kind": kind,
            "image_path": source,
            "image_sha256": image_sha256,
            "width": width,
            "height": height,
            "image_mode": rgba.mode,
            "has_alpha": has_alpha,
            "has_transparent_pixels": nontransparent < width * height,
            "nontransparent_pixel_count": nontransparent,
            "technical_requirements": requirements,
        },
    }
    lineage = _derivation(derivation, output_sha256=image_sha256)
    if lineage:
        report["derivation"] = lineage
    report["fingerprint"] = content_fingerprint(report, length=64)
    atomic_write_json(destination, report)
    return report


def promote_asset_candidate(candidate_path: str, output_path: str, *, reviewer: str, note: str) -> dict:
    """Bind a human approval to a prior inspection without copying the source file."""
    candidate_file = os.path.realpath(os.path.abspath(_required_text(candidate_path, "candidate_path")))
    candidate = read_json(candidate_file)
    asset = _validate_candidate(candidate)
    key = _promotion_signing_key(purpose="asset promotion")
    destination = _new_file(output_path, "asset promotion output")
    promotion = {
        "schema_version": ASSET_INTAKE_SCHEMA_VERSION,
        "status": "human_promoted",
        "reviewer": _required_text(reviewer, "reviewer"),
        "note": _required_text(note, "note"),
        "candidate_report_sha256": _file_sha256(candidate_file),
        "candidate_fingerprint": candidate.get("fingerprint", ""),
        "asset": asset,
        "signing_key_id": _signing_key_id(key),
    }
    lineage = _derivation(candidate.get("derivation"), output_sha256=str(asset["image_sha256"]))
    if lineage:
        promotion["derivation"] = lineage
    promotion["integrity_signature"] = _signature(promotion, key)
    atomic_write_json(destination, promotion)
    return promotion


def verify_asset_promotion(
    promotion_path: str,
    *,
    asset_id: str,
    revision: str,
    asset_type: str,
    image_path: str,
    image_sha256: str,
    width: int,
    height: int,
    derivation_roots: Sequence[str] | None = None,
) -> dict:
    """Verify a provider asset promotion against the current registry record."""
    promotion = read_json(promotion_path)
    return verify_asset_promotion_evidence(
        promotion, asset_id=asset_id, revision=revision, asset_type=asset_type,
        image_path=image_path, image_sha256=image_sha256, width=width, height=height,
        derivation_roots=derivation_roots,
    )


def verify_asset_promotion_evidence(
    promotion: object,
    *,
    asset_id: str,
    revision: str,
    asset_type: str,
    image_path: str,
    image_sha256: str,
    width: int,
    height: int,
    derivation_roots: Sequence[str] | None = None,
) -> dict:
    """Validate signed promotion evidence embedded in an immutable plan."""
    if not isinstance(promotion, dict) or promotion.get("status") != "human_promoted":
        raise AssetIntakeError("provider asset requires a human_promoted asset promotion")
    key = _promotion_signing_key(purpose="provider asset promotion verification")
    if promotion.get("signing_key_id") != _signing_key_id(key):
        raise AssetIntakeError("provider asset promotion signing key identifier does not match the active key")
    actual_signature = str(promotion.get("integrity_signature", ""))
    if not actual_signature or not hmac.compare_digest(actual_signature, _signature(promotion, key)):
        raise AssetIntakeError("provider asset promotion integrity check failed")
    asset = promotion.get("asset")
    if not isinstance(asset, dict):
        raise AssetIntakeError("provider asset promotion is missing asset evidence")
    expected = {
        "asset_id": asset_id, "revision": revision, "asset_type": asset_type,
        "image_path": os.path.realpath(image_path), "image_sha256": image_sha256,
        "width": width, "height": height, "source_kind": "provider",
    }
    actual = {key: asset.get(key) for key in expected}
    if actual != expected:
        raise AssetIntakeError("provider asset promotion does not match the registry asset")
    _derivation(
        promotion.get("derivation"), output_sha256=image_sha256,
        derivation_roots=derivation_roots,
    )
    return promotion
