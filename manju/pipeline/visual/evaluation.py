"""Offline M8 visual-quality evaluation contracts and evidence tooling.

This module deliberately does not import a provider or start a network client.
Formal packaging reads the externally managed ProductionRun HMAC key only to
authenticate signed evidence; it never persists or prints it.  The module owns
the boring but important parts of an M8
evaluation: a reproducible sample scope, blind A/B material packaging, strict
review-file validation, and decoded score aggregation.  Human visual review
and any real image generation remain explicit gates outside this module.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import math
import stat
import tempfile
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
import zlib


CONTRACT_SCHEMA_VERSION = "m8-visual-quality-contract-v1"
SAMPLE_MANIFEST_SCHEMA_VERSION = "m8-visual-sample-manifest-v1"
PAIR_INPUT_SCHEMA_VERSION = "m8-visual-pair-input-v1"
PAIR_EVIDENCE_SCHEMA_VERSION = "m8-visual-pair-evidence-v1"
PUBLIC_MATERIAL_SCHEMA_VERSION = "m8-visual-blind-material-v1"
MAPPING_SCHEMA_VERSION = "m8-visual-ab-mapping-v1"
REVIEW_SCHEMA_VERSION = "m8-visual-review-v2"

MINIMUM_STORY_COUNT = 20
MINIMUM_SCENE_GROUP_COUNT = 60
PREFERENCE_THRESHOLD = 0.60
SCORE_MINIMUM = 1
SCORE_MAXIMUM = 5
ABSOLUTE_SCORE_FLOOR = 3.0
MAX_AGENT_SEVERE_ERROR_RATE = 0.10

DIMENSIONS = (
    "source_fidelity",
    "character_consistency",
    "wardrobe_continuity",
    "prop_continuity",
    "composition_readability",
    "action_continuity",
    "production_readiness",
)

REVIEW_PERSPECTIVES = (
    "content",
    "visual_production",
    "target_user",
)

SEVERE_ERROR_CODES = (
    "source_fact_invented_or_omitted",
    "character_identity_or_count_wrong",
    "wardrobe_state_breaks_continuity",
    "key_prop_identity_state_or_scale_wrong",
    "spatial_relation_or_action_time_wrong",
    "unusable_artifact_or_multi_panel_output",
)

REQUIRED_COVERAGE_CODES = (
    "multi_character",
    "duplicate_name_identity",
    "wardrobe_continuity",
    "key_prop_continuity",
    "day_night_transition",
    "action_continuity",
    "complex_composition",
)

_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_REQUIRED_PAIR_ARTIFACTS = frozenset({
    "source_or_storyboard_binding",
    "character_identity_cards",
    "scene_master",
    "shot_inputs",
    "agent_candidate_image",
    "legacy_candidate_image",
    "revision_trace",
    "visual_agent_run",
    "visual_event_log",
    "production_event_log",
    "visual_review_evidence",
    "cost_record_or_zero_cost_fixture_record",
})
_FORBIDDEN_BLIND_KEYS = frozenset({
    "agent", "legacy", "workflow", "engine", "provider", "model",
    "api_key", "token", "secret", "credential", "run_id",
})


class M8EvaluationError(ValueError):
    """Raised when an M8 contract or evidence file is not safe to use."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_canonical(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise M8EvaluationError(f"cannot read file for hashing: {path.name}") from exc
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M8EvaluationError(f"cannot read JSON file: {path.name}") from exc


def _write_json_new(path: Path, value: Any) -> None:
    """Write an evidence file without overwriting an existing artifact."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            dir=str(path.parent), prefix=".m8-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A same-filesystem hard link is an atomic create-if-absent.  It
            # cannot replace a target created after our initial checks.
            os.link(temporary, path)
        except FileExistsError as exc:
            raise M8EvaluationError(f"refusing to overwrite existing evidence: {path}") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _replace_json_atomic(path: Path, value: Any) -> None:
    """Atomically replace a preparatory private evidence JSON file."""
    path = path.resolve()
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            dir=str(path.parent), prefix=".m8-attest-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _validate_identifier(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result or not _IDENTIFIER_RE.fullmatch(result):
        raise M8EvaluationError(f"{field} must be a portable lowercase identifier")
    return result


def _is_link_or_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _resolve_under(root: Path, relative: str, *, field: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise M8EvaluationError(f"{field} must be relative to its declared root")
    lexical = root.resolve() / relative_path
    current = root.resolve()
    for part in relative_path.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise M8EvaluationError(f"{field} cannot traverse a link or reparse point")
    candidate = lexical.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise M8EvaluationError(f"{field} escapes its declared root") from exc
    return candidate


def _resolve_external_no_links(base: Path, value: str, *, field: str) -> Path:
    supplied = Path(value)
    lexical = supplied if supplied.is_absolute() else base / supplied
    lexical = Path(os.path.abspath(lexical))
    parts = lexical.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if _is_link_or_reparse(current):
            raise M8EvaluationError(f"{field} cannot traverse a link or reparse point")
    return lexical.resolve()


def _portable_source_ref(root: Path, path: Path) -> str:
    return "/".join((root.name, path.relative_to(root).as_posix()))


def _coverage_catalog() -> list[dict[str, Any]]:
    return [
        {
            "code": "multi_character",
            "label": "多人关系",
            "requirement": "至少一个组必须同时呈现两名或以上可区分人物及其空间关系。",
        },
        {
            "code": "duplicate_name_identity",
            "label": "重名与身份",
            "requirement": "至少一个组必须能核对重名角色的身份、数量和指代，不能靠名字猜测。",
        },
        {
            "code": "wardrobe_continuity",
            "label": "服装连续性",
            "requirement": "跨相邻镜头核对服装、发型和可见状态；变化必须有来源依据。",
        },
        {
            "code": "key_prop_continuity",
            "label": "关键道具",
            "requirement": "核对道具身份、数量、状态、空间位置和必要的相对尺度。",
        },
        {
            "code": "day_night_transition",
            "label": "昼夜变化",
            "requirement": "跨场景或相邻镜头核对昼夜、光线和时间状态，不允许无来源跳变。",
        },
        {
            "code": "action_continuity",
            "label": "动作连续性",
            "requirement": "核对动作发生顺序、前后状态和因果关系，尤其是关键动作。",
        },
        {
            "code": "complex_composition",
            "label": "复杂构图",
            "requirement": "覆盖前后景、遮挡、多人站位或多个关键物体的可制作构图。",
        },
        {
            "code": "single_character_baseline",
            "label": "单人基线",
            "requirement": "提供低复杂度基线，用来区分复杂场景收益与基本生成稳定性。",
        },
        {
            "code": "silent_action",
            "label": "无对白动作",
            "requirement": "不依赖对白，单独核对视觉动作和情绪表达。",
        },
        {
            "code": "text_boundary",
            "label": "文字边界",
            "requirement": "精确文字有要求时核对可读性；无要求时不得把乱码当作来源事实。",
        },
        {
            "code": "legacy_compatibility",
            "label": "旧路径兼容",
            "requirement": "至少一组来自旧 storyboard 输入，核对旧格式映射和可比较产物。",
        },
        {
            "code": "unverifiable_gate",
            "label": "不可验证门",
            "requirement": "至少一组保留证据不足时进入人工门的路径，不用规则检查冒充审美通过。",
        },
    ]


_COVERAGE_TRIADS = (
    ("multi_character", "key_prop_continuity", "action_continuity"),
    ("duplicate_name_identity", "wardrobe_continuity", "day_night_transition"),
    ("complex_composition", "multi_character", "wardrobe_continuity"),
    ("key_prop_continuity", "action_continuity", "text_boundary"),
    ("day_night_transition", "complex_composition", "single_character_baseline"),
    ("action_continuity", "duplicate_name_identity", "silent_action"),
    ("multi_character", "wardrobe_continuity", "legacy_compatibility"),
    ("key_prop_continuity", "complex_composition", "unverifiable_gate"),
    ("day_night_transition", "action_continuity", "text_boundary"),
    ("duplicate_name_identity", "multi_character", "single_character_baseline"),
)


def build_contract(
    *,
    baseline_commit: str = "d7191cd",
    baseline_branch: str = "feat/m3.4.1-audit-baseline",
) -> dict[str, Any]:
    """Return the versioned, provider-neutral M8 quality contract."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", str(baseline_commit)):
        raise M8EvaluationError("baseline_commit must be a git commit prefix or hash")
    contract: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": "m8-visual-agent-quality-v1",
        "status": "frozen",
        "baseline": {
            "commit": str(baseline_commit),
            "branch": str(baseline_branch),
            "comparison_baseline": "legacy",
        },
        "scope": {
            "unit": "one story + one pre-registered scene group + one comparable A/B image pair",
            "stories_minimum": MINIMUM_STORY_COUNT,
            "scene_groups_minimum": MINIMUM_SCENE_GROUP_COUNT,
            "storyboard_binding": "bind every group to the selected storyboard artifact and source evidence before review",
            "image_generation": "not performed by this contract tool; paid generation remains approval/grant gated",
        },
        "treatments": {
            "candidate": "agent",
            "baseline": "legacy",
            "comparison_rule": "same source, same storyboard binding, same scene-group scope, same review package",
            "excluded_pairs": "incomplete or non-comparable pairs are excluded and listed; they are never counted as failures or wins",
        },
        "required_coverage": {
            "codes": list(REQUIRED_COVERAGE_CODES),
            "catalog": _coverage_catalog(),
            "minimum_groups_per_code": 2,
        },
        "required_evidence_per_group": sorted(_REQUIRED_PAIR_ARTIFACTS),
        "blind_ab": {
            "public_materials": ["input.txt", "A/image.<ext>", "B/image.<ext>", "manifest.json"],
            "public_must_not_contain": [
                "engine names", "provider/model names", "run IDs", "absolute source paths",
                "credentials", "private A/B mapping", "reviewer results",
            ],
            "private_mapping": "store separately from the reviewer package; never distribute to reviewers",
            "randomization": "derive the formal seed from the frozen sample fingerprint and balance A/B within stories",
            "reviewer_perspectives": list(REVIEW_PERSPECTIVES),
            "reviewer_count": 3,
        },
        "scoring": {
            "scale": {"minimum": SCORE_MINIMUM, "maximum": SCORE_MAXIMUM, "higher_is_better": True},
            "dimensions": list(DIMENSIONS),
            "dimension_weights": {dimension: 1 for dimension in DIMENSIONS},
            "preference_values": ["A", "B", "tie"],
            "group_winner": "strict majority of the three preference votes; otherwise tie",
            "missing_or_invalid_row": "the visual gate is not ready; do not impute a score",
        },
        "severe_errors": {
            "definition": "a visible error that changes source meaning/identity/time/space or makes the image unusable for the declared shot",
            "codes": list(SEVERE_ERROR_CODES),
            "majority_rule": "at least 2 of 3 reviewers mark the same side severe in a group",
            "review_requirement": "a severe mark requires at least one code and a concrete note; no generic pass/fail text",
            "not_severe_by_itself": [
                "mere style preference", "minor anatomy or rendering blemish", "different but source-compatible framing",
            ],
        },
        "budget": {
            "offline_contract_and_packaging_calls": 0,
            "offline_provider_calls": 0,
            "offline_paid_amount_minor": 0,
            "per_story_generation_budget": "freeze before any real run; use the project Grant and never infer a price here",
            "retry_policy": "no automatic paid retry for unknown outcomes; local packaging never retries a provider",
        },
        "visual_gate": {
            "preference_threshold": PREFERENCE_THRESHOLD,
            "preference_statistics": "story-clustered one-sided 95% lower bound must meet the threshold",
            "source_fidelity": "story-clustered one-sided 95% lower bound of agent-minus-legacy must be >= 0",
            "character_consistency": "story-clustered one-sided 95% lower bound of agent-minus-legacy must be >= 0",
            "absolute_score_floor": ABSOLUTE_SCORE_FLOOR,
            "severe_error_count": "agent majority-group count must be <= legacy majority-group count",
            "agent_severe_error_rate_maximum": MAX_AGENT_SEVERE_ERROR_RATE,
            "coverage": "all required coverage codes must be represented in the frozen sample manifest",
        },
        "release_gates": {
            "engineering": "all visual/M2/ProductionRun structure, approval, authorization, budget, recovery and no-duplicate tests pass",
            "human_visual": "three independent blind reviewers complete every comparable pair; no automated rule result substitutes for this gate",
            "real_execution": "any real image generation requires user approval, isolated account, allowed profile, small Grant and operator evidence",
            "red_team": "three independent reviews close all high-priority findings",
            "platform": "Windows/Linux full regression passes on a clean commit",
            "default_route": "do not change the legacy default until every gate is evidenced",
        },
        "safety": {
            "provider_calls": "never from this module or its freeze/aggregate commands",
            "secret_handling": "formal verification reads only the external ProductionRun HMAC key; never persist or print it or Provider credentials",
            "authority": "ProductionRun HMAC events, final m8_visual_evidence_attested witness, VisualStageAdapter receipts and approved grants remain authoritative",
            "result_status": "aggregation may report visual_gate_passed/failed, never formal release without external evidence",
        },
    }
    contract["fingerprint"] = _fingerprint(contract)
    return contract


def _validate_source_manifest(source_manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_manifest_path = source_manifest_path.resolve()
    payload = _read_json(source_manifest_path)
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise M8EvaluationError("source manifest must contain a samples list")
    samples = payload["samples"]
    if len(samples) < MINIMUM_STORY_COUNT:
        raise M8EvaluationError(
            f"M8 requires at least {MINIMUM_STORY_COUNT} stories; source has {len(samples)}"
        )
    root = source_manifest_path.parent
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(samples, 1):
        if not isinstance(raw, dict):
            raise M8EvaluationError(f"source sample {index} must be an object")
        sample_id = _validate_identifier(raw.get("id"), f"samples[{index}].id")
        filename = str(raw.get("filename", "")).strip()
        if not filename or filename.startswith(("/", "\\")) or ".." in Path(filename).parts:
            raise M8EvaluationError(f"samples[{index}].filename is not a safe relative path")
        if sample_id in seen_ids or filename in seen_files:
            raise M8EvaluationError("source manifest sample IDs and filenames must be unique")
        seen_ids.add(sample_id)
        seen_files.add(filename)
        actual = _resolve_under(root, filename, field=f"samples[{index}].filename")
        if not actual.is_file():
            raise M8EvaluationError(f"source sample is missing: {filename}")
        actual_sha = _sha256_file(actual)
        expected_sha = str(raw.get("source_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or actual_sha != expected_sha:
            raise M8EvaluationError(f"source hash mismatch: {filename}")
        expected_bytes = raw.get("bytes")
        if not isinstance(expected_bytes, int) or expected_bytes != actual.stat().st_size:
            raise M8EvaluationError(f"source byte count mismatch: {filename}")
        records.append({
            "id": sample_id,
            "kind": str(raw.get("kind", "")),
            "filename": filename,
            "source_sha256": actual_sha,
            "bytes": actual.stat().st_size,
            "expected_stage": str(raw.get("expected_stage", "")),
            "source_ref": _portable_source_ref(root, actual),
        })
    return {"source_manifest": source_manifest_path, "root": root}, records


def build_sample_manifest(
    source_manifest_path: str | os.PathLike[str],
    *,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the current M7 source files into 20 stories and 60 group slots."""
    source_path = Path(source_manifest_path).resolve()
    context, records = _validate_source_manifest(source_path)
    active_contract = dict(contract or build_contract())
    stories: list[dict[str, Any]] = []
    scene_groups: list[dict[str, Any]] = []
    coverage_counts = {code: 0 for code in (item["code"] for item in _coverage_catalog())}
    evidence = list(active_contract["required_evidence_per_group"])
    for story_index, record in enumerate(records, 1):
        story_id = f"story-{story_index:02d}"
        triad = _COVERAGE_TRIADS[(story_index - 1) % len(_COVERAGE_TRIADS)]
        story_groups: list[str] = []
        for ordinal, coverage_code in enumerate(triad, 1):
            group_id = f"{story_id}-group-{ordinal:02d}"
            story_groups.append(group_id)
            coverage_counts[coverage_code] += 1
            selection_basis = ("baseline" if ordinal == 1 else "continuity_pair" if ordinal == 2 else "edge_case_probe")
            scene_groups.append({
                "group_id": group_id,
                "story_id": story_id,
                "ordinal": ordinal,
                "coverage_codes": [coverage_code],
                "selection_basis": selection_basis,
                "source_binding": {
                    "source_ref": record["source_ref"],
                    "source_sha256": record["source_sha256"],
                    "storyboard_binding": "bind after the storyboard artifact is selected; no guessed scene IDs",
                },
                "required_evidence": evidence,
                "execution_status": "not_started",
            })
        stories.append({
            "story_id": story_id,
            "source_sample_id": record["id"],
            "kind": record["kind"],
            "source_ref": record["source_ref"],
            "source_sha256": record["source_sha256"],
            "source_bytes": record["bytes"],
            "expected_stage": record["expected_stage"],
            "scene_group_ids": story_groups,
        })
    if len(scene_groups) < MINIMUM_SCENE_GROUP_COUNT:
        raise M8EvaluationError("sample manifest did not produce the required scene-group count")
    manifest: dict[str, Any] = {
        "schema_version": SAMPLE_MANIFEST_SCHEMA_VERSION,
        "status": "frozen_scope_not_executed",
        "contract_fingerprint": str(active_contract.get("fingerprint", "")),
        "source": {
            "manifest_ref": f"{context['root'].name}/{source_path.name}",
            "manifest_sha256": _sha256_file(source_path),
            "sample_root_ref": context["root"].name,
        },
        "minimums": {
            "stories": MINIMUM_STORY_COUNT,
            "scene_groups": MINIMUM_SCENE_GROUP_COUNT,
        },
        "story_count": len(stories),
        "scene_group_count": len(scene_groups),
        "coverage_catalog": _coverage_catalog(),
        "coverage_counts": coverage_counts,
        "stories": stories,
        "scene_groups": scene_groups,
        "execution": {
            "status": "not_started",
            "provider_calls": 0,
            "paid_amount_minor": 0,
            "human_review_rows": 0,
            "note": "This is a pre-registered scope; it is not evidence that images or reviews already exist.",
        },
    }
    manifest["fingerprint"] = _fingerprint(manifest)
    return manifest


def validate_sample_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SAMPLE_MANIFEST_SCHEMA_VERSION:
        raise M8EvaluationError("unsupported M8 sample manifest schema")
    stories = manifest.get("stories")
    groups = manifest.get("scene_groups")
    if not isinstance(stories, list) or not isinstance(groups, list):
        raise M8EvaluationError("sample manifest must contain stories and scene_groups")
    if len(stories) < MINIMUM_STORY_COUNT or len(groups) < MINIMUM_SCENE_GROUP_COUNT:
        raise M8EvaluationError("sample manifest is smaller than the M8 minimum scope")
    if manifest.get("story_count") != len(stories) or manifest.get("scene_group_count") != len(groups):
        raise M8EvaluationError("sample manifest declared counts do not match its contents")
    declared_fingerprint = str(manifest.get("fingerprint", ""))
    fingerprint_input = dict(manifest)
    fingerprint_input.pop("fingerprint", None)
    if declared_fingerprint != _fingerprint(fingerprint_input):
        raise M8EvaluationError("sample manifest fingerprint mismatch")
    required = set(REQUIRED_COVERAGE_CODES)
    actual_counts: dict[str, int] = {}
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        for code in set(map(str, group.get("coverage_codes", []))):
            actual_counts[code] = actual_counts.get(code, 0) + 1
    missing = sorted(code for code in required if actual_counts.get(code, 0) < 2)
    if missing:
        raise M8EvaluationError(f"sample manifest has insufficient coverage: {missing}")
    declared_counts = manifest.get("coverage_counts")
    catalog_codes = {
        str(item.get("code", ""))
        for item in manifest.get("coverage_catalog", [])
        if isinstance(item, Mapping)
    }
    if (
        not isinstance(declared_counts, Mapping)
        or set(map(str, declared_counts)) != catalog_codes
        or dict(declared_counts) != {code: actual_counts.get(code, 0) for code in catalog_codes}
    ):
        raise M8EvaluationError("sample manifest coverage_counts do not match scene groups")
    if any(not isinstance(item, Mapping) for item in stories + groups):
        raise M8EvaluationError("sample manifest stories and groups must be objects")
    story_ids = [str(item.get("story_id", "")) for item in stories]
    group_ids = [str(item.get("group_id", "")) for item in groups]
    if len(story_ids) != len(set(story_ids)) or len(group_ids) != len(set(group_ids)):
        raise M8EvaluationError("sample manifest IDs must be unique")
    if any(not _IDENTIFIER_RE.fullmatch(item) for item in story_ids + group_ids):
        raise M8EvaluationError("sample manifest contains unsafe IDs")
    story_id_set = set(story_ids)
    for story in stories:
        declared_groups = story.get("scene_group_ids")
        if not isinstance(declared_groups, list) or any(group_id not in set(group_ids) for group_id in declared_groups):
            raise M8EvaluationError(f"story {story['story_id']} has invalid scene_group_ids")
    for group in groups:
        if group.get("story_id") not in story_id_set:
            raise M8EvaluationError(f"group {group['group_id']} references an unknown story")
        codes = group.get("coverage_codes")
        if not isinstance(codes, list) or not codes:
            raise M8EvaluationError(f"group {group['group_id']} has no coverage code")
    return {
        "story_count": len(stories),
        "scene_group_count": len(groups),
        "coverage_codes": sorted(actual_counts),
        "fingerprint": str(manifest.get("fingerprint", "")),
    }


def freeze_m8(
    *,
    source_manifest_path: str | os.PathLike[str],
    contract_output: str | os.PathLike[str],
    sample_output: str | os.PathLike[str],
    baseline_commit: str = "d7191cd",
    baseline_branch: str = "feat/m3.4.1-audit-baseline",
) -> dict[str, Any]:
    """Write the frozen contract and source-bound sample manifest."""
    contract_path = Path(contract_output).resolve()
    sample_path = Path(sample_output).resolve()
    if contract_path.exists() or sample_path.exists():
        raise M8EvaluationError("freeze refuses to overwrite an existing contract or sample manifest")
    contract = build_contract(
        baseline_commit=baseline_commit,
        baseline_branch=baseline_branch,
    )
    manifest = build_sample_manifest(source_manifest_path, contract=contract)
    validate_sample_manifest(manifest)
    _write_json_new(contract_path, contract)
    try:
        _write_json_new(sample_path, manifest)
    except Exception:
        # A failed freeze must not leave a half-published contract artifact.
        try:
            contract_path.unlink()
        except OSError:
            pass
        raise
    return {
        "contract_path": str(contract_path),
        "sample_manifest_path": str(sample_path),
        "contract_fingerprint": contract["fingerprint"],
        "story_count": manifest["story_count"],
        "scene_group_count": manifest["scene_group_count"],
        "provider_calls": 0,
    }


def _path_value(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if isinstance(value, Mapping):
        value = value.get("path")
    result = str(value or "").strip()
    if not result:
        raise M8EvaluationError(f"pair field {key} is required")
    return result


def _load_pair_input(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != PAIR_INPUT_SCHEMA_VERSION:
        raise M8EvaluationError("unsupported M8 pair input schema")
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise M8EvaluationError("pair input must contain a non-empty pairs list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(pairs, 1):
        if not isinstance(raw, Mapping):
            raise M8EvaluationError(f"pairs[{index}] must be an object")
        group_id = _validate_identifier(raw.get("group_id"), f"pairs[{index}].group_id")
        if group_id in seen:
            raise M8EvaluationError("pair group IDs must be unique")
        seen.add(group_id)
        source_file = str(raw.get("source_file", "")).strip()
        source_text = raw.get("source_text")
        if not source_file and not isinstance(source_text, str):
            raise M8EvaluationError(f"pairs[{index}] requires source_file or source_text")
        def input_path(key: str) -> str:
            return str(_resolve_external_no_links(
                path.parent, _path_value(raw, key), field=f"pair field {key}"
            ))

        result.append({
            "group_id": group_id,
            "story_id": str(raw.get("story_id", "")),
            "source_file": str(_resolve_external_no_links(
                path.parent, source_file, field="pair field source_file"
            )) if source_file else "",
            "source_text": source_text,
            "agent_image": input_path("agent_image"),
            "legacy_image": input_path("legacy_image"),
            "evidence_file": input_path("evidence_file"),
        })
    return result


def attest_pair_evidence(evidence_file: str | os.PathLike[str]) -> dict[str, Any]:
    """Append the operator-authorized final HMAC witness for one private pair."""
    evidence_path = _resolve_external_no_links(
        Path.cwd(), str(evidence_file), field="evidence_file",
    )
    payload = _read_json(evidence_path)
    if not isinstance(payload, dict) or payload.get("schema_version") != PAIR_EVIDENCE_SCHEMA_VERSION:
        raise M8EvaluationError("unsupported pair evidence schema")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != _REQUIRED_PAIR_ARTIFACTS:
        raise M8EvaluationError("pair evidence artifacts are incomplete")
    artifact_sha256: dict[str, str] = {}
    production_log: Path | None = None
    for name in sorted(_REQUIRED_PAIR_ARTIFACTS):
        item = artifacts[name]
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise M8EvaluationError(f"invalid pair evidence artifact: {name}")
        artifact = _resolve_under(evidence_path.parent, str(item["path"]), field=f"artifacts.{name}.path")
        if artifact.is_symlink() or not artifact.is_file():
            raise M8EvaluationError(f"pair evidence artifact is missing: {name}")
        if name == "production_event_log":
            production_log = artifact
            continue
        if artifact.stat().st_size != _declared_bytes(item["bytes"], field=f"artifacts.{name}.bytes"):
            raise M8EvaluationError(f"pair evidence artifact size differs: {name}")
        digest = _sha256_file(artifact)
        if digest != str(item["sha256"]):
            raise M8EvaluationError(f"pair evidence artifact hash differs: {name}")
        artifact_sha256[name] = digest
    production = payload.get("production")
    execution = payload.get("agent_execution")
    if not isinstance(production, Mapping) or not isinstance(execution, Mapping) or production_log is None:
        raise M8EvaluationError("pair evidence lacks ProductionRun or Agent execution binding")
    key_id = str(production.get("hmac_key_id", "")).strip()
    configured_key = os.environ.get("MANJU_PRODUCTION_HMAC_KEY", "")
    if not key_id or not configured_key:
        raise M8EvaluationError("attestation requires MANJU_PRODUCTION_HMAC_KEY and hmac_key_id")
    try:
        from manju.production.events import EventStore
        from manju.production.security import MappingHmacKeyProvider

        store = EventStore(
            str(production_log),
            key_provider=MappingHmacKeyProvider({key_id: configured_key.encode("utf-8")}),
        )
        events = store.read()
        scoped = [
            event for event in events
            if event.get("project_id") == production.get("project_id")
            and event.get("run_id") == production.get("run_id")
        ]
        completed = next(
            event for event in reversed(scoped)
            if event.get("event_type") == "stage_completed"
            and (event.get("payload") or {}).get("stage") == "visual"
            and (event.get("payload") or {}).get("stage_run_id") == production.get("stage_run_id")
        )
        source_sha = str(payload.get("frozen_source_sha256", ""))
        agent_sha = artifact_sha256.get("agent_candidate_image", "")
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha) or not re.fullmatch(r"[0-9a-f]{64}", agent_sha):
            raise ValueError("source or Agent candidate hash is invalid")
        attestation_payload = {
            "group_id": str(payload.get("group_id", "")),
            "story_id": str(payload.get("story_id", "")),
            "frozen_source_sha256": source_sha,
            "agent_run_id": str(execution.get("run_id", "")),
            "agent_candidate_sha256": agent_sha,
            "evidence_artifact_sha256": dict(sorted(artifact_sha256.items())),
            "stage_completed_event_hash": completed.get("event_hash"),
            "key_id": key_id,
        }
        if scoped[-1].get("event_type") == "stage_completed":
            event = store.append(
                "m8_visual_evidence_attested",
                project_id=str(production.get("project_id", "")),
                run_id=str(production.get("run_id", "")),
                payload=attestation_payload,
            )
        elif (
            scoped[-1].get("event_type") == "m8_visual_evidence_attested"
            and all((scoped[-1].get("payload") or {}).get(key) == value for key, value in attestation_payload.items())
        ):
            # Recover idempotently if the process stopped after the durable
            # event append but before the private evidence JSON was replaced.
            event = scoped[-1]
        else:
            raise ValueError("ProductionRun does not end at the expected M8 attestation boundary")
    except Exception as exc:
        raise M8EvaluationError("cannot create trusted M8 ProductionRun attestation") from exc
    production_item = artifacts["production_event_log"]
    production_item["sha256"] = _sha256_file(production_log)
    production_item["bytes"] = production_log.stat().st_size
    _replace_json_atomic(evidence_path, payload)
    return {
        "evidence_file": str(evidence_path),
        "event_hash": event["event_hash"],
        "production_event_log_sha256": production_item["sha256"],
    }


def _validate_pair_evidence(
    pair: Mapping[str, Any],
    *,
    source_bytes: bytes,
    agent_path: Path,
    legacy_path: Path,
    expected_frozen_group: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_path = Path(str(pair["evidence_file"])).resolve()
    payload = _read_json(evidence_path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != PAIR_EVIDENCE_SCHEMA_VERSION:
        raise M8EvaluationError(f"unsupported pair evidence schema: {evidence_path.name}")
    if payload.get("group_id") != pair["group_id"] or str(payload.get("story_id", "")) != str(pair.get("story_id", "")):
        raise M8EvaluationError(f"pair evidence identity mismatch: {pair['group_id']}")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if payload.get("shared_input_sha256") != source_sha:
        raise M8EvaluationError(f"pair evidence is not bound to the shared input: {pair['group_id']}")
    frozen_source_sha = str(payload.get("frozen_source_sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", frozen_source_sha):
        raise M8EvaluationError(f"pair evidence has no frozen source binding: {pair['group_id']}")
    if expected_frozen_group is not None:
        expected_story = str(expected_frozen_group.get("story_id", ""))
        source_binding = expected_frozen_group.get("source_binding")
        expected_source_sha = (
            str(source_binding.get("source_sha256", ""))
            if isinstance(source_binding, Mapping)
            else ""
        )
        if pair.get("story_id") != expected_story or frozen_source_sha != expected_source_sha:
            raise M8EvaluationError(f"pair evidence does not match the frozen story/source: {pair['group_id']}")
        if source_sha != expected_source_sha:
            raise M8EvaluationError(
                f"formal reviewer input must be the exact frozen source bytes: {pair['group_id']}"
            )

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _REQUIRED_PAIR_ARTIFACTS:
        raise M8EvaluationError(f"pair evidence artifacts are incomplete: {pair['group_id']}")
    verified: dict[str, dict[str, Any]] = {}
    verified_paths: dict[str, Path] = {}
    for name in sorted(_REQUIRED_PAIR_ARTIFACTS):
        item = artifacts[name]
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "bytes"}:
            raise M8EvaluationError(f"invalid pair evidence artifact: {pair['group_id']}/{name}")
        artifact = _resolve_under(evidence_path.parent, str(item["path"]), field=f"artifacts.{name}.path")
        declared = _declared_bytes(item["bytes"], field=f"artifacts.{name}.bytes")
        if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size != declared:
            raise M8EvaluationError(f"pair evidence artifact integrity failed: {pair['group_id']}/{name}")
        digest = _sha256_file(artifact)
        if digest != str(item["sha256"]):
            raise M8EvaluationError(f"pair evidence artifact integrity failed: {pair['group_id']}/{name}")
        verified[name] = {"path": artifact.name, "sha256": digest, "bytes": declared}
        verified_paths[name] = artifact
    if verified["source_or_storyboard_binding"]["sha256"] != source_sha:
        raise M8EvaluationError(f"source/storyboard artifact differs from reviewer input: {pair['group_id']}")
    if verified["agent_candidate_image"]["sha256"] != _sha256_file(agent_path):
        raise M8EvaluationError(f"agent candidate differs from evidence: {pair['group_id']}")
    if verified["legacy_candidate_image"]["sha256"] != _sha256_file(legacy_path):
        raise M8EvaluationError(f"legacy candidate differs from evidence: {pair['group_id']}")

    execution = payload.get("agent_execution")
    if (
        not isinstance(execution, Mapping)
        or not str(execution.get("run_id", "")).strip()
        or execution.get("status") != "completed"
        or execution.get("stop_reason") != "completed"
        or execution.get("automated_review_completed") is not True
        or execution.get("passed_without_override") is not True
        or execution.get("manual_quality_override") is not False
        or execution.get("blocking_status") != "clear"
    ):
        raise M8EvaluationError(f"agent execution evidence is not a clear no-override completion: {pair['group_id']}")
    run_manifest = _read_json(verified_paths["visual_agent_run"])
    visual_review = _read_json(verified_paths["visual_review_evidence"])
    cost_record = _read_json(verified_paths["cost_record_or_zero_cost_fixture_record"])
    if not all(isinstance(item, Mapping) for item in (run_manifest, visual_review, cost_record)):
        raise M8EvaluationError(f"authoritative visual artifacts must be JSON objects: {pair['group_id']}")
    run_id = str(execution.get("run_id", ""))
    quality_gate = run_manifest.get("quality_gate")
    if (
        run_manifest.get("run_id") != run_id
        or run_manifest.get("status") != "completed"
        or run_manifest.get("stop_reason") != "completed"
        or not isinstance(quality_gate, Mapping)
        or quality_gate.get("automated_review_completed") is not True
        or quality_gate.get("passed_without_override") is not True
        or quality_gate.get("blocking_status") != "clear"
        or int(quality_gate.get("overridden_blocking_issue_count", 0) or 0) != 0
        or quality_gate.get("overridden_issue_ids") not in (None, [])
    ):
        raise M8EvaluationError(f"visual_agent_run contradicts the no-override completion: {pair['group_id']}")
    if (
        visual_review.get("run_id") != run_id
        or visual_review.get("status") != "completed"
        or visual_review.get("automated_review_completed") is not True
        or visual_review.get("passed_without_override") is not True
        or visual_review.get("blocking_status") != "clear"
        or int(visual_review.get("overridden_blocking_issue_count", 0) or 0) != 0
        or visual_review.get("overridden_issue_ids") not in (None, [])
    ):
        raise M8EvaluationError(f"visual_review contradicts the no-override completion: {pair['group_id']}")
    try:
        from manju.pipeline.visual.events import event_from_dict
        from manju.pipeline.visual.reducer import replay_visual_events

        event_values = []
        with verified_paths["visual_event_log"].open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    event_values.append(event_from_dict(json.loads(line)))
        recovered = replay_visual_events(event_values)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise M8EvaluationError(f"visual event chain is invalid: {pair['group_id']}") from exc
    event_store = run_manifest.get("event_store")
    recovered_quality = recovered.get("quality_gate") if isinstance(recovered, Mapping) else None
    if (
        not event_values
        or not isinstance(recovered, Mapping)
        or recovered.get("run_id") != run_id
        or recovered.get("status") != "completed"
        or recovered.get("stop_reason") != "completed"
        or not isinstance(recovered_quality, Mapping)
        or recovered_quality.get("automated_review_completed") is not True
        or recovered_quality.get("passed_without_override") is not True
        or recovered_quality.get("blocking_status") != "clear"
        or int(recovered_quality.get("overridden_blocking_issue_count", 0) or 0) != 0
        or recovered_quality.get("overridden_issue_ids") not in (None, [])
        or not isinstance(event_store, Mapping)
        or event_store.get("event_sequence") != event_values[-1].sequence
        or event_store.get("event_checksum") != event_values[-1].checksum
    ):
        raise M8EvaluationError(f"visual event chain does not match visual_agent_run: {pair['group_id']}")
    cost = payload.get("cost")
    approved = cost.get("approved_paid_calls") if isinstance(cost, Mapping) else None
    used = cost.get("used_paid_calls") if isinstance(cost, Mapping) else None
    if (
        not isinstance(cost, Mapping)
        or cost.get("run_id") != execution.get("run_id")
        or isinstance(approved, bool)
        or not isinstance(approved, int)
        or isinstance(used, bool)
        or not isinstance(used, int)
        or approved < 0
        or used < 0
        or used > approved
        or cost.get("actionable_uncertain_paid_jobs") not in (None, [])
        or cost.get("settled_or_zero_cost") is not True
    ):
        raise M8EvaluationError(f"pair cost evidence is unresolved or over budget: {pair['group_id']}")
    if (
        cost_record.get("run_id") != run_id
        or cost_record.get("approved_paid_calls") != approved
        or cost_record.get("used_paid_calls") != used
        or cost_record.get("actionable_uncertain_paid_jobs") not in (None, [])
    ):
        raise M8EvaluationError(f"cost_plan contradicts pair cost evidence: {pair['group_id']}")
    if expected_frozen_group is not None:
        _validate_signed_production_provenance(
            payload,
            event_log_path=verified_paths["production_event_log"],
            agent_sha256=_sha256_file(agent_path),
            source_sha256=source_sha,
            story_id=str(pair.get("story_id", "")),
            agent_run_id=run_id,
            artifact_sha256={
                name: item["sha256"]
                for name, item in verified.items()
                if name != "production_event_log"
            },
            group_id=str(pair["group_id"]),
        )
    return {"path": evidence_path.name, "sha256": _sha256_file(evidence_path)}


def _validate_signed_production_provenance(
    payload: Mapping[str, Any],
    *,
    event_log_path: Path,
    agent_sha256: str,
    source_sha256: str,
    story_id: str,
    agent_run_id: str,
    artifact_sha256: Mapping[str, str],
    group_id: str,
) -> None:
    """Verify the paid image result against the trusted ProductionRun HMAC boundary."""
    production = payload.get("production")
    if not isinstance(production, Mapping):
        raise M8EvaluationError(f"formal evidence has no ProductionRun binding: {group_id}")
    project_id = str(production.get("project_id", "")).strip()
    production_run_id = str(production.get("run_id", "")).strip()
    operation_id = str(production.get("operation_id", "")).strip()
    stage_run_id = str(production.get("stage_run_id", "")).strip()
    key_id = str(production.get("hmac_key_id", "")).strip()
    if not all((project_id, production_run_id, operation_id, stage_run_id, key_id)) or production.get("stage") != "visual":
        raise M8EvaluationError(f"formal ProductionRun binding is incomplete: {group_id}")
    configured_key = os.environ.get("MANJU_PRODUCTION_HMAC_KEY", "")
    if not configured_key:
        raise M8EvaluationError(
            "formal M8 verification requires MANJU_PRODUCTION_HMAC_KEY from the trusted ProductionRun runtime"
        )
    try:
        from manju.production.approvals import ApprovalRequest, Grant
        from manju.production.events import EventStore
        from manju.production.operations import OperationRecord
        from manju.production.security import MappingHmacKeyProvider

        event_values: list[dict[str, Any]] = []
        with event_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("ProductionRun event must be an object")
                    event_values.append(value)
        EventStore.verify(
            event_values,
            key_provider=MappingHmacKeyProvider({key_id: configured_key.encode("utf-8")}),
        )
        scoped = [
            event for event in event_values
            if event.get("project_id") == project_id and event.get("run_id") == production_run_id
        ]
        grant_event = next(event for event in scoped if event.get("event_type") == "grant_issued")
        grant = Grant.from_dict((grant_event.get("payload") or {}).get("grant", {}))
        requested = next(event for event in scoped if event.get("event_type") == "approval_requested")
        approved = next(event for event in scoped if event.get("event_type") == "approval_approved")
        requested_contract = (requested.get("payload") or {}).get("approval_request", {})
        request = ApprovalRequest.from_dict(requested_contract)
        if (
            grant.key_id != key_id
            or (grant.project_id, grant.run_id, grant.stage, grant.stage_run_id)
            != (project_id, production_run_id, "visual", stage_run_id)
            or grant.stage != "visual"
            or operation_id not in grant.operation_ids
            or not isinstance(requested_contract, Mapping)
            or requested_contract.get("request_id") != grant.request_id
            or (approved.get("payload") or {}).get("request_id") != grant.request_id
            or not grant.matches_request(request)
        ):
            raise ValueError("grant and approval do not bind the visual operation")
        source_version = {
            "artifact_id": f"m8-source:{group_id}",
            "version_id": f"sha256:{source_sha256}",
        }
        if source_version not in grant.artifact_versions:
            raise ValueError("signed grant does not bind the frozen M8 source/group")
        if any(event.get("event_type") == "grant_revoked" for event in scoped):
            raise ValueError("visual grant was revoked")
        operation_events = [
            event for event in scoped
            if event.get("event_type") in {
                "call_reserved", "call_submitted", "call_settled", "call_reconciled",
            }
            and (event.get("payload") or {}).get("operation", {}).get("operation_id") == operation_id
        ]
        event_types = [str(event.get("event_type", "")) for event in operation_events]
        if event_types not in (
            ["call_reserved", "call_submitted", "call_settled"],
            ["call_reserved", "call_submitted", "call_settled", "call_reconciled"],
        ):
            raise ValueError("visual operation lifecycle is incomplete or out of order")
        operations = [
            OperationRecord.from_dict((event.get("payload") or {}).get("operation", {}))
            for event in operation_events
        ]
        grant_binding = next(item for item in grant.operation_bindings if item.get("operation_id") == operation_id)
        if (
            operations[0].input_fingerprint != grant_binding.get("input_fingerprint")
            or operations[0].kind != grant_binding.get("kind")
            or operations[0].provider_profile != grant.provider_profile
        ):
            raise ValueError("visual operation does not match its signed Grant binding")
        if any(
            (item.operation_id, item.grant_id, item.kind, item.input_fingerprint, item.provider_profile)
            != (
                operations[0].operation_id, operations[0].grant_id, operations[0].kind,
                operations[0].input_fingerprint, operations[0].provider_profile,
            )
            for item in operations[1:]
        ):
            raise ValueError("visual operation binding changes during its lifecycle")
        terminal_event = next(
            event for event in reversed(operation_events)
            if event.get("event_type") in {"call_settled", "call_reconciled"}
        )
        operation = OperationRecord.from_dict((terminal_event.get("payload") or {}).get("operation", {}))
        if (
            operation.grant_id != grant.grant_id
            or operation.status != "settled"
            or operation.outcome != "succeeded"
            or operation.result_fingerprint != f"sha256:{agent_sha256}"
        ):
            raise ValueError("signed paid-operation result does not bind the Agent candidate bytes")
        completed = next(
            event for event in reversed(scoped)
            if event.get("event_type") == "stage_completed"
            and (event.get("payload") or {}).get("stage") == "visual"
            and (event.get("payload") or {}).get("stage_run_id") == stage_run_id
        )
        ordered_sequences = [
            requested.get("sequence", 0), approved.get("sequence", 0), grant_event.get("sequence", 0),
            *[event.get("sequence", 0) for event in operation_events], completed.get("sequence", 0),
        ]
        if ordered_sequences != sorted(ordered_sequences) or len(set(ordered_sequences)) != len(ordered_sequences):
            raise ValueError("approval, grant, operation and completion events are out of order")
        attestation = next(
            event for event in reversed(scoped)
            if event.get("event_type") == "m8_visual_evidence_attested"
        )
        attested = attestation.get("payload") or {}
        expected_attestation = {
            "group_id": group_id,
            "story_id": story_id,
            "frozen_source_sha256": source_sha256,
            "agent_run_id": agent_run_id,
            "agent_candidate_sha256": agent_sha256,
            "evidence_artifact_sha256": dict(sorted(artifact_sha256.items())),
            "stage_completed_event_hash": completed.get("event_hash"),
        }
        if (
            int(attestation.get("sequence", 0)) <= int(completed.get("sequence", 0))
            or attestation is not scoped[-1]
            or any(attested.get(name) != value for name, value in expected_attestation.items())
        ):
            raise ValueError("final signed M8 attestation does not bind the complete evidence set")
        heads_json = os.environ.get("MANJU_M8_PRODUCTION_HEADS_JSON", "")
        if not heads_json:
            raise ValueError("formal M8 verification requires the current ProductionRun head map")
        heads = json.loads(heads_json)
        head_key = f"{project_id}/{production_run_id}"
        if not isinstance(heads, Mapping) or heads.get(head_key) != attestation.get("event_hash"):
            raise ValueError("evidence log is not the externally anchored current ProductionRun head")
    except (OSError, UnicodeError, json.JSONDecodeError, StopIteration, TypeError, ValueError) as exc:
        raise M8EvaluationError(f"signed ProductionRun provenance is invalid: {group_id}") from exc
    except Exception as exc:
        # Production contracts expose typed validation failures; keep the M8
        # public surface stable without leaking key or signature details.
        raise M8EvaluationError(f"signed ProductionRun provenance is invalid: {group_id}") from exc


def _assert_mapping_path_outside(mapping_path: Path, public_dir: Path) -> None:
    try:
        mapping_path.resolve().relative_to(public_dir.resolve())
    except ValueError:
        return
    raise M8EvaluationError("private mapping must be outside the public reviewer directory")


def _relative_public_file(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _declared_bytes(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise M8EvaluationError(f"{field} must be a non-negative integer")
    return value


def _sanitize_png(data: bytes) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise M8EvaluationError("PNG image has an invalid signature")
    position = len(signature)
    output = bytearray(signature)
    required = {b"IHDR", b"IDAT", b"IEND"}
    seen: set[bytes] = set()
    while position < len(data):
        if position + 12 > len(data):
            raise M8EvaluationError("PNG image has a truncated chunk")
        length = int.from_bytes(data[position:position + 4], "big")
        chunk_type = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            raise M8EvaluationError("PNG image has a truncated chunk payload")
        chunk = data[position:end]
        stored_crc = int.from_bytes(chunk[-4:], "big")
        if zlib.crc32(chunk[4:-4]) & 0xffffffff != stored_crc:
            raise M8EvaluationError("PNG image has an invalid chunk checksum")
        if chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            raise M8EvaluationError("animated PNG is not allowed in an M8 still-image pair")
        seen.add(chunk_type)
        # Keep only image structure and transparency.  Text, EXIF, ICC and
        # other ancillary chunks are deliberately not copied to the packet.
        if chunk_type in {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}:
            output.extend(chunk)
        position = end
        if chunk_type == b"IEND":
            break
    if position != len(data) or not required.issubset(seen):
        raise M8EvaluationError("PNG image is incomplete")
    return bytes(output)


def _sanitize_jpeg(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        raise M8EvaluationError("JPEG image has an invalid signature")
    output = bytearray(data[:2])
    position = 2
    saw_scan = False
    while position < len(data):
        if data[position] != 0xff:
            raise M8EvaluationError("JPEG image has an invalid marker")
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker == 0xd9:
            output.extend((0xff, marker))
            break
        if marker in {0x01, *range(0xd0, 0xd8)}:
            output.extend((0xff, marker))
            continue
        if position + 2 > len(data):
            raise M8EvaluationError("JPEG image has a truncated segment")
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > len(data):
            raise M8EvaluationError("JPEG image has an invalid segment length")
        segment_end = position + length
        # APP0..APP15 and COM are all user-controlled containers.  Baseline
        # decoders do not require JFIF APP0, so remove the entire family.
        if not (0xe0 <= marker <= 0xef or marker == 0xfe):
            output.extend((0xff, marker))
            output.extend(data[position:segment_end])
        position = segment_end
        if marker == 0xda:
            if not data.endswith(b"\xff\xd9"):
                raise M8EvaluationError("JPEG image has no end marker")
            output.extend(data[position:])
            saw_scan = True
            position = len(data)
            break
    if not saw_scan:
        raise M8EvaluationError("JPEG image has no scan data")
    return bytes(output)


def _sanitize_webp(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise M8EvaluationError("WebP image has an invalid signature")
    position = 12
    chunks: list[bytes] = []
    allowed = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH"}
    while position + 8 <= len(data):
        chunk_type = data[position:position + 4]
        length = int.from_bytes(data[position + 4:position + 8], "little")
        end = position + 8 + length + (length & 1)
        if end > len(data):
            raise M8EvaluationError("WebP image has a truncated chunk")
        if chunk_type in {b"ANIM", b"ANMF"}:
            raise M8EvaluationError("animated WebP is not allowed in an M8 still-image pair")
        if chunk_type not in allowed:
            raise M8EvaluationError(f"WebP contains an unsupported metadata chunk: {chunk_type!r}")
        chunks.append(data[position:end])
        position = end
    if position != len(data):
        raise M8EvaluationError("WebP image has a truncated chunk")
    if not any(chunk[:4] in {b"VP8 ", b"VP8L"} for chunk in chunks):
        raise M8EvaluationError("WebP image has no still-image payload")
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _sanitized_image_bytes(source: Path) -> bytes:
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise M8EvaluationError(f"cannot read image: {source.name}") from exc
    suffix = source.suffix.lower()
    if suffix == ".png":
        return _sanitize_png(data)
    if suffix in {".jpg", ".jpeg"}:
        return _sanitize_jpeg(data)
    if suffix == ".webp":
        return _sanitize_webp(data)
    raise M8EvaluationError(f"unsupported image suffix: {source.name}")


def _load_frozen_groups(path: Path) -> tuple[dict[str, dict[str, Any]], str, str]:
    payload = _read_json(path)
    validate_sample_manifest(payload)
    repository_root = path.parent.parent.resolve()
    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise M8EvaluationError("frozen sample manifest has no source binding")
    source_manifest = _resolve_under(
        repository_root,
        str(source.get("manifest_ref", "")),
        field="source.manifest_ref",
    )
    if source_manifest.is_symlink() or not source_manifest.is_file() or _sha256_file(source_manifest) != source.get("manifest_sha256"):
        raise M8EvaluationError("frozen source manifest has changed since M8 freeze")
    for story in payload["stories"]:
        if not isinstance(story, Mapping):
            continue
        source_file = _resolve_under(repository_root, str(story.get("source_ref", "")), field="stories.source_ref")
        if (
            source_file.is_symlink()
            or not source_file.is_file()
            or source_file.stat().st_size != story.get("source_bytes")
            or _sha256_file(source_file) != story.get("source_sha256")
        ):
            raise M8EvaluationError(f"frozen source file has changed: {story.get('story_id', '')}")
    groups = {
        str(item["group_id"]): dict(item)
        for item in payload["scene_groups"]
        if isinstance(item, Mapping) and item.get("group_id")
    }
    return (
        groups,
        str(payload.get("contract_fingerprint", "")),
        str(payload.get("fingerprint", "")),
    )


def _formal_balanced_assignments(
    pairs: Sequence[Mapping[str, Any]],
    frozen_groups: Mapping[str, Mapping[str, Any]],
    *,
    seed: int,
) -> dict[str, str]:
    """Balance treatment side overall, within story, and by coverage code."""
    by_story: dict[str, list[str]] = {}
    for pair in pairs:
        by_story.setdefault(str(pair.get("story_id", "")), []).append(str(pair["group_id"]))
    story_ids = sorted(by_story)
    target_agent = len(pairs) // 2
    floor_total = sum(len(by_story[story_id]) // 2 for story_id in story_ids)
    extra_story_count = target_agent - floor_total
    if extra_story_count < 0 or extra_story_count > len(story_ids):
        raise M8EvaluationError("cannot balance formal M8 assignments by story")
    coverage_totals: dict[str, int] = {}
    for group in frozen_groups.values():
        for code in set(map(str, group.get("coverage_codes", []))):
            coverage_totals[code] = coverage_totals.get(code, 0) + 1
    rng = random.Random(seed)
    # The frozen suite repeats each ten-story coverage design twice.  Pair
    # stories with identical ordered coverage signatures and use complementary
    # side assignments.  This gives exact coverage balance while retaining at
    # least one A and one B group inside every three-group story.
    signature_buckets: dict[tuple[tuple[str, ...], ...], list[str]] = {}
    ordered_story_groups: dict[str, list[str]] = {}
    for story_id in story_ids:
        group_ids = sorted(
            by_story[story_id],
            key=lambda group_id: int(frozen_groups[group_id].get("ordinal", 0)),
        )
        ordered_story_groups[story_id] = group_ids
        signature = tuple(
            tuple(sorted(map(str, frozen_groups[group_id].get("coverage_codes", []))))
            for group_id in group_ids
        )
        signature_buckets.setdefault(signature, []).append(story_id)
    if all(len(bucket) % 2 == 0 for bucket in signature_buckets.values()):
        paired: dict[str, str] = {}
        for signature in sorted(signature_buckets, key=str):
            bucket = sorted(signature_buckets[signature])
            rng.shuffle(bucket)
            for offset in range(0, len(bucket), 2):
                first, second = bucket[offset:offset + 2]
                length = len(ordered_story_groups[first])
                first_agent_count = length // 2 + rng.randrange(2)
                first_agent_positions = set(rng.sample(range(length), first_agent_count))
                for index, group_id in enumerate(ordered_story_groups[first]):
                    paired[group_id] = "agent" if index in first_agent_positions else "legacy"
                for index, group_id in enumerate(ordered_story_groups[second]):
                    paired[group_id] = "legacy" if index in first_agent_positions else "agent"
        if sum(value == "agent" for value in paired.values()) == target_agent:
            return paired
    best: dict[str, str] | None = None
    best_score: tuple[int, int] | None = None
    for _attempt in range(50000):
        extra_stories = set(rng.sample(story_ids, extra_story_count))
        candidate: dict[str, str] = {}
        for story_id in story_ids:
            group_ids = list(by_story[story_id])
            rng.shuffle(group_ids)
            agent_count = len(group_ids) // 2 + (1 if story_id in extra_stories else 0)
            agent_groups = set(group_ids[:agent_count])
            candidate.update({
                group_id: "agent" if group_id in agent_groups else "legacy"
                for group_id in group_ids
            })
        agent_by_code = {code: 0 for code in coverage_totals}
        for group_id, treatment in candidate.items():
            if treatment != "agent":
                continue
            for code in set(map(str, frozen_groups[group_id].get("coverage_codes", []))):
                agent_by_code[code] += 1
        imbalances = [
            abs(2 * agent_by_code[code] - total)
            for code, total in coverage_totals.items()
        ]
        score = (max(imbalances, default=0), sum(imbalances))
        if best_score is None or score < best_score:
            best, best_score = candidate, score
        if score[0] <= 1:
            return candidate
    raise M8EvaluationError(
        f"cannot satisfy formal coverage-side balance; best imbalance={best_score}"
    )
def generate_blind_materials(
    *,
    pair_input_path: str | os.PathLike[str],
    public_output_dir: str | os.PathLike[str],
    private_mapping_output: str | os.PathLike[str],
    sample_manifest_path: str | os.PathLike[str] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Package existing local A/B images without invoking any image service."""
    pair_path = Path(pair_input_path).resolve()
    public_dir = Path(public_output_dir).resolve()
    mapping_path = Path(private_mapping_output).resolve()
    if public_dir.exists():
        raise M8EvaluationError(f"refusing to overwrite public material directory: {public_dir}")
    if mapping_path.exists():
        raise M8EvaluationError(f"refusing to overwrite private mapping: {mapping_path}")
    _assert_mapping_path_outside(mapping_path, public_dir)
    pairs = _load_pair_input(pair_path)
    frozen_groups: dict[str, dict[str, Any]] | None = None
    expected_groups: set[str] | None = None
    contract_fingerprint = ""
    sample_fingerprint = ""
    if sample_manifest_path:
        frozen_groups, contract_fingerprint, sample_fingerprint = _load_frozen_groups(
            Path(sample_manifest_path).resolve()
        )
        expected_groups = set(frozen_groups)
        actual_groups = {item["group_id"] for item in pairs}
        if actual_groups != expected_groups:
            missing = sorted(expected_groups - actual_groups)
            extra = sorted(actual_groups - expected_groups)
            raise M8EvaluationError(f"pair scope does not match frozen manifest; missing={missing[:3]} extra={extra[:3]}")
    if expected_groups is not None and len(expected_groups) < MINIMUM_SCENE_GROUP_COUNT:
        raise M8EvaluationError("frozen sample manifest is below the M8 scene-group minimum")

    public_dir.parent.mkdir(parents=True, exist_ok=True)
    package_dir = Path(tempfile.mkdtemp(prefix=".m8-public-", dir=str(public_dir.parent))).resolve()
    ordered = sorted(pairs, key=lambda item: item["group_id"])
    if sample_fingerprint:
        derived_seed = int(hashlib.sha256(sample_fingerprint.encode("ascii")).hexdigest()[:8], 16)
        if seed is not None and int(seed) != derived_seed:
            raise M8EvaluationError(
                f"formal M8 seed is derived from the frozen sample fingerprint and must be {derived_seed}"
            )
        rng_seed = derived_seed
    else:
        rng_seed = secrets.randbits(32) if seed is None else int(seed)
    rng = random.Random(rng_seed)
    if frozen_groups:
        assignment_by_group = _formal_balanced_assignments(
            ordered, frozen_groups, seed=rng_seed
        )
    else:
        assignment_by_group: dict[str, str] = {}
        by_story: dict[str, list[dict[str, Any]]] = {}
        for pair in ordered:
            by_story.setdefault(str(pair.get("story_id", "")), []).append(pair)
        for story_index, story_id in enumerate(sorted(by_story)):
            story_pairs = sorted(by_story[story_id], key=lambda item: item["group_id"])
            rng.shuffle(story_pairs)
            for index, pair in enumerate(story_pairs):
                assignment_by_group[pair["group_id"]] = (
                    "agent" if (index + story_index) % 2 == 0 else "legacy"
                )
    assignments = [assignment_by_group[pair["group_id"]] for pair in ordered]
    public_groups: list[dict[str, Any]] = []
    private_groups: dict[str, dict[str, Any]] = {}
    for index, (pair, agent_side) in enumerate(zip(ordered, assignments), 1):
        public_group_id = f"g{index:03d}"
        # The public package contains only neutral A/B labels.  Internal
        # treatment names are retained exclusively in the private mapping.
        source_text = pair.get("source_text")
        if isinstance(source_text, str):
            text = source_text
        else:
            source_path = Path(str(pair["source_file"])).resolve()
            if not source_path.is_file():
                raise M8EvaluationError(f"source_file is missing: {source_path}")
            try:
                text = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise M8EvaluationError("source_file must be readable UTF-8 text") from exc
        if not text.strip():
            raise M8EvaluationError(f"empty source context for {pair['group_id']}")
        input_path = package_dir / "groups" / public_group_id / "input.txt"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(text, encoding="utf-8", newline="\n")
        agent_path = Path(pair["agent_image"]).resolve()
        legacy_path = Path(pair["legacy_image"]).resolve()
        for label, media_path in (("agent_image", agent_path), ("legacy_image", legacy_path)):
            if media_path.suffix.lower() not in _IMAGE_SUFFIXES:
                raise M8EvaluationError(f"{label} must be PNG/JPEG/WebP: {media_path.name}")
            if media_path.is_symlink() or not media_path.is_file():
                raise M8EvaluationError(f"{label} is missing: {media_path}")
            if media_path.stat().st_size <= 0:
                raise M8EvaluationError(f"{label} is empty: {media_path.name}")
        evidence = _validate_pair_evidence(
            pair,
            source_bytes=text.encode("utf-8"),
            agent_path=agent_path,
            legacy_path=legacy_path,
            expected_frozen_group=frozen_groups.get(pair["group_id"]) if frozen_groups else None,
        )
        # Copy each treatment into its assigned public side.  This means A/B
        # material is never accidentally tied to the source file name.
        source_by_treatment = {"agent": agent_path, "legacy": legacy_path}
        public_media: dict[str, dict[str, Any]] = {}
        for side, treatment in (("A", agent_side), ("B", "legacy" if agent_side == "agent" else "agent")):
            destination = package_dir / "groups" / public_group_id / side / (
                "image" + source_by_treatment[treatment].suffix.lower()
            )
            source = source_by_treatment[treatment]
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                destination.write_bytes(_sanitized_image_bytes(source))
            except OSError as exc:
                raise M8EvaluationError(f"cannot write blind media: {destination.name}") from exc
            public_media[side] = _relative_public_file(package_dir, destination)
        public_input = _relative_public_file(package_dir, input_path)
        public_groups.append({
            "group_id": public_group_id,
            "input": public_input,
            "options": {"A": public_media["A"], "B": public_media["B"]},
        })
        private_groups[public_group_id] = {
            "source_group_id": pair["group_id"],
            "story_id": pair.get("story_id", ""),
            "A": agent_side,
            "B": "legacy" if agent_side == "agent" else "agent",
            "evidence": evidence,
        }

    mapping_commitment_payload = {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "seed": rng_seed,
        "groups": private_groups,
        "contract_fingerprint": contract_fingerprint,
        "sample_fingerprint": sample_fingerprint,
    }
    mapping_commitment = _sha256_canonical(mapping_commitment_payload)
    public_manifest: dict[str, Any] = {
        "schema_version": PUBLIC_MATERIAL_SCHEMA_VERSION,
        "status": "ready_for_human_review" if sample_fingerprint else "ready_for_protocol_dry_run",
        "blind": True,
        "group_count": len(public_groups),
        "groups": public_groups,
        "review_contract": {
            "dimensions": list(DIMENSIONS),
            "score_minimum": SCORE_MINIMUM,
            "score_maximum": SCORE_MAXIMUM,
            "preference_values": ["A", "B", "tie"],
            "serious_error_fields": ["codes", "note"],
        },
        "integrity": {
            "package_files_are_sha256_bound": True,
            "contract_fingerprint": contract_fingerprint,
            "sample_fingerprint": sample_fingerprint,
            "mapping_commitment_sha256": mapping_commitment,
        },
        "note": "Only A/B labels are public. Keep this package separate from the private mapping and reviewer results.",
    }
    instructions_path = package_dir / "review_instructions.md"
    instructions_path.write_text(
        "# M8 视觉盲评\n\n"
        "逐组阅读 input.txt，并只比较 A 与 B。不要根据文件名、目录顺序或任何外部信息推断来源。\n\n"
        "对每个方案按 1–5 分别评价来源忠实性、人物一致性、服装连续性、道具连续性、构图可读性、"
        "动作连续性和制作可用性；然后选择 A、B 或 tie。若标记严重错误，必须填写具体错误类别和可定位说明。\n\n"
        "请将结果填写为合同规定的 JSON，并把 reviewer_id、视角和完整组数写入文件。不要在结果中写入模型、"
        "Provider、引擎名称、绝对路径或凭据。\n",
        encoding="utf-8",
        newline="\n",
    )
    public_manifest["instructions"] = _relative_public_file(package_dir, instructions_path)
    _write_json_new(package_dir / "manifest.json", public_manifest)
    public_manifest_sha = _sha256_file(package_dir / "manifest.json")
    private_mapping = {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "status": "private_do_not_distribute",
        "seed": rng_seed,
        "group_count": len(public_groups),
        "groups": private_groups,
        "public_manifest_sha256": public_manifest_sha,
        "contract_fingerprint": contract_fingerprint,
        "sample_fingerprint": sample_fingerprint,
        "mapping_commitment_sha256": mapping_commitment,
        "note": "This file decodes A/B and must not be shared with reviewers.",
    }
    _write_json_new(mapping_path, private_mapping)
    try:
        os.rename(package_dir, public_dir)
    except OSError as exc:
        try:
            mapping_path.unlink()
        except OSError:
            pass
        raise M8EvaluationError(f"cannot publish blind material directory: {public_dir}") from exc
    return {
        "public_output_dir": str(public_dir),
        "private_mapping_output": str(mapping_path),
        "group_count": len(public_groups),
        "a_count": sum(1 for item in private_groups.values() if item["A"] == "agent"),
        "b_count": sum(1 for item in private_groups.values() if item["B"] == "agent"),
        "provider_calls": 0,
    }


def _reject_blind_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_BLIND_KEYS:
                raise M8EvaluationError(f"blind review data contains forbidden field: {path}.{key}")
            _reject_blind_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_blind_keys(child, path=f"{path}[{index}]")


def _validate_public_manifest(path: Path) -> tuple[dict[str, Any], set[str]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != PUBLIC_MATERIAL_SCHEMA_VERSION:
        raise M8EvaluationError("unsupported public M8 material schema")
    if payload.get("blind") is not True or payload.get("status") not in {
        "ready_for_human_review", "ready_for_protocol_dry_run",
    }:
        raise M8EvaluationError("public material is not marked blind and ready for review")
    _reject_blind_keys(payload)
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise M8EvaluationError("public material has no groups")
    group_ids: set[str] = set()
    root = path.parent.resolve()
    expected_files = {"manifest.json"}
    instructions = payload.get("instructions")
    if not isinstance(instructions, Mapping) or set(instructions) != {"path", "sha256", "bytes"}:
        raise M8EvaluationError("public review instructions are not integrity-bound")
    instructions_path = _resolve_under(root, str(instructions["path"]), field="instructions.path")
    instructions_bytes = _declared_bytes(instructions["bytes"], field="instructions.bytes")
    if (
        instructions_path.is_symlink()
        or not instructions_path.is_file()
        or instructions_path.stat().st_size != instructions_bytes
        or _sha256_file(instructions_path) != str(instructions["sha256"])
    ):
        raise M8EvaluationError("public review instructions integrity failed")
    expected_files.add(instructions_path.relative_to(root).as_posix())
    for index, group in enumerate(groups, 1):
        if not isinstance(group, Mapping):
            raise M8EvaluationError(f"public group {index} is invalid")
        group_id = _validate_identifier(group.get("group_id"), f"groups[{index}].group_id")
        if group_id in group_ids:
            raise M8EvaluationError("public group IDs must be unique")
        group_ids.add(group_id)
        if set(group) != {"group_id", "input", "options"}:
            raise M8EvaluationError(f"public group {group_id} has unexpected fields")
        for side in ("A", "B"):
            item = group.get("options", {}).get(side) if isinstance(group.get("options"), Mapping) else None
            if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "bytes"}:
                raise M8EvaluationError(f"public group {group_id} side {side} is invalid")
            media = _resolve_under(root, str(item["path"]), field=f"groups[{index}].options.{side}.path")
            declared_bytes = _declared_bytes(item["bytes"], field=f"groups[{index}].options.{side}.bytes")
            if not media.is_file() or media.is_symlink() or media.stat().st_size != declared_bytes:
                raise M8EvaluationError(f"public media integrity failed for {group_id}/{side}")
            if _sha256_file(media) != str(item["sha256"]):
                raise M8EvaluationError(f"public media integrity failed for {group_id}/{side}")
            expected_files.add(media.relative_to(root).as_posix())
        source = group.get("input")
        if not isinstance(source, Mapping) or set(source) != {"path", "sha256", "bytes"}:
            raise M8EvaluationError(f"public group {group_id} input is invalid")
        source_path = _resolve_under(root, str(source["path"]), field=f"groups[{index}].input.path")
        declared_bytes = _declared_bytes(source["bytes"], field=f"groups[{index}].input.bytes")
        if not source_path.is_file() or source_path.is_symlink() or source_path.stat().st_size != declared_bytes:
            raise M8EvaluationError(f"public input integrity failed for {group_id}")
        if _sha256_file(source_path) != str(source["sha256"]):
            raise M8EvaluationError(f"public input integrity failed for {group_id}")
        expected_files.add(source_path.relative_to(root).as_posix())
    if int(payload.get("group_count", -1)) != len(group_ids):
        raise M8EvaluationError("public group_count does not match groups")
    actual_files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() or item.is_symlink()
    }
    if actual_files != expected_files:
        raise M8EvaluationError(
            f"public package contains missing or unregistered files: {sorted(actual_files ^ expected_files)[:3]}"
        )
    return dict(payload), group_ids


def _validate_mapping(
    path: Path,
    expected_groups: set[str],
    public_manifest_sha256: str | None,
    expected_commitment: str | None,
) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != MAPPING_SCHEMA_VERSION:
        raise M8EvaluationError("unsupported private M8 mapping schema")
    if payload.get("status") != "private_do_not_distribute":
        raise M8EvaluationError("private mapping is not marked private")
    groups = payload.get("groups")
    if not isinstance(groups, Mapping) or set(map(str, groups)) != expected_groups:
        raise M8EvaluationError("private mapping groups do not match public materials")
    if public_manifest_sha256 and payload.get("public_manifest_sha256") != public_manifest_sha256:
        raise M8EvaluationError("private mapping is not bound to the public manifest")
    for group_id, item in groups.items():
        if not isinstance(item, Mapping) or item.get("A") not in {"agent", "legacy"} or item.get("B") not in {"agent", "legacy"}:
            raise M8EvaluationError(f"private mapping side assignment is invalid: {group_id}")
        if item["A"] == item["B"]:
            raise M8EvaluationError(f"private mapping sides are identical: {group_id}")
    commitment_payload = {
        "schema_version": payload.get("schema_version"),
        "seed": payload.get("seed"),
        "groups": groups,
        "contract_fingerprint": payload.get("contract_fingerprint", ""),
        "sample_fingerprint": payload.get("sample_fingerprint", ""),
    }
    actual_commitment = _sha256_canonical(commitment_payload)
    if (
        not expected_commitment
        or payload.get("mapping_commitment_sha256") != expected_commitment
        or actual_commitment != expected_commitment
    ):
        raise M8EvaluationError("private mapping does not match the pre-review public commitment")
    return dict(payload)


def _validate_review(
    path: Path,
    expected_groups: set[str],
    *,
    materials_manifest_sha256: str,
    mapping_commitment_sha256: str,
) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise M8EvaluationError(f"unsupported review schema: {path.name}")
    _reject_blind_keys(payload)
    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    reviewer_key = unicodedata.normalize("NFKC", reviewer_id).casefold()
    perspective = str(payload.get("perspective", "")).strip()
    if not reviewer_id or perspective not in REVIEW_PERSPECTIVES:
        raise M8EvaluationError(f"reviewer_id and perspective are required: {path.name}")
    if payload.get("materials_manifest_sha256") != materials_manifest_sha256:
        raise M8EvaluationError(f"review is not bound to the reviewed material manifest: {path.name}")
    if payload.get("mapping_commitment_sha256") != mapping_commitment_sha256:
        raise M8EvaluationError(f"review is not bound to the pre-review mapping commitment: {path.name}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != len(expected_groups):
        raise M8EvaluationError(f"review row count does not match materials: {path.name}")
    seen: set[str] = set()
    normalized_rows: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, Mapping):
            raise M8EvaluationError(f"review row {index} is invalid: {path.name}")
        group_id = _validate_identifier(raw.get("group_id"), f"{path.name}.rows[{index}].group_id")
        if group_id in seen or group_id not in expected_groups:
            raise M8EvaluationError(f"review group IDs do not match materials: {path.name}")
        seen.add(group_id)
        required_keys = {"group_id", "scores", "preference", "serious_error", "serious_error_codes", "notes"}
        if set(raw) != required_keys:
            raise M8EvaluationError(f"review row fields are not frozen: {path.name}/{group_id}")
        scores = raw["scores"]
        if not isinstance(scores, Mapping) or set(scores) != {"A", "B"}:
            raise M8EvaluationError(f"review scores must contain separate A/B values: {path.name}/{group_id}")
        score_values: dict[str, dict[str, int]] = {}
        for side in ("A", "B"):
            side_scores = scores[side]
            if not isinstance(side_scores, Mapping) or set(side_scores) != set(DIMENSIONS):
                raise M8EvaluationError(
                    f"review scores must cover every dimension for {side}: {path.name}/{group_id}"
                )
            score_values[side] = {}
            for dimension in DIMENSIONS:
                score = side_scores[dimension]
                if (
                    isinstance(score, bool)
                    or not isinstance(score, int)
                    or not SCORE_MINIMUM <= score <= SCORE_MAXIMUM
                ):
                    raise M8EvaluationError(
                        f"score out of range: {path.name}/{group_id}/{side}/{dimension}"
                    )
                score_values[side][dimension] = score
        if raw["preference"] not in {"A", "B", "tie"}:
            raise M8EvaluationError(f"invalid preference: {path.name}/{group_id}")
        serious = raw["serious_error"]
        codes = raw["serious_error_codes"]
        notes = raw["notes"]
        if not isinstance(serious, Mapping) or set(serious) != {"A", "B"} or not all(isinstance(serious[s], bool) for s in ("A", "B")):
            raise M8EvaluationError(f"serious_error must contain boolean A/B values: {path.name}/{group_id}")
        if not isinstance(codes, Mapping) or set(codes) != {"A", "B"} or not isinstance(notes, Mapping) or set(notes) != {"A", "B"}:
            raise M8EvaluationError(f"serious error details must contain A/B values: {path.name}/{group_id}")
        normalized_codes: dict[str, list[str]] = {}
        normalized_notes: dict[str, str] = {}
        for side in ("A", "B"):
            if not isinstance(codes[side], list) or any(code not in SEVERE_ERROR_CODES for code in codes[side]):
                raise M8EvaluationError(f"unknown severe-error code: {path.name}/{group_id}/{side}")
            normalized_codes[side] = list(dict.fromkeys(codes[side]))
            if not isinstance(notes[side], str):
                raise M8EvaluationError(f"review note must be text: {path.name}/{group_id}/{side}")
            normalized_notes[side] = notes[side]
            if serious[side] and (not normalized_codes[side] or len(normalized_notes[side].strip()) < 10):
                raise M8EvaluationError(f"severe error requires a code and concrete note: {path.name}/{group_id}/{side}")
            if not serious[side] and normalized_codes[side]:
                raise M8EvaluationError(f"non-severe side cannot contain severe-error codes: {path.name}/{group_id}/{side}")
        normalized_rows[group_id] = {
            "group_id": group_id,
            "scores": score_values,
            "preference": raw["preference"],
            "serious_error": {side: bool(serious[side]) for side in ("A", "B")},
            "serious_error_codes": normalized_codes,
            "notes": normalized_notes,
        }
    if seen != expected_groups:
        raise M8EvaluationError(f"review groups are incomplete: {path.name}")
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "reviewer_id": reviewer_id,
        "reviewer_key": reviewer_key,
        "perspective": perspective,
        "rows": normalized_rows,
        "source_file": path.name,
        "source_sha256": _sha256_file(path),
    }


def _aggregate(values: Sequence[float | int]) -> dict[str, Any]:
    total = sum(values)
    return {"sum": total, "count": len(values), "mean": round(total / len(values), 6) if values else None}


def _cluster_lower_bound(values: Sequence[float]) -> float | None:
    """Conservative one-sided 95% lower bound over story-level values."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    # 1.729 is t(19, .95); retaining it for n>=20 is conservative as n grows.
    critical = 1.729 if len(values) >= 20 else 2.0
    return round(mean - critical * math.sqrt(variance / len(values)), 6)


def _side_to_engine(mapping: Mapping[str, Any], side: str) -> str:
    engine = str(mapping.get(side, ""))
    if engine not in {"agent", "legacy"}:
        raise M8EvaluationError("mapping contains an unsupported treatment")
    return engine


def aggregate_reviews(
    *,
    materials_manifest_path: str | os.PathLike[str],
    private_mapping_path: str | os.PathLike[str],
    review_paths: Iterable[str | os.PathLike[str]],
    output_path: str | os.PathLike[str],
    contract_path: str | os.PathLike[str] | None = None,
    sample_manifest_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Decode three blind review files into an auditable, non-release summary."""
    materials_path = Path(materials_manifest_path).resolve()
    mapping_path = Path(private_mapping_path).resolve()
    output = Path(output_path).resolve()
    review_files = [Path(item).resolve() for item in review_paths]
    if len(review_files) != len(REVIEW_PERSPECTIVES):
        raise M8EvaluationError(f"M8 requires exactly {len(REVIEW_PERSPECTIVES)} reviewer files")
    materials, expected_groups = _validate_public_manifest(materials_path)
    integrity = materials.get("integrity")
    mapping_commitment = (
        str(integrity.get("mapping_commitment_sha256", ""))
        if isinstance(integrity, Mapping)
        else ""
    )
    materials_sha256 = _sha256_file(materials_path)
    mapping = _validate_mapping(
        mapping_path,
        expected_groups,
        materials_sha256,
        mapping_commitment,
    )
    reviews = [
        _validate_review(
            path,
            expected_groups,
            materials_manifest_sha256=materials_sha256,
            mapping_commitment_sha256=mapping_commitment,
        )
        for path in review_files
    ]
    perspectives = [item["perspective"] for item in reviews]
    if set(perspectives) != set(REVIEW_PERSPECTIVES) or len(set(perspectives)) != len(REVIEW_PERSPECTIVES):
        raise M8EvaluationError("review files must cover content, visual_production and target_user exactly once")
    reviewer_ids = [item["reviewer_id"] for item in reviews]
    reviewer_keys = [item["reviewer_key"] for item in reviews]
    if len(set(reviewer_keys)) != len(reviewer_keys):
        raise M8EvaluationError("reviewer IDs must be unique")
    contract: dict[str, Any] | None = None
    sample_manifest: dict[str, Any] | None = None
    if contract_path:
        value = _read_json(Path(contract_path).resolve())
        if not isinstance(value, Mapping) or value.get("schema_version") != CONTRACT_SCHEMA_VERSION:
            raise M8EvaluationError("unsupported M8 contract schema")
        contract = dict(value)
        declared_contract_fingerprint = str(contract.get("fingerprint", ""))
        contract_input = dict(contract)
        contract_input.pop("fingerprint", None)
        if declared_contract_fingerprint != _fingerprint(contract_input):
            raise M8EvaluationError("M8 contract fingerprint mismatch")
        if int(materials.get("group_count", 0)) < MINIMUM_SCENE_GROUP_COUNT:
            raise M8EvaluationError("full M8 visual gate requires at least 60 material groups")
        expected_contract_fingerprint = str(contract.get("fingerprint", ""))
        material_contract_fingerprint = str(
            materials.get("integrity", {}).get("contract_fingerprint", "")
            if isinstance(materials.get("integrity"), Mapping) else ""
        )
        if not expected_contract_fingerprint or material_contract_fingerprint != expected_contract_fingerprint:
            raise M8EvaluationError("public materials are not bound to the supplied M8 contract")
    if sample_manifest_path:
        value = _read_json(Path(sample_manifest_path).resolve())
        if not isinstance(value, Mapping):
            raise M8EvaluationError("unsupported M8 sample manifest")
        validate_sample_manifest(value)
        sample_manifest = dict(value)
        material_sample_fingerprint = str(
            integrity.get("sample_fingerprint", "") if isinstance(integrity, Mapping) else ""
        )
        if material_sample_fingerprint != str(sample_manifest.get("fingerprint", "")):
            raise M8EvaluationError("public materials are not bound to the supplied M8 sample manifest")
        frozen_groups = {
            str(item["group_id"]): str(item["story_id"])
            for item in sample_manifest["scene_groups"]
            if isinstance(item, Mapping)
        }
        mapped_groups = {
            str(item.get("source_group_id", "")): str(item.get("story_id", ""))
            for item in mapping["groups"].values()
            if isinstance(item, Mapping)
        }
        if mapped_groups != frozen_groups:
            raise M8EvaluationError("private mapping does not match the frozen group/story scope")

    side_scores = {side: {dimension: [] for dimension in DIMENSIONS} for side in ("A", "B")}
    engine_scores = {engine: {dimension: [] for dimension in DIMENSIONS} for engine in ("agent", "legacy")}
    winner_groups = {engine: [] for engine in ("agent", "legacy")}
    winner_groups["tie"] = []
    serious_groups = {engine: [] for engine in ("agent", "legacy")}
    groups_summary: dict[str, Any] = {}
    story_preference: dict[str, list[float]] = {}
    story_dimension_differences: dict[str, dict[str, list[float]]] = {}
    unanimous_preference_groups = 0
    for group_id in sorted(expected_groups):
        rows = [item["rows"][group_id] for item in reviews]
        story_id = str(mapping["groups"][group_id].get("story_id", ""))
        preference_votes = {
            side: sum(row["preference"] == side for row in rows)
            for side in ("A", "B", "tie")
        }
        top = max(preference_votes.values())
        winners = [side for side, count in preference_votes.items() if count == top]
        winner_side = winners[0] if len(winners) == 1 else "tie"
        winner_engine = None if winner_side == "tie" else _side_to_engine(mapping["groups"][group_id], winner_side)
        winner_groups[winner_engine or "tie"].append(group_id)
        story_preference.setdefault(story_id, []).append(1.0 if winner_engine == "agent" else 0.0)
        if max(preference_votes.values()) == len(reviews):
            unanimous_preference_groups += 1
        serious_votes = {
            side: sum(bool(row["serious_error"][side]) for row in rows)
            for side in ("A", "B")
        }
        serious_majority = {side: serious_votes[side] >= 2 for side in ("A", "B")}
        for side, is_majority in serious_majority.items():
            if is_majority:
                serious_groups[_side_to_engine(mapping["groups"][group_id], side)].append(group_id)
        for dimension in DIMENSIONS:
            group_means: dict[str, float] = {}
            for side in ("A", "B"):
                values = [row["scores"][side][dimension] for row in rows]
                side_scores[side][dimension].extend(values)
                engine = _side_to_engine(mapping["groups"][group_id], side)
                engine_scores[engine][dimension].extend(values)
                group_means[engine] = sum(values) / len(values)
            story_dimension_differences.setdefault(
                story_id, {name: [] for name in DIMENSIONS}
            )[dimension].append(group_means["agent"] - group_means["legacy"])
        groups_summary[group_id] = {
            "preference_votes": preference_votes,
            "winner_side": winner_side,
            "winner_engine": winner_engine,
            "serious_error_votes": serious_votes,
            "serious_error_majority": serious_majority,
        }

    aggregate_side = {
        side: {dimension: _aggregate(values) for dimension, values in dimensions.items()}
        for side, dimensions in side_scores.items()
    }
    aggregate_engine = {
        engine: {dimension: _aggregate(values) for dimension, values in dimensions.items()}
        for engine, dimensions in engine_scores.items()
    }

    # The dimension aggregates above retain sums rather than raw values.  A
    # simple equal-weight overall mean is deterministic and matches the frozen
    # contract; compute it from sums/counts without retaining reviewer data.
    combined_engine: dict[str, dict[str, Any]] = {}
    for engine, dimensions in aggregate_engine.items():
        total = sum(float(item["sum"]) for item in dimensions.values())
        count = sum(int(item["count"]) for item in dimensions.values())
        combined_engine[engine] = {"sum": total, "count": count, "mean": round(total / count, 6) if count else None}
    combined_side: dict[str, dict[str, Any]] = {}
    for side, dimensions in aggregate_side.items():
        total = sum(float(item["sum"]) for item in dimensions.values())
        count = sum(int(item["count"]) for item in dimensions.values())
        combined_side[side] = {"sum": total, "count": count, "mean": round(total / count, 6) if count else None}

    group_count = len(expected_groups)
    comparable_count = group_count
    agent_wins = len(winner_groups["agent"])
    source_agent = aggregate_engine["agent"]["source_fidelity"]["mean"]
    source_legacy = aggregate_engine["legacy"]["source_fidelity"]["mean"]
    character_agent = aggregate_engine["agent"]["character_consistency"]["mean"]
    character_legacy = aggregate_engine["legacy"]["character_consistency"]["mean"]
    story_win_rates = [sum(values) / len(values) for values in story_preference.values() if values]
    preference_lower_bound = _cluster_lower_bound(story_win_rates)
    dimension_lower_bounds = {
        dimension: _cluster_lower_bound([
            sum(values[dimension]) / len(values[dimension])
            for values in story_dimension_differences.values()
            if values[dimension]
        ])
        for dimension in ("source_fidelity", "character_consistency")
    }
    gate_results = {
        "review_complete": {
            "passed": True,
            "observed_reviewers": len(reviews),
            "required_reviewers": len(REVIEW_PERSPECTIVES),
        },
        "balanced_ab": {
            "passed": abs(
                sum(item["A"] == "agent" for item in mapping["groups"].values())
                - sum(item["B"] == "agent" for item in mapping["groups"].values())
            ) <= (1 if group_count % 2 else 0),
            "agent_on_a": sum(item["A"] == "agent" for item in mapping["groups"].values()),
            "agent_on_b": sum(item["B"] == "agent" for item in mapping["groups"].values()),
        },
        "preference": {
            "passed": bool(
                preference_lower_bound is not None
                and preference_lower_bound >= PREFERENCE_THRESHOLD
            ),
            "agent_wins": agent_wins,
            "comparable_groups": comparable_count,
            "win_rate": round(agent_wins / comparable_count, 6) if comparable_count else None,
            "threshold": PREFERENCE_THRESHOLD,
            "story_clustered_one_sided_95_lower_bound": preference_lower_bound,
        },
        "source_fidelity_non_inferior": {
            "passed": bool(
                dimension_lower_bounds["source_fidelity"] is not None
                and dimension_lower_bounds["source_fidelity"] >= 0
            ),
            "agent_mean": source_agent,
            "legacy_mean": source_legacy,
            "story_clustered_difference_lower_bound": dimension_lower_bounds["source_fidelity"],
        },
        "character_consistency_non_inferior": {
            "passed": bool(
                dimension_lower_bounds["character_consistency"] is not None
                and dimension_lower_bounds["character_consistency"] >= 0
            ),
            "agent_mean": character_agent,
            "legacy_mean": character_legacy,
            "story_clustered_difference_lower_bound": dimension_lower_bounds["character_consistency"],
        },
        "absolute_quality_floor": {
            "passed": source_agent >= ABSOLUTE_SCORE_FLOOR and character_agent >= ABSOLUTE_SCORE_FLOOR,
            "source_fidelity_mean": source_agent,
            "character_consistency_mean": character_agent,
            "minimum_mean": ABSOLUTE_SCORE_FLOOR,
        },
        "severe_error_non_inferior": {
            "passed": len(serious_groups["agent"]) <= len(serious_groups["legacy"]),
            "agent_majority_groups": len(serious_groups["agent"]),
            "legacy_majority_groups": len(serious_groups["legacy"]),
        },
        "absolute_severe_error_limit": {
            "passed": len(serious_groups["agent"]) / group_count <= MAX_AGENT_SEVERE_ERROR_RATE,
            "agent_majority_groups": len(serious_groups["agent"]),
            "group_count": group_count,
            "rate": round(len(serious_groups["agent"]) / group_count, 6),
            "maximum_rate": MAX_AGENT_SEVERE_ERROR_RATE,
        },
    }
    formal_scope = contract is not None and sample_manifest is not None
    visual_gate_passed = formal_scope and all(bool(item.get("passed")) for item in gate_results.values())
    status = (
        "visual_gate_passed"
        if visual_gate_passed
        else "visual_gate_failed" if formal_scope else "visual_gate_incomplete"
    )
    release_blockers = [
        "engineering_evidence_must_be_attached_from_the_M2_and_ProductionRun_test_runs",
        "three_independent_red_team_reviews_must_close_high_priority_findings",
        "clean_commit_cross_platform_regression_evidence_is_required",
    ]
    summary: dict[str, Any] = {
        "schema_version": "m8-visual-review-summary-v1",
        "status": status,
        "release_eligible": False,
        "release_blockers": release_blockers,
        "group_count": group_count,
        "reviewer_count": len(reviews),
        "perspectives": perspectives,
        "reviewer_agreement": {
            "unanimous_preference_groups": unanimous_preference_groups,
            "unanimous_preference_rate": round(unanimous_preference_groups / group_count, 6),
        },
        "contract_fingerprint": str(contract.get("fingerprint", "")) if contract else "",
        "winner_groups": winner_groups,
        "winner_counts": {key: len(value) for key, value in winner_groups.items()},
        "serious_error_majority_groups": serious_groups,
        "serious_error_majority_counts": {key: len(value) for key, value in serious_groups.items()},
        "scores_by_anonymous_side": aggregate_side,
        "scores_by_decoded_treatment": aggregate_engine,
        "combined_scores_by_anonymous_side": combined_side,
        "combined_scores_by_decoded_treatment": combined_engine,
        "groups": groups_summary,
        "gates": gate_results,
        "source_files": [
            {"role": "materials_manifest", "path": materials_path.name, "sha256": _sha256_file(materials_path)},
            {"role": "private_mapping", "path": mapping_path.name, "sha256": _sha256_file(mapping_path)},
            *[
                {"role": f"reviewer_{index}", "path": path.name, "sha256": _sha256_file(path)}
                for index, path in enumerate(review_files, 1)
            ],
        ],
        "note": "This summary decodes human review data; it does not claim that the images passed an automated visual test or that formal release gates are complete.",
    }
    _write_json_new(output, summary)
    return {
        "output_path": str(output),
        "status": status,
        "release_eligible": False,
        "group_count": group_count,
        "agent_win_rate": gate_results["preference"]["win_rate"],
    }


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "DIMENSIONS",
    "M8EvaluationError",
    "MAPPING_SCHEMA_VERSION",
    "MINIMUM_SCENE_GROUP_COUNT",
    "MINIMUM_STORY_COUNT",
    "PAIR_INPUT_SCHEMA_VERSION",
    "PUBLIC_MATERIAL_SCHEMA_VERSION",
    "REVIEW_PERSPECTIVES",
    "REVIEW_SCHEMA_VERSION",
    "REQUIRED_COVERAGE_CODES",
    "SEVERE_ERROR_CODES",
    "SAMPLE_MANIFEST_SCHEMA_VERSION",
    "aggregate_reviews",
    "build_contract",
    "build_sample_manifest",
    "freeze_m8",
    "generate_blind_materials",
    "validate_sample_manifest",
]
