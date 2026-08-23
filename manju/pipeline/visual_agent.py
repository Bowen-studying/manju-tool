"""LangGraph supervisor Agent for approval-gated visual asset production.

The language model chooses actions through a provider-neutral JSON protocol.
Code owns paid-call authorization, approval freshness, foundation ordering,
file/schema validity and completion gates.
"""

from __future__ import annotations

import json
import copy
import functools
import hashlib
import os
import re
import shutil
import socket
import ssl
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Literal, TypedDict

from manju.knowledge.production_playbook import PLAYBOOK_VERSION, get_playbook_sections
from manju.pipeline.generate_image import (
    DEFAULT_SIZE,
    generate_image_with_references,
    get_image_provider_capabilities,
)
from manju.pipeline.storyboard_schema import (
    get_characters,
    get_prompt,
    get_scene_heading,
    get_style_anchor,
    get_visual,
    validate_storyboard,
)
from manju.pipeline.visual.approvals import (
    decision_template,
    is_placeholder_review_text as _core_is_placeholder_review_text,
    validate_common_decision,
)
from manju.pipeline.visual.commands import (
    next_visual_command,
    recommended_visual_command,
)
from manju.pipeline.visual.constraints import (
    compile_fallback_constraints,
    compile_shot_constraints,
    detect_constraint_conflicts,
    fallback_constraint_envelope,
    prioritize_reference_assets,
    prompt_constraint_envelope,
)
from manju.pipeline.visual.calibration import (
    calibrate_verdict,
    calibration_summary,
    load_calibration_profile,
)
from manju.pipeline.visual.escalation import (
    ESCALATION_SCHEMA_VERSION,
    ESCALATION_STRATEGY_VERSION,
    build_constraint_isolation_tasks,
    isolation_prompt_envelope,
)
from manju.pipeline.visual.identity import (
    compatibility_report,
    create_run_identity,
    identity_from_dict,
)
from manju.pipeline.visual.projections import architecture_manifest, projection_metadata
from manju.pipeline.visual.review import (
    blocking_verdict_is_actionable,
    normalize_issue_verdict,
)
from manju.pipeline.visual.store import VisualEventStore, recover_current_state
from manju.utils.ai import call_llm, parse_json_response
from manju.utils.config import load_manju_env
from manju.utils.runtime import (
    atomic_write_json,
    content_fingerprint,
    file_data_url,
    join_api_url,
    read_json,
    safe_filename,
)


VISUAL_AGENT_VERSION = "4.0"
VISUAL_TOOLSET_VERSION = "4.0.0-rc2"
VISUAL_RECOVERY_PATCH_VERSION = "4.0.0-provider-escalation-rc2"
FOUNDATION_PHASES = (
    "style", "character_identity", "character_turnaround",
    "character_expression_pose", "location", "prop",
)
ALLOWED_ACTIONS = {
    "inspect_storyboard", "retrieve_playbook", "build_visual_bible",
    "request_foundation_approval", "generate_foundation_candidates",
    "rank_foundation_candidates", "request_foundation_lock",
    "build_scene_groups", "request_scene_group_approval",
    "generate_scene_group", "inspect_scene_group", "revise_scene_group",
    "finalize_scene_group", "request_manual_review", "finalize_vision_recheck", "finalize",
    "stop_needs_review",
}
STAGE_ACTIONS = {
    "new": {"inspect_storyboard"}, "inspected": {"build_visual_bible"},
    "planned": {"request_foundation_approval"},
    "foundation_retry_approval": {"request_foundation_approval"},
    "foundation_generate": {"generate_foundation_candidates"},
    "foundation_rank": {"rank_foundation_candidates"},
    "foundation_lock": {"request_foundation_lock"},
    "foundation_complete": {"build_scene_groups"},
    "group_approval": {"request_scene_group_approval"},
    "group_generate": {"generate_scene_group"},
    "group_review": {"inspect_scene_group"},
    "group_retry": {"revise_scene_group"},
    "group_finalize": {"finalize_scene_group"},
    "manual_review": {"request_manual_review"},
    "vision_recheck_finalize": {"finalize_vision_recheck"},
    "ready_finalize": {"finalize"},
}


class VisualAgentState(TypedDict, total=False):
    run_id: str
    run_identity: dict
    run_invocation_contract: dict
    invocation_compatibility: dict
    event_sequence: int
    event_checksum: str
    compiled_constraints_by_shot: dict[str, list[dict]]
    fallback_constraints_by_shot: dict[str, dict]
    status: str
    stop_reason: str
    stage: str
    storyboard_path: str
    output_dir: str
    storyboard: dict
    inventory: dict
    visual_bible: dict
    foundation_assets: list[dict]
    foundation_phase_index: int
    candidates: dict[str, list[dict]]
    rankings: dict[str, dict]
    locked_assets: dict[str, dict]
    scene_groups: list[dict]
    current_group_index: int
    group_states: dict[str, dict]
    issues: list[dict]
    preflight_issues: list[dict]
    quality_gate: dict
    pending_approval: dict
    foundation_budget_approved: bool
    paid_authorized: bool
    counters: dict
    run_budget_usage: dict
    invocation_budget_history: list[dict]
    budgets: dict
    action: str
    action_args: dict
    decision_summary: str
    invalid_actions: int
    no_progress: int
    last_progress_fingerprint: str
    supervisor_unavailable_this_invocation: bool
    trace_seq: int
    invocation_count: int
    provider_capabilities: dict
    size: str
    target_aspect_ratio: float
    aspect_mode: str
    approval_grants: dict
    foundation_grant_id: str
    foundation_primary_grant_id: str
    foundation_retry_grant_id: str
    paid_ledger: dict
    vision_failure_history: list[dict]
    verification_history: list[dict]
    vision_recheck_only: bool
    vision_recheck_group_ids: list[str]
    vision_repair_mode: bool
    vision_confidence_calibration: dict
    provider_escalation_mode: bool
    repair_source_run_id: str
    repair_group_ids: list[str]
    repair_plan: dict
    repair_history: list[dict]
    resume_migration_contract: dict
    foundation_reset: dict
    foundation_candidate_history: dict[str, list[dict]]


SupervisorProvider = Callable[[dict], dict | str | None]
ImageProvider = Callable[[str, str, list[str], str], str | None]
VisionProvider = Callable[[str, list[str], dict], dict | None]


def _configured_image_parallelism() -> int:
    env = load_manju_env()
    try:
        value = int(env.get("MANJU_IMAGE_MAX_PARALLEL", "4"))
    except (TypeError, ValueError):
        value = 4
    return max(1, min(16, value))


def _configured_aspect_mode() -> str:
    mode = str(load_manju_env().get("MANJU_IMAGE_ASPECT_MODE", "cover")).strip().lower()
    aliases = {"contain": "contain_blur", "crop": "cover"}
    mode = aliases.get(mode, mode)
    return mode if mode in {"cover", "contain_blur", "strict"} else "cover"


def _storyboard_aspect(storyboard: dict) -> float:
    value = str(storyboard.get("creative_bible", {}).get("aspect_ratio", "9:16"))
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)\s*", value)
    if not match or float(match.group(2)) <= 0:
        return 9 / 16
    return max(0.1, min(10.0, float(match.group(1)) / float(match.group(2))))


def _request_size_for_aspect(storyboard: dict, requested: str | None) -> str:
    if requested and str(requested).strip().lower() != "auto":
        return str(requested).strip()
    ratio = _storyboard_aspect(storyboard)
    configured = load_manju_env().get("MANJU_IMAGE_SUPPORTED_SIZES", "")
    candidates = [value.strip() for value in configured.split(",") if value.strip()]
    if not candidates:
        candidates = ["1024x1024", "1024x1536", "1536x1024"]
    parsed: list[tuple[float, str]] = []
    for candidate in candidates:
        match = re.fullmatch(r"(\d+)x(\d+)", candidate.lower())
        if match and int(match.group(2)) > 0:
            parsed.append((int(match.group(1)) / int(match.group(2)), candidate.lower()))
    return min(parsed, key=lambda item: abs(item[0] - ratio))[1] if parsed else DEFAULT_SIZE


def _normalize_shot_canvas(path: str, target_ratio: float, mode: str = "cover") -> dict:
    """Normalize provider output using an explicit, auditable aspect policy."""
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if width < 1 or height < 1:
        raise ValueError("generated image has invalid dimensions")
    actual_ratio = width / height
    if abs(actual_ratio - target_ratio) / target_ratio <= 0.015:
        return {"original_size": [width, height], "actual_size": [width, height],
                "target_aspect_ratio": target_ratio, "postprocessed": False,
                "method": "native"}
    if mode == "strict":
        raise ValueError(
            f"provider output {width}x{height} does not match target ratio {target_ratio:.6f}"
        )
    if actual_ratio < target_ratio:
        target_size = (max(width, int(round(height * target_ratio))), height)
    else:
        target_size = (width, max(height, int(round(width / target_ratio))))
    if mode == "cover":
        normalized = ImageOps.fit(
            image, target_size, method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        method = "cover_center_crop"
        padding_fraction = 0.0
    else:
        normalized = ImageOps.fit(image, target_size, method=Image.Resampling.LANCZOS).filter(
            ImageFilter.GaussianBlur(radius=max(8, min(target_size) // 40))
        )
        foreground = ImageOps.contain(image, target_size, method=Image.Resampling.LANCZOS)
        x = (target_size[0] - foreground.width) // 2
        y = (target_size[1] - foreground.height) // 2
        normalized.paste(foreground, (x, y))
        method = "contain_on_blurred_canvas"
        padding_fraction = 1.0 - ((foreground.width * foreground.height) /
                                  (target_size[0] * target_size[1]))
    normalized.save(path, format="PNG")
    return {"original_size": [width, height], "actual_size": list(target_size),
            "target_aspect_ratio": target_ratio, "postprocessed": True,
            "method": method, "padding_fraction": round(padding_fraction, 6)}


def _record_shot_dimensions(path: str, details: dict, provenance: dict | None = None) -> None:
    metadata_path = path + ".manju.json"
    metadata = read_json(metadata_path) or {}
    metadata["dimensions"] = details
    if isinstance(provenance, dict):
        metadata["production"] = {
            key: value for key, value in provenance.items()
            if key in {
                "shot_id", "group_id", "logical_job_id", "ledger_job_id",
                "visible_character_ids", "visible_prop_ids", "reference_asset_ids",
                "previous_shot_reference_path", "previous_shot_reference_role",
                "primary_reference_role", "primary_reference_asset_ids", "reference_strategy",
                "revision_reference_board", "temporal_context_shot_ids",
                "temporal_context_paths", "provider_reference_mode",
                "provider_reference_paths", "excluded_image_reference_paths",
                "failed_shot_reference_included", "temporal_image_references_excluded",
            }
        }
    atomic_write_json(metadata_path, metadata)


def _revision_board_manifests(output_dir: str) -> list[dict]:
    boards_root = os.path.join(output_dir, "assets", "reference_boards")
    records: list[dict] = []
    if not os.path.isdir(boards_root):
        return records
    for root, _dirs, files in os.walk(boards_root):
        for name in files:
            if not name.endswith(".png.manju.json"):
                continue
            manifest_path = os.path.join(root, name)
            manifest = read_json(manifest_path)
            if not isinstance(manifest, dict) or manifest.get("board_role") != "targeted_revision":
                continue
            records.append({
                "manifest": manifest,
                "manifest_path": manifest_path,
                "board_path": manifest_path.removesuffix(".manju.json"),
            })
    return records


def reconcile_visual_metadata(storyboard_json: str, output_dir: str | None = None) -> dict:
    """Backfill revision provenance from local board manifests without model or image calls."""
    storyboard_path = os.path.abspath(storyboard_json)
    output_dir = os.path.abspath(output_dir or os.path.dirname(storyboard_path))
    manifest = read_json(os.path.join(output_dir, "visual_agent_run.json"))
    run_id = str(manifest.get("run_id", "")) if isinstance(manifest, dict) else ""
    if not run_id:
        raise ValueError("metadata reconcile requires an existing visual_agent_run.json")
    state_path = os.path.join(
        output_dir, "stages", "visual_agent", "runs", run_id, "state.json"
    )
    state = read_json(state_path)
    if not isinstance(state, dict):
        raise ValueError("metadata reconcile requires the current visual Agent state")
    boards = _revision_board_manifests(output_dir)
    scanned = 0
    updated = 0
    already_complete = 0
    skipped_non_targeted = 0
    missing: list[dict] = []
    for group_id, group_state in state.get("group_states", {}).items():
        generated = group_state.get("generated", {}) if isinstance(group_state, dict) else {}
        for shot_id, relative in generated.items():
            relative = str(relative)
            retry_match = re.search(r"_retry(\d{2})_", os.path.basename(relative))
            if not retry_match:
                continue
            board_id = "{}_{}_retry{}".format(
                str(group_id), safe_filename(str(shot_id)), retry_match.group(1)
            )
            board_id_candidates = [
                record for record in boards
                if str(record["manifest"].get("board_id", "")) == board_id
            ]
            image_path = os.path.join(output_dir, relative)
            metadata_path = image_path + ".manju.json"
            metadata = read_json(metadata_path)
            if not isinstance(metadata, dict):
                if board_id_candidates:
                    missing.append({
                        "group_id": str(group_id), "shot_id": str(shot_id),
                        "reason": "targeted revision image sidecar is missing", "path": relative,
                    })
                else:
                    skipped_non_targeted += 1
                continue
            references = {
                os.path.basename(str(value)) for value in metadata.get("references", [])
                if str(value)
            } if isinstance(metadata.get("references"), list) else set()
            candidates = [
                record for record in boards
                if os.path.basename(record["board_path"]) in references
            ]
            if not candidates:
                candidates = board_id_candidates
            if not candidates:
                skipped_non_targeted += 1
                continue
            scanned += 1
            if len(candidates) > 1:
                relative_parts = relative.replace("\\", "/").split("/")
                image_run_id = relative_parts[2] if len(relative_parts) > 2 else ""
                same_run = [
                    record for record in candidates
                    if image_run_id
                    and image_run_id in os.path.normpath(record["board_path"]).split(os.sep)
                ]
                if len(same_run) == 1:
                    candidates = same_run
            if len(candidates) != 1:
                missing.append({
                    "group_id": str(group_id), "shot_id": str(shot_id),
                    "reason": "targeted revision board manifest is missing or ambiguous",
                    "path": relative, "candidate_count": len(candidates),
                })
                continue
            record = candidates[0]
            board = record["manifest"]
            temporal = [
                item for item in board.get("temporal_context", []) if isinstance(item, dict)
            ]
            board_primary_role = str(
                board.get("primary_reference_role", "")
            ) or "previous_shot_edit"
            provenance = {
                "previous_shot_reference_path": str(board.get("primary_shot_reference", "")),
                "previous_shot_reference_role": (
                    "excluded_nonconverging_source"
                    if board.get("failed_shot_reference_included") is False
                    else "primary_edit_reference"
                    if board_primary_role == "previous_shot_edit"
                    else "edit_target_reference"
                ),
                "primary_reference_role": board_primary_role,
                "primary_reference_asset_ids": list(
                    board.get("reference_strategy", {}).get("primary_asset_ids", [])
                ) if isinstance(board.get("reference_strategy"), dict) else [],
                "reference_strategy": (
                    dict(board.get("reference_strategy", {}))
                    if isinstance(board.get("reference_strategy"), dict) else {}
                ),
                "revision_reference_board": os.path.relpath(record["board_path"], output_dir),
                "temporal_context_shot_ids": [str(item.get("shot_id", "")) for item in temporal],
                "temporal_context_paths": [str(item.get("path", "")) for item in temporal],
            }
            production = dict(metadata.get("production", {}))
            if all(production.get(key) == value for key, value in provenance.items()):
                already_complete += 1
                continue
            production.update(provenance)
            metadata["production"] = production
            metadata["metadata_reconciliation"] = {
                "toolset_version": VISUAL_TOOLSET_VERSION,
                "source": "targeted_revision_board_manifest",
                "reconciled_at": _now(),
            }
            atomic_write_json(metadata_path, metadata)
            updated += 1
    return {
        "status": "completed" if not missing else "needs_review",
        "toolset_version": VISUAL_TOOLSET_VERSION,
        "run_id": run_id,
        "output_dir": output_dir,
        "scanned_revision_sidecars": scanned,
        "updated_sidecars": updated,
        "already_complete_sidecars": already_complete,
        "skipped_non_targeted_sidecars": skipped_non_targeted,
        "missing_or_ambiguous": missing,
        "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
        "image_calls_after": int(state.get("counters", {}).get("image_calls", 0)),
        "model_calls_made": 0,
        "vision_calls_made": 0,
        "image_calls_made": 0,
        "quality_gate_unchanged": True,
        "paid_ledger_unchanged": True,
    }


def reconcile_paid_artifacts(storyboard_json: str, output_dir: str | None = None) -> dict:
    """Close durable paid artifacts and approval state without calling any API."""
    storyboard_path = os.path.abspath(storyboard_json)
    output_dir = os.path.abspath(output_dir or os.path.dirname(storyboard_path))
    with _visual_invocation_lease(output_dir):
        manifest = read_json(os.path.join(output_dir, "visual_agent_run.json"))
        event_state = recover_current_state(output_dir)
        run_id = (
            str(event_state.get("run_id", ""))
            if isinstance(event_state, dict) else
            str(manifest.get("run_id", "")) if isinstance(manifest, dict) else ""
        )
        if not run_id:
            raise ValueError(
                "paid artifact reconcile requires an event-store current run or visual_agent_run.json"
            )
        state_path = os.path.join(
            output_dir, "stages", "visual_agent", "runs", run_id, "state.json"
        )
        state = event_state
        if not isinstance(state, dict) or str(state.get("run_id", "")) != run_id:
            state = read_json(state_path)
        if not isinstance(state, dict):
            raise ValueError("paid artifact reconcile requires the current visual Agent state")
        state["output_dir"] = output_dir
        state["storyboard_path"] = storyboard_path
        image_calls_before = int(state.get("counters", {}).get("image_calls", 0))
        _activate_trace(state, reset_private=False)
        backfilled_contracts = _backfill_foundation_reference_contracts(state)
        reconciliation = _reconcile_durable_progress(state)
        if backfilled_contracts:
            reconciliation["foundation_reference_contracts_backfilled"] = backfilled_contracts
        transition = _normalize_recoverable_paid_state(state)
        if transition is None:
            transition = _prepare_technical_retry_transition(state, "offline_reconcile")
        if transition is None:
            group = _current_group(state)
            if group:
                group_state = state.get("group_states", {}).get(
                    str(group.get("group_id", "")), {}
                )
                blocking = [
                    issue for issue in group_state.get("issues", [])
                    if isinstance(issue, dict) and issue.get("blocking") is True
                ] if isinstance(group_state, dict) else []
                transition = (
                    _close_blocked_post_transfer_scale_evidence_reconstruction(
                        state, group, group_state, blocking, "offline_reconcile"
                    )
                    or _prepare_post_transfer_scale_evidence_reconstruction(
                        state, group, group_state, blocking, "offline_reconcile"
                    )
                    or _close_blocked_post_foundation_reset_transfer(
                        state, group, group_state, blocking, "offline_reconcile"
                    )
                    or _prepare_post_foundation_reset_transfer(
                        state, group, group_state, blocking, "offline_reconcile"
                    )
                )
        if transition is None:
            transition = _reclassify_scale_reference_nonconvergence(
                state, "offline_reconcile"
            )
        if transition and state.get("stage") == "foundation_retry_approval":
            approval = _tool_request_foundation_approval(state)
        elif (
            transition
            and transition.get("operation") == "technical_retry"
            and state.get("stage") == "group_approval"
        ):
            approval = _tool_request_scene_group_approval(state)
        elif (
            transition
            and transition.get("operation") == "scale_evidence_reconstruction"
        ):
            state["stage"] = "manual_review"
            approval = (
                _tool_request_manual_review(state)
                if not state.get("pending_approval")
                else copy.deepcopy(state.get("pending_approval", {}))
            )
        else:
            approval = {}
        stored_invocation_contract = state.get("run_invocation_contract")
        invocation_contract = (
            copy.deepcopy(stored_invocation_contract)
            if isinstance(stored_invocation_contract, dict) and stored_invocation_contract
            else _resume_contract_from_state(state, manifest or {})
        )
        _attach_v4_run_identity(
            state,
            invocation_contract,
            run_kind="legacy_migration" if not state.get("run_identity") else "production",
        )
        state["resume_migration_contract"] = {
            "schema_version": 1,
            "migrated_run_id": run_id,
            "source_toolset_version": str(
                manifest.get("toolset_version", "") if isinstance(manifest, dict) else
                state.get("resume_migration_contract", {}).get("target_toolset_version", "")
            ),
            "target_toolset_version": VISUAL_TOOLSET_VERSION,
            "invocation_contract": invocation_contract,
            "created_at": _now(),
        }
        _trace(state, "paid_artifact_reconcile_completed", {
            "reconciliation": reconciliation,
            "transition": transition or {},
            "approval": approval,
            "resume_migration_contract": state["resume_migration_contract"],
            "api_calls_made": 0,
        })
        persisted = _persist_artifacts(state)
        return {
            "status": persisted.get("status", state.get("status", "")),
            "stop_reason": persisted.get("stop_reason", state.get("stop_reason", "")),
            "stage": persisted.get("stage", state.get("stage", "")),
            "run_id": run_id,
            "output_dir": output_dir,
            "toolset_version": VISUAL_TOOLSET_VERSION,
            "recovery_patch_version": VISUAL_RECOVERY_PATCH_VERSION,
            "reconciliation": reconciliation,
            "code_owned_transition": transition or {},
            "approval": approval,
            "image_calls_before": image_calls_before,
            "image_calls_after": int(state.get("counters", {}).get("image_calls", 0)),
            "model_calls_made": 0,
            "vision_calls_made": 0,
            "image_calls_made": 0,
        }


def prepare_provider_escalation(
    storyboard_json: str,
    output_dir: str | None = None,
) -> dict:
    """Build one exact, zero-API constraint-isolation plan after non-convergence."""
    storyboard_path = os.path.abspath(storyboard_json)
    output_dir = os.path.abspath(output_dir or os.path.dirname(storyboard_path))
    with _visual_invocation_lease(output_dir):
        manifest = read_json(os.path.join(output_dir, "visual_agent_run.json"))
        state = recover_current_state(output_dir)
        run_id = str(state.get("run_id", "")) if isinstance(state, dict) else str(
            manifest.get("run_id", "") if isinstance(manifest, dict) else ""
        )
        if not run_id:
            raise ValueError("provider escalation requires an existing visual run")
        if not isinstance(state, dict):
            state = read_json(os.path.join(
                output_dir, "stages", "visual_agent", "runs", run_id, "state.json"
            ))
        if not isinstance(state, dict):
            raise ValueError("provider escalation source state is missing")
        if (
            state.get("status") != "needs_review"
            or state.get("stop_reason") != "scene_group_non_converging"
        ):
            raise ValueError(
                "provider escalation requires a reviewed scene_group_non_converging state"
            )
        state["output_dir"] = output_dir
        state["storyboard_path"] = storyboard_path
        image_model = str(
            state.get("run_invocation_contract", {}).get("models", {}).get("image_model", "")
        )
        strategy_key = "provider_strategy_" + content_fingerprint(
            ESCALATION_STRATEGY_VERSION,
            image_model,
            state.get("provider_capabilities", {}),
            length=20,
        )
        existing_plan = state.get("repair_plan", {})
        if (
            isinstance(existing_plan, dict)
            and existing_plan.get("status") == "provider_escalation_proposed"
            and existing_plan.get("strategy", {}).get("strategy_key") == strategy_key
        ):
            existing_tasks = [
                task for group in existing_plan.get("groups", [])
                for task in group.get("tasks", []) if isinstance(task, dict)
            ]
            return {
                "status": state.get("status", ""),
                "stop_reason": state.get("stop_reason", ""),
                "run_id": run_id,
                "repair_plan_status": existing_plan["status"],
                "task_ids": [str(task.get("task_id", "")) for task in existing_tasks],
                "shot_ids": list(map(str, existing_plan.get("shot_ids", []))),
                "maximum_paid_calls": int(existing_plan.get("maximum_paid_calls", 0) or 0),
                "required_provider_capabilities": list(
                    existing_plan.get("strategy", {}).get("required_provider_capabilities", [])
                ),
                "idempotent": True,
                "model_calls_made": 0,
                "vision_calls_made": 0,
                "image_calls_made": 0,
            }
        attempted_strategy_keys = {
            str(item.get("strategy", {}).get("strategy_key", ""))
            for item in state.get("repair_history", []) if isinstance(item, dict)
        }
        if state.get("provider_escalation_mode") or strategy_key in attempted_strategy_keys:
            raise ValueError(
                "this provider/model already used the current constraint-isolation strategy; "
                "a distinct provider capability or strategy version is required"
            )
        foundation = _foundation_asset_index(state)
        scale_asset_ids = {
            asset_id for asset_id, asset in foundation.items()
            if _asset_scale_contract(asset).get("required") is True
        }
        group_index = {
            str(group.get("group_id", "")): group
            for group in state.get("scene_groups", []) if isinstance(group, dict)
        }
        plan_groups: list[dict] = []
        all_tasks: list[dict] = []
        for group_id, group_state in state.get("group_states", {}).items():
            if not isinstance(group_state, dict):
                continue
            blocking = [
                copy.deepcopy(issue) for issue in group_state.get("issues", [])
                if isinstance(issue, dict) and issue.get("blocking") is True
            ]
            tasks = build_constraint_isolation_tasks(
                blocking, scale_asset_ids=scale_asset_ids
            )
            if not tasks:
                continue
            marker = {
                "schema_version": ESCALATION_SCHEMA_VERSION,
                "strategy_version": ESCALATION_STRATEGY_VERSION,
                "status": "proposed",
                "group_id": str(group_id),
                "tasks": tasks,
                "shot_ids": [task["shot_id"] for task in tasks],
                "maximum_paid_calls": len(tasks),
            }
            group_state["provider_escalation"] = marker
            plan_groups.append({
                "group_id": str(group_id),
                "shot_ids": marker["shot_ids"],
                "maximum_paid_calls": marker["maximum_paid_calls"],
                "tasks": copy.deepcopy(tasks),
                "issues": blocking,
                "scene_group": copy.deepcopy(group_index.get(str(group_id), {})),
            })
            all_tasks.extend(tasks)
        if not all_tasks:
            raise ValueError(
                "provider escalation found no evidence-backed blocking constraint tasks"
            )
        previous_plan = copy.deepcopy(state.get("repair_plan", {}))
        required_capabilities = sorted({
            capability for task in all_tasks
            for capability in task.get("required_provider_capabilities", [])
        })
        plan = {
            "schema_version": "2.0",
            "status": "provider_escalation_proposed",
            "source_run_id": run_id,
            "created_at": _now(),
            "requires_new_paid_grant": True,
            "maximum_paid_calls": len(all_tasks),
            "group_ids": [item["group_id"] for item in plan_groups],
            "shot_ids": [task["shot_id"] for task in all_tasks],
            "groups": plan_groups,
            "strategy": {
                "schema_version": ESCALATION_SCHEMA_VERSION,
                "strategy_version": ESCALATION_STRATEGY_VERSION,
                "strategy_key": strategy_key,
                "name": "constraint_isolated_edit",
                "one_active_constraint_per_shot": True,
                "preserve_unaffected_pixels": True,
                "exclude_temporal_image_references": True,
                "required_provider_capabilities": required_capabilities,
                "source_provider_capabilities": copy.deepcopy(
                    state.get("provider_capabilities", {})
                ),
                "source_image_model": image_model,
            },
            "source_nonconvergence": previous_plan,
            "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
        }
        state["repair_plan"] = plan
        _trace(state, "provider_escalation_prepared", {
            "task_ids": [task["task_id"] for task in all_tasks],
            "shot_ids": plan["shot_ids"],
            "maximum_paid_calls": plan["maximum_paid_calls"],
            "api_calls_made": 0,
        })
        persisted = _persist_artifacts(state)
        return {
            "status": persisted.get("status", ""),
            "stop_reason": persisted.get("stop_reason", ""),
            "run_id": run_id,
            "repair_plan_status": plan["status"],
            "task_ids": [task["task_id"] for task in all_tasks],
            "shot_ids": plan["shot_ids"],
            "maximum_paid_calls": plan["maximum_paid_calls"],
            "required_provider_capabilities": required_capabilities,
            "model_calls_made": 0,
            "vision_calls_made": 0,
            "image_calls_made": 0,
        }


def _record_foundation_metadata(path: str, asset_id: str, candidate_id: str) -> dict:
    """Persist provider output facts for auditable foundation assets."""
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    if width < 1 or height < 1:
        raise ValueError("generated foundation image has invalid dimensions")
    details = {
        "asset_id": asset_id,
        "candidate_id": candidate_id,
        "original_size": [width, height],
        "actual_size": [width, height],
        "actual_aspect_ratio": round(width / height, 8),
        "postprocessed": False,
        "method": "provider_output_unmodified",
        "file_sha256": _file_fingerprint(path),
    }
    metadata_path = path + ".manju.json"
    metadata = read_json(metadata_path) or {}
    metadata["foundation_image"] = details
    atomic_write_json(metadata_path, metadata)
    return details


def _run_image_jobs(image_provider: ImageProvider, jobs: list[dict],
                    parallelism: int) -> list[dict]:
    """Run independent image calls concurrently and return results in job order."""
    if not jobs:
        return []

    def execute(job: dict) -> dict:
        try:
            result = image_provider(
                job["prompt"], job["output_path"], job["references"], job["size"]
            )
            valid = bool(result and os.path.isfile(result) and os.path.getsize(result) > 0)
            return {**job, "result": result if valid else None,
                    "error": "" if valid else "provider returned no valid image"}
        except Exception as exc:
            return {**job, "result": None, "error": f"{type(exc).__name__}: {exc}"[:500]}

    if parallelism <= 1 or len(jobs) == 1:
        return [execute(job) for job in jobs]
    completed: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(parallelism, len(jobs))) as executor:
        futures = {executor.submit(execute, job): index for index, job in enumerate(jobs)}
        for future in as_completed(futures):
            completed[futures[future]] = future.result()
    return [completed[index] for index in range(len(jobs))]


_PAID_LEDGER_LOCK = threading.RLock()
_INVOCATION_REGISTRY_LOCK = threading.Lock()
_ACTIVE_INVOCATION_LEASES: set[str] = set()


@contextmanager
def _visual_invocation_lease(output_dir: str):
    """Hold one cross-process visual-agent invocation per output directory."""
    lease_dir = os.path.join(output_dir, "stages", "visual_agent")
    os.makedirs(lease_dir, exist_ok=True)
    lease_path = os.path.abspath(os.path.join(lease_dir, "invocation.lock"))
    registry_key = os.path.normcase(lease_path)
    with _INVOCATION_REGISTRY_LOCK:
        if registry_key in _ACTIVE_INVOCATION_LEASES:
            raise RuntimeError(
                f"visual_agent_run_already_active: invocation lease is held for {output_dir}"
            )
        _ACTIVE_INVOCATION_LEASES.add(registry_key)

    handle = None
    locked = False
    try:
        handle = open(lease_path, "a+b")
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(
                    f"visual_agent_run_already_active: invocation lease is held for {output_dir}"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(
                    f"visual_agent_run_already_active: invocation lease is held for {output_dir}"
                ) from exc
        locked = True
        metadata = json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "acquired_at": _now(),
        }, ensure_ascii=True).encode("utf-8")
        handle.seek(0)
        handle.truncate()
        handle.write(metadata)
        handle.flush()
        os.fsync(handle.fileno())
        yield lease_path
    finally:
        if handle is not None:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        with _INVOCATION_REGISTRY_LOCK:
            _ACTIVE_INVOCATION_LEASES.discard(registry_key)


def _exclusive_visual_invocation(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapped(storyboard_json: str, output_dir: str | None = None, *args, **kwargs):
        storyboard_path = os.path.abspath(storyboard_json)
        resolved_output = os.path.abspath(output_dir or os.path.dirname(storyboard_path))
        with _visual_invocation_lease(resolved_output):
            return func(storyboard_json, output_dir, *args, **kwargs)
    return wrapped


def _file_fingerprint(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _paid_ledger_path(state: VisualAgentState) -> str:
    return os.path.join(
        state["output_dir"], "stages", "visual_agent", "runs",
        state["run_id"], "paid_ledger.json",
    )


def _load_paid_ledger(state: VisualAgentState) -> dict:
    with _PAID_LEDGER_LOCK:
        value = read_json(_paid_ledger_path(state))
        if not isinstance(value, dict) or value.get("run_id") != state.get("run_id"):
            value = {"run_id": state.get("run_id", ""), "grants": {}, "jobs": {}}
        value.setdefault("grants", {})
        value.setdefault("jobs", {})
        return value


def _save_paid_ledger(state: VisualAgentState, ledger: dict) -> None:
    with _PAID_LEDGER_LOCK:
        ledger["updated_at"] = _now()
        atomic_write_json(_paid_ledger_path(state), ledger)
        state["paid_ledger"] = ledger
        state.setdefault("counters", {})["image_calls"] = len([
            item for item in ledger.get("jobs", {}).values()
            if item.get("status") in {
                "started", "uncertain", "produced", "publishing", "succeeded", "failed",
            }
        ])


def _revision_attempt_summary(ledger: dict) -> dict:
    revision_jobs = [
        item for item in ledger.get("jobs", {}).values()
        if str(item.get("operation_kind", "")) == "shot_revision"
        or str(item.get("logical_job_id", "")).startswith("retry:")
    ]
    logical_ids = {
        str(item.get("logical_job_id", "")) for item in revision_jobs
        if str(item.get("logical_job_id", ""))
    }
    artifact_ids = {
        str(item.get("logical_job_id", "")) for item in revision_jobs
        if item.get("status") == "succeeded" and not item.get("technical_invalid")
        and str(item.get("logical_job_id", ""))
    }
    logical_counts: dict[str, int] = {}
    logical_contracts: dict[str, set[str]] = {}
    for item in revision_jobs:
        logical_id = str(item.get("logical_job_id", ""))
        if not logical_id:
            continue
        logical_counts[logical_id] = logical_counts.get(logical_id, 0) + 1
        contract_id = str(item.get("correction_contract_id", ""))
        if contract_id:
            logical_contracts.setdefault(logical_id, set()).add(contract_id)
    lineage_conflicts = sorted(
        logical_id for logical_id, contract_ids in logical_contracts.items()
        if len(contract_ids) > 1
    )
    return {
        "logical_retries": len(logical_ids),
        "provider_attempts": len(revision_jobs),
        "artifacts_created": len(artifact_ids),
        "failed_provider_attempts": sum(
            1 for item in revision_jobs if item.get("status") == "failed"
        ),
        "duplicate_logical_attempts": sum(
            max(0, count - 1) for count in logical_counts.values()
        ),
        "lineage_conflict_count": len(lineage_conflicts),
        "lineage_conflict_logical_job_ids": lineage_conflicts[:20],
    }


def _candidate_identity(logical_job_id: str) -> tuple[str, str, int, int] | None:
    """Decode a stable foundation job ID without depending on story content."""
    if not logical_job_id.startswith("foundation:"):
        return None
    candidate_id = logical_job_id.removeprefix("foundation:")
    match = re.fullmatch(r"(.+)_r(\d+)_c(\d+)", candidate_id)
    if not match:
        return None
    return match.group(1), candidate_id, int(match.group(2)), int(match.group(3))


def _recovery_item_from_ledger_entry(state: VisualAgentState, entry: dict) -> dict:
    logical_id = str(entry.get("logical_job_id", ""))
    item = copy.deepcopy(entry.get("finalization_payload", {}))
    if not isinstance(item, dict):
        item = {}
    identity = _candidate_identity(logical_id)
    if identity:
        item.setdefault("asset_id", identity[0])
        item.setdefault("candidate_id", identity[1])
        item.setdefault("round", identity[2])
        item.setdefault("number", identity[3])
    relative_output = str(entry.get("output_path", ""))
    item.update({
        "job_id": logical_id,
        "logical_job_id": logical_id,
        "ledger_job_id": str(entry.get("ledger_job_id", "")),
        "logical_output_path": os.path.join(state["output_dir"], relative_output),
        "output_path": os.path.join(state["output_dir"], relative_output),
        "operation_kind": str(entry.get("operation_kind", "")),
        "group_id": item.get("group_id") or str(entry.get("group_id", "")),
        "shot_id": item.get("shot_id") or str(entry.get("shot_id", "")),
    })
    return item


def _is_actionable_paid_entry(entry: dict) -> bool:
    return str(entry.get("lifecycle_status", "")) != "superseded"


def _active_foundation_candidate_round(state: VisualAgentState, asset_id: str) -> int:
    reset = state.get("foundation_reset", {})
    if not isinstance(reset, dict):
        return 0
    reset_asset_ids = set(map(str, reset.get("asset_ids", [])))
    if str(asset_id) not in reset_asset_ids:
        return 0
    rounds = reset.get("round_by_asset", {})
    if not isinstance(rounds, dict):
        return 0
    return int(rounds.get(str(asset_id), 0) or 0)


def _reconcile_durable_progress(state: VisualAgentState) -> dict:
    """Merge per-call durable facts into a possibly stale graph snapshot."""
    ledger = _load_paid_ledger(state)
    recovered_candidates = 0
    recovered_jobs = 0
    uncertain_jobs: list[str] = []
    changed = False
    asset_ids = {
        str(item.get("asset_id")) for item in state.get("foundation_assets", [])
        if isinstance(item, dict) and item.get("asset_id")
    }
    # A provider result is durable before graph state is. Finish every complete
    # v2 attempt before exposing it to candidates or generated-shot state.
    for entry in list(ledger.get("jobs", {}).values()):
        if (
            not isinstance(entry, dict)
            or int(entry.get("artifact_binding_version", 0) or 0) < 2
            or entry.get("status") != "produced"
            or not _is_actionable_paid_entry(entry)
        ):
            continue
        attempt_relative = str(entry.get("attempt_output_path", ""))
        attempt_path = os.path.join(state["output_dir"], attempt_relative) if attempt_relative else ""
        expected_sha = str(entry.get("file_sha256", ""))
        if not (
            attempt_path and os.path.isfile(attempt_path) and os.path.getsize(attempt_path) > 0
            and (not expected_sha or _file_fingerprint(attempt_path) == expected_sha)
        ):
            continue
        item = _recovery_item_from_ledger_entry(state, entry)
        try:
            _finalize_paid_image_result(state, item, attempt_path)
            recovered_jobs += 1
        except Exception as exc:
            _mark_paid_output_invalid(state, attempt_path, str(exc))
    ledger = _load_paid_ledger(state)
    legacy_path_counts: dict[str, int] = {}
    legacy_logical_counts: dict[str, int] = {}
    for entry in ledger.get("jobs", {}).values():
        if not isinstance(entry, dict) or entry.get("artifact_binding_version"):
            continue
        relative = os.path.normcase(str(entry.get("output_path", "")))
        logical_id = str(entry.get("logical_job_id", ""))
        if relative:
            legacy_path_counts[relative] = legacy_path_counts.get(relative, 0) + 1
        if logical_id:
            legacy_logical_counts[logical_id] = legacy_logical_counts.get(logical_id, 0) + 1
    for entry in ledger.get("jobs", {}).values():
        if not isinstance(entry, dict):
            continue
        binding_version = int(entry.get("artifact_binding_version", 0) or 0)
        status = str(entry.get("status", ""))
        if binding_version >= 2 and status == "publishing":
            published_relative = str(entry.get("published_output_path", ""))
            published_path = (
                os.path.join(state["output_dir"], published_relative)
                if published_relative else ""
            )
            published_valid = bool(
                published_path and os.path.isfile(published_path)
                and os.path.getsize(published_path) > 0
            )
            expected_published_sha = str(entry.get("publish_expected_sha256", ""))
            if (
                published_valid and expected_published_sha
                and _file_fingerprint(published_path) == expected_published_sha
            ):
                entry["status"] = "succeeded"
                entry["recovered_from_file"] = True
                entry["published_file_sha256"] = expected_published_sha
                entry["published_at"] = entry.get("published_at") or _now()
                relative = published_relative
                path = published_path
                has_file = True
                recovered_jobs += 1
                changed = True
            else:
                relative = str(entry.get("attempt_output_path", ""))
                path = os.path.join(state["output_dir"], relative) if relative else ""
                has_file = bool(path and os.path.isfile(path) and os.path.getsize(path) > 0)
                expected_sha = str(entry.get("file_sha256", ""))
                if has_file and expected_sha and _file_fingerprint(path) != expected_sha:
                    has_file = False
                if has_file:
                    entry["status"] = "produced"
                    changed = True
                else:
                    uncertain_jobs.append(str(entry.get("logical_job_id", "")))
        elif binding_version >= 2:
            relative = str(
                entry.get("published_output_path", "")
                if status == "succeeded" else entry.get("attempt_output_path", "")
            )
            path = os.path.join(state["output_dir"], relative) if relative else ""
            has_file = bool(path and os.path.isfile(path) and os.path.getsize(path) > 0)
        else:
            relative = str(entry.get("output_path", ""))
            unambiguous = (
                legacy_path_counts.get(os.path.normcase(relative), 0) == 1
                and legacy_logical_counts.get(str(entry.get("logical_job_id", "")), 0) == 1
            )
            if entry.get("status") in {"started", "uncertain"} and not unambiguous:
                relative = ""
            path = os.path.join(state["output_dir"], relative) if relative else ""
            has_file = bool(path and os.path.isfile(path) and os.path.getsize(path) > 0)
        status = str(entry.get("status", status))
        expected_sha = str(
            entry.get("published_file_sha256", "")
            if status == "succeeded" else entry.get("file_sha256", "")
        )
        if status != "publishing" and has_file and expected_sha and _file_fingerprint(path) != expected_sha:
            has_file = False
        if has_file and status in {"started", "uncertain"}:
            entry["status"] = "succeeded" if binding_version < 2 else "produced"
            entry["recovered_from_file"] = True
            entry["file_sha256"] = _file_fingerprint(path)
            entry["completed_at"] = entry.get("completed_at") or _now()
            if binding_version < 2:
                entry["published_output_path"] = relative
                entry["published_file_sha256"] = entry["file_sha256"]
                entry["published_at"] = entry.get("published_at") or _now()
            recovered_jobs += 1
            changed = True
        elif has_file and status == "produced" and binding_version < 2:
            entry["status"] = "succeeded"
            entry["published_output_path"] = relative
            entry["published_file_sha256"] = _file_fingerprint(path)
            entry["published_at"] = entry.get("published_at") or _now()
            recovered_jobs += 1
            changed = True
        elif status == "started" and not has_file:
            entry["status"] = "uncertain"
            entry["uncertain_since"] = entry.get("uncertain_since") or _now()
            if _is_actionable_paid_entry(entry):
                uncertain_jobs.append(str(entry.get("logical_job_id", "")))
            changed = True
        elif status == "uncertain" and not has_file and _is_actionable_paid_entry(entry):
            uncertain_jobs.append(str(entry.get("logical_job_id", "")))

        identity = _candidate_identity(str(entry.get("logical_job_id", "")))
        if not has_file or identity is None or entry.get("status") != "succeeded":
            continue
        asset_id, candidate_id, round_number, number = identity
        if asset_id not in asset_ids:
            continue
        active_round = _active_foundation_candidate_round(state, asset_id)
        if active_round and round_number != active_round:
            continue
        candidates = state.setdefault("candidates", {}).setdefault(asset_id, [])
        metadata = read_json(path + ".manju.json") or {}
        foundation_metadata = metadata.get("foundation_image")
        if not isinstance(foundation_metadata, dict):
            foundation_metadata = _record_foundation_metadata(path, asset_id, candidate_id)
        recovered_candidate = {
            "candidate_id": candidate_id,
            "asset_id": asset_id,
            "round": round_number,
            "number": number,
            "path": relative,
            "prompt_fingerprint": str(entry.get("prompt_fingerprint", ""))[:16],
            "image_metadata": foundation_metadata,
            "ledger_job_id": str(entry.get("ledger_job_id", "")),
            "recovered_from_ledger": True,
        }
        existing_index = next(
            (index for index, item in enumerate(candidates)
             if item.get("candidate_id") == candidate_id),
            None,
        )
        if existing_index is None:
            candidates.append(recovered_candidate)
            recovered_candidates += 1
        elif candidates[existing_index] != recovered_candidate:
            candidates[existing_index] = recovered_candidate
            recovered_candidates += 1

    if changed:
        _save_paid_ledger(state, ledger)
    else:
        state["paid_ledger"] = ledger
        state.setdefault("counters", {})["image_calls"] = len([
            item for item in ledger.get("jobs", {}).values()
            if item.get("status") in {
                "started", "uncertain", "produced", "publishing", "succeeded", "failed",
            }
        ])
    post_reset_collision_repair = _repair_post_foundation_reset_revision_collision(
        state, ledger
    )
    _reconcile_revision_attempt_history(state, ledger)
    state["approval_grants"] = copy.deepcopy(ledger.get("grants", {}))
    state["reconciliation"] = {
        "recovered_jobs": recovered_jobs,
        "recovered_candidates": recovered_candidates,
        "uncertain_paid_jobs": sorted(set(filter(None, uncertain_jobs))),
        "actionable_uncertain_paid_jobs": sorted(set(filter(None, uncertain_jobs))),
        "superseded_uncertain_paid_jobs": sorted(set(filter(None, (
            str(item.get("logical_job_id", ""))
            for item in ledger.get("jobs", {}).values()
            if isinstance(item, dict)
            and item.get("status") in {"started", "uncertain"}
            and not _is_actionable_paid_entry(item)
        )))),
        "post_reset_revision_collision_repair": post_reset_collision_repair,
        "reconciled_at": _now(),
    }
    return state["reconciliation"]


def _register_approval_grant(state: VisualAgentState, pending: dict) -> str:
    grant_id = str(pending["request_id"])
    maximum = max(0, int(pending.get("maximum_paid_calls", 0)))
    with _PAID_LEDGER_LOCK:
        ledger = _load_paid_ledger(state)
        existing = ledger["grants"].get(grant_id)
        contract = {
            "grant_id": grant_id,
            "stage": pending.get("stage", ""),
            "state_fingerprint": pending.get("state_fingerprint", ""),
            "maximum_paid_calls": maximum,
        }
        if existing:
            if any(existing.get(key) != value for key, value in contract.items()):
                raise ValueError("approval grant contract changed after creation")
        else:
            ledger["grants"][grant_id] = {
                **contract, "used_calls": 0, "created_at": _now(),
            }
        _save_paid_ledger(state, ledger)
    state.setdefault("approval_grants", {})[grant_id] = ledger["grants"][grant_id]
    return grant_id


def _paid_attempt_output_path(logical_output_path: str, ledger_key: str) -> str:
    directory = os.path.dirname(logical_output_path)
    basename = os.path.basename(logical_output_path)
    stem, extension = os.path.splitext(basename)
    return os.path.join(directory, ".attempts", f"{stem}_{ledger_key}{extension or '.bin'}")


def _publish_paid_output(
    state: VisualAgentState, item: dict, produced_path: str,
) -> str:
    """Atomically publish one validated attempt and bind it to its ledger entry."""
    logical_path = os.path.abspath(str(item.get("logical_output_path") or item["output_path"]))
    produced_path = os.path.abspath(produced_path)
    os.makedirs(os.path.dirname(logical_path), exist_ok=True)
    ledger_key = str(item.get("ledger_job_id", ""))
    expected_sha = _file_fingerprint(produced_path)
    with _PAID_LEDGER_LOCK:
        ledger = _load_paid_ledger(state)
        entry = ledger.get("jobs", {}).get(ledger_key)
        if not isinstance(entry, dict):
            raise RuntimeError("paid ledger entry is missing during artifact publication")
        entry["status"] = "publishing"
        entry["published_output_path"] = os.path.relpath(logical_path, state["output_dir"])
        entry["publish_expected_sha256"] = expected_sha
        entry["publishing_at"] = entry.get("publishing_at") or _now()
        _save_paid_ledger(state, ledger)
    if produced_path != logical_path:
        os.replace(produced_path, logical_path)
        produced_sidecar = produced_path + ".manju.json"
        if os.path.isfile(produced_sidecar):
            os.replace(produced_sidecar, logical_path + ".manju.json")
    with _PAID_LEDGER_LOCK:
        ledger = _load_paid_ledger(state)
        entry = ledger.get("jobs", {}).get(ledger_key)
        if not isinstance(entry, dict):
            raise RuntimeError("paid ledger entry is missing during artifact publication")
        published_sha = _file_fingerprint(logical_path)
        if published_sha != expected_sha:
            raise RuntimeError("published artifact fingerprint changed during publication")
        entry["status"] = "succeeded"
        entry["published_output_path"] = os.path.relpath(logical_path, state["output_dir"])
        entry["published_file_sha256"] = published_sha
        entry["published_at"] = entry.get("published_at") or _now()
        logical_id = str(entry.get("logical_job_id", ""))
        for prior_key, prior in ledger.get("jobs", {}).items():
            if prior_key == ledger_key or str(prior.get("logical_job_id", "")) != logical_id:
                continue
            if prior.get("status") in {"started", "uncertain", "produced", "failed"}:
                prior["lifecycle_status"] = "superseded"
                prior["superseded_by"] = ledger_key
                prior["superseded_at"] = _now()
        _save_paid_ledger(state, ledger)
    return logical_path


def _paid_job_operation_kind(item: dict) -> str:
    kind = str(item.get("operation_kind", ""))
    logical_id = str(item.get("job_id") or item.get("logical_job_id") or "")
    if kind:
        return kind
    if logical_id.startswith("foundation:"):
        return "foundation_candidate"
    if logical_id.startswith("retry:"):
        return "shot_revision"
    if logical_id.startswith("group:"):
        return "shot_initial"
    return "generic"


def _paid_job_finalization_payload(job: dict) -> dict:
    """Persist only the JSON-safe facts needed to finalize after a crash."""
    keys = {
        "asset_id", "candidate_id", "round", "number", "shot_id", "group_id",
        "visible_character_ids", "visible_prop_ids", "reference_asset_ids",
        "previous_shot_reference_path", "previous_shot_reference_role",
        "primary_reference_role", "primary_reference_asset_ids", "reference_strategy",
        "correction_contract_id", "correction_contract", "revision_reference_board",
        "temporal_context_shot_ids", "temporal_context_paths",
        "provider_reference_mode", "provider_reference_paths",
        "excluded_image_reference_paths", "failed_shot_reference_included",
        "temporal_image_references_excluded", "constraint_isolation_task",
        "provider_escalation_task_id", "deferred_issue_ids",
    }
    return {key: copy.deepcopy(job[key]) for key in keys if key in job}


def _finalize_paid_image_result(
    state: VisualAgentState, item: dict, produced_path: str,
) -> str:
    """Validate, describe and publish one ledger-bound provider result."""
    kind = _paid_job_operation_kind(item)
    logical_id = str(item.get("job_id") or item.get("logical_job_id") or "")
    ledger_job_id = str(item.get("ledger_job_id", ""))
    if kind == "foundation_candidate":
        identity = _candidate_identity(logical_id)
        asset_id = str(item.get("asset_id", ""))
        candidate_id = str(item.get("candidate_id", ""))
        if identity:
            asset_id = asset_id or identity[0]
            candidate_id = candidate_id or identity[1]
        if not asset_id or not candidate_id:
            raise ValueError("foundation result is missing its stable candidate identity")
        _record_foundation_metadata(produced_path, asset_id, candidate_id)
    elif kind in {"shot_initial", "shot_revision"}:
        dimensions = _normalize_shot_canvas(
            produced_path,
            float(state["target_aspect_ratio"]),
            str(state.get("aspect_mode", "cover")),
        )
        _record_shot_dimensions(produced_path, dimensions, {
            "shot_id": item.get("shot_id", ""),
            "group_id": item.get("group_id", ""),
            "logical_job_id": logical_id,
            "ledger_job_id": ledger_job_id,
            "visible_character_ids": item.get("visible_character_ids", []),
            "visible_prop_ids": item.get("visible_prop_ids", []),
            "reference_asset_ids": item.get("reference_asset_ids", []),
            "previous_shot_reference_path": item.get("previous_shot_reference_path", ""),
            "previous_shot_reference_role": item.get("previous_shot_reference_role", ""),
            "primary_reference_role": item.get("primary_reference_role", ""),
            "primary_reference_asset_ids": item.get("primary_reference_asset_ids", []),
            "reference_strategy": item.get("reference_strategy", {}),
            "correction_contract_id": item.get("correction_contract_id", ""),
            "correction_contract": item.get("correction_contract", {}),
            "revision_reference_board": item.get("revision_reference_board", ""),
            "temporal_context_shot_ids": item.get("temporal_context_shot_ids", []),
            "temporal_context_paths": item.get("temporal_context_paths", []),
            "provider_reference_mode": item.get("provider_reference_mode", ""),
            "provider_reference_paths": item.get("provider_reference_paths", []),
            "excluded_image_reference_paths": item.get("excluded_image_reference_paths", []),
            "failed_shot_reference_included": item.get(
                "failed_shot_reference_included"
            ),
            "temporal_image_references_excluded": item.get(
                "temporal_image_references_excluded", False
            ),
            "constraint_isolation_task": item.get("constraint_isolation_task", {}),
            "provider_escalation_task_id": item.get("provider_escalation_task_id", ""),
            "deferred_issue_ids": item.get("deferred_issue_ids", []),
        })
    return _publish_paid_output(state, item, produced_path)


def _run_paid_image_jobs(
    state: VisualAgentState,
    image_provider: ImageProvider,
    jobs: list[dict],
    parallelism: int,
    grant_id: str,
) -> list[dict]:
    """Execute paid jobs with a durable pre-call debit and per-job commit.

    A successful file from an interrupted process is adopted on resume. A
    started/failed attempt without a valid file is never silently retried under
    the same approval grant.
    """
    if not jobs:
        return []

    def execute(job: dict) -> dict:
        logical_id = str(job["job_id"])
        ledger_key = content_fingerprint(state["run_id"], logical_id, grant_id, length=32)
        logical_output_path = os.path.abspath(job["output_path"])
        attempt_output_path = _paid_attempt_output_path(logical_output_path, ledger_key)
        with _PAID_LEDGER_LOCK:
            ledger = _load_paid_ledger(state)
            # Only an artifact uniquely bound to its ledger attempt is recoverable.
            for prior in ledger["jobs"].values():
                if prior.get("logical_job_id") != logical_id:
                    continue
                if prior.get("technical_invalid"):
                    continue
                binding_version = int(prior.get("artifact_binding_version", 0) or 0)
                prior_status = str(prior.get("status", ""))
                if prior_status == "succeeded":
                    relative = str(
                        prior.get("published_output_path")
                        or (prior.get("output_path") if binding_version < 2 else "")
                    )
                elif prior_status == "publishing" and binding_version >= 2:
                    published_relative = str(prior.get("published_output_path", ""))
                    published_path = (
                        os.path.join(state["output_dir"], published_relative)
                        if published_relative else ""
                    )
                    publish_sha = str(prior.get("publish_expected_sha256", ""))
                    if (
                        published_path and os.path.isfile(published_path)
                        and publish_sha and _file_fingerprint(published_path) == publish_sha
                    ):
                        prior["status"] = "succeeded"
                        prior["published_file_sha256"] = publish_sha
                        prior["published_at"] = prior.get("published_at") or _now()
                        prior_status = "succeeded"
                        relative = published_relative
                    else:
                        relative = str(prior.get("attempt_output_path", ""))
                elif prior_status in {"started", "uncertain", "produced"} and binding_version >= 2:
                    relative = str(prior.get("attempt_output_path", ""))
                else:
                    relative = ""
                prior_path = os.path.join(state["output_dir"], relative) if relative else ""
                valid_prior = bool(
                    prior_path and os.path.isfile(prior_path) and os.path.getsize(prior_path) > 0
                )
                expected_sha = str(
                    prior.get("published_file_sha256") if prior_status == "succeeded"
                    else prior.get("file_sha256", "")
                )
                if valid_prior and expected_sha and _file_fingerprint(prior_path) != expected_sha:
                    valid_prior = False
                if valid_prior:
                    if prior_status in {"started", "uncertain", "publishing"}:
                        prior["status"] = "produced"
                    prior["recovered_from_file"] = True
                    prior["file_sha256"] = _file_fingerprint(prior_path)
                    prior["completed_at"] = prior.get("completed_at") or _now()
                    _save_paid_ledger(state, ledger)
                    return {
                        **job, "result": prior_path, "error": "", "recovered": True,
                        "ledger_job_id": prior.get("ledger_job_id", ""),
                        "logical_output_path": logical_output_path,
                        "already_published": prior_status == "succeeded",
                        "provider_attempted": False,
                    }
            existing = ledger["jobs"].get(ledger_key)
            if existing:
                existing_status = str(existing.get("status", ""))
                return {
                    **job, "result": None,
                    "error": str(existing.get("error", "")) or (
                        "paid attempt already failed or was interrupted; new approval required"
                    ),
                    "ledger_job_id": str(existing.get("ledger_job_id", ledger_key)),
                    "provider_attempted": existing_status == "failed",
                    "recovered_attempt_state": existing_status,
                }
            grant = ledger["grants"].get(grant_id)
            if not grant:
                return {**job, "result": None, "error": "approval grant is missing",
                        "provider_attempted": False}
            if int(grant.get("used_calls", 0)) >= int(grant.get("maximum_paid_calls", 0)):
                return {
                    **job, "result": None,
                    "error": "local approval grant exhausted; new approval required",
                    "provider_attempted": False,
                }
            for prior_key, prior in ledger.get("jobs", {}).items():
                if (
                    prior_key != ledger_key
                    and str(prior.get("logical_job_id", "")) == logical_id
                    and prior.get("status") == "failed"
                    and _is_actionable_paid_entry(prior)
                ):
                    prior["lifecycle_status"] = "superseded"
                    prior["superseded_by"] = ledger_key
                    prior["superseded_at"] = _now()
            grant["used_calls"] = int(grant.get("used_calls", 0)) + 1
            ledger["jobs"][ledger_key] = {
                "ledger_job_id": ledger_key,
                "logical_job_id": logical_id,
                "grant_id": grant_id,
                "status": "started",
                "started_at": _now(),
                "output_path": os.path.relpath(logical_output_path, state["output_dir"]),
                "attempt_output_path": os.path.relpath(attempt_output_path, state["output_dir"]),
                "published_output_path": "",
                "artifact_binding_version": 2,
                "prompt_fingerprint": content_fingerprint(job.get("prompt", ""), length=24),
                "reference_fingerprints": [
                    _file_fingerprint(path) for path in job.get("references", [])
                    if os.path.isfile(path)
                ],
                "size": job.get("size", ""),
                "operation_kind": str(job.get("operation_kind", "")),
                "group_id": str(job.get("group_id", "")),
                "shot_id": str(job.get("shot_id", "")),
                "revision_attempt_number": int(job.get("revision_attempt_number", 0) or 0),
                "revision_strategy": str(job.get("revision_strategy", "")),
                "correction_contract_id": str(job.get("correction_contract_id", "")),
                "finalization_payload": _paid_job_finalization_payload(job),
            }
            _save_paid_ledger(state, ledger)
        os.makedirs(os.path.dirname(attempt_output_path), exist_ok=True)
        try:
            result = image_provider(
                job["prompt"], attempt_output_path, job["references"], job["size"]
            )
            if result and os.path.isfile(result) and os.path.abspath(result) != attempt_output_path:
                shutil.copy2(result, attempt_output_path)
            valid = bool(os.path.isfile(attempt_output_path) and os.path.getsize(attempt_output_path) > 0)
            result = attempt_output_path if valid else None
            error = "" if valid else "provider returned no valid image"
        except Exception as exc:
            # Some adapters raise after the response body was already saved.
            # A complete file is the durable success signal.
            valid = os.path.isfile(attempt_output_path) and os.path.getsize(attempt_output_path) > 0
            result = attempt_output_path if valid else None
            error = "" if valid else f"{type(exc).__name__}: {exc}"[:500]
        with _PAID_LEDGER_LOCK:
            ledger = _load_paid_ledger(state)
            entry = ledger["jobs"][ledger_key]
            entry["status"] = "produced" if valid else "failed"
            entry["completed_at"] = _now()
            entry["error"] = error
            if valid:
                entry["file_sha256"] = _file_fingerprint(str(result))
            _save_paid_ledger(state, ledger)
        return {
            **job, "result": result if valid else None, "error": error,
            "recovered": False, "ledger_job_id": ledger_key,
            "logical_output_path": logical_output_path,
            "already_published": False,
            "provider_attempted": True,
        }

    def execute_and_finalize(job: dict) -> dict:
        item = execute(job)
        result = item.get("result")
        if not result or item.get("already_published"):
            return item
        try:
            item["result"] = _finalize_paid_image_result(state, item, str(result))
            item["already_published"] = True
        except Exception as exc:
            _mark_paid_output_invalid(state, str(result), str(exc))
            item["result"] = None
            item["error"] = f"invalid image output: {exc}"[:500]
        return item

    if parallelism <= 1 or len(jobs) == 1:
        return [execute_and_finalize(job) for job in jobs]
    completed: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(parallelism, len(jobs))) as executor:
        futures = {
            executor.submit(execute_and_finalize, job): index for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            completed[futures[future]] = future.result()
    return [completed[index] for index in range(len(jobs))]


def _mark_paid_output_invalid(state: VisualAgentState, path: str, error: str) -> None:
    """Prevent a corrupt or dimension-invalid paid file from being adopted on resume."""
    relative = os.path.normcase(os.path.relpath(path, state["output_dir"]))
    with _PAID_LEDGER_LOCK:
        ledger = _load_paid_ledger(state)
        for entry in ledger.get("jobs", {}).values():
            bound_paths = {
                os.path.normcase(str(entry.get("output_path", ""))),
                os.path.normcase(str(entry.get("attempt_output_path", ""))),
                os.path.normcase(str(entry.get("published_output_path", ""))),
            }
            if relative not in bound_paths:
                continue
            entry["technical_invalid"] = True
            entry["technical_error"] = str(error)[:500]
            entry["technical_invalid_at"] = _now()
        _save_paid_ledger(state, ledger)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _attach_v4_run_identity(
    state: VisualAgentState,
    invocation_contract: dict,
    *,
    run_kind: str = "production",
    parent_run_id: str = "",
) -> None:
    """Attach one immutable identity and keep compatibility as a separate fact."""
    run_id = str(state.get("run_id", ""))
    existing = identity_from_dict(state.get("run_identity"))
    if existing and existing.run_id == run_id:
        report = compatibility_report(existing, invocation_contract)
        if not report["compatible"]:
            raise ValueError(
                "current invocation is incompatible with immutable run identity "
                f"{run_id}; create a new run"
            )
        state["invocation_compatibility"] = report
        stored_contract = state.get("run_invocation_contract")
        if isinstance(stored_contract, dict) and stored_contract:
            if not compatibility_report(existing, stored_contract)["compatible"]:
                raise ValueError("stored invocation contract does not match immutable run identity")
        else:
            state["run_invocation_contract"] = copy.deepcopy(invocation_contract)
        return
    identity = create_run_identity(
        invocation_contract,
        run_id=run_id,
        run_kind=run_kind,
        parent_run_id=parent_run_id,
    )
    state["run_identity"] = identity.to_dict()
    state["run_invocation_contract"] = copy.deepcopy(invocation_contract)
    state["invocation_compatibility"] = compatibility_report(identity, invocation_contract)


def _read_storyboard(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read storyboard: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("storyboard must be a JSON object")
    errors = validate_storyboard(value)
    if errors:
        raise ValueError("invalid storyboard: " + "; ".join(errors[:8]))
    return value


def _upstream_storyboard_gate(storyboard: dict) -> dict:
    metadata = storyboard.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    status = str(metadata.get("agent_status", "")).strip().lower()
    verification = str(metadata.get("agent_verification_state", "")).strip().lower()
    agent_declared = bool(
        str(metadata.get("generation_engine", "")).strip().lower() == "agent"
        or "agent_status" in metadata
        or "agent_verification_state" in metadata
    )
    verified_states = {"verified", "complete", "completed"}
    blocked = bool(agent_declared and (
        status != "completed" or verification not in verified_states
    ))
    return {
        "passed": not blocked,
        "agent_provenance_declared": agent_declared,
        "agent_status": status or "not_declared",
        "verification_state": verification or "not_declared",
        "reason": "upstream_storyboard_needs_review" if blocked else "passed",
    }


def _plain_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _strip_generated_anchor_suffixes(value: str) -> str:
    text = str(value or "")
    positions = [
        index for marker in ("角色固定锚点", "fixed character anchor")
        if (index := text.casefold().find(marker.casefold())) >= 0
    ]
    return text[:min(positions)].strip(" ;；") if positions else text


def _name_only_offscreen(text: str, name: str) -> bool:
    """Recognize explicit out-of-frame mentions without guessing visibility."""
    source = _plain_text(text)
    needle = _plain_text(name).strip()
    if not source or not needle or needle not in source:
        return False
    starts = [match.start() for match in re.finditer(re.escape(needle), source)]
    markers = (
        "off-screen", "off screen", "off-camera", "off camera", "outside the frame",
        "out of frame", "画外", "镜外", "框外", "不入镜", "未入镜",
    )
    for start in starts:
        window = source[max(0, start - 28):start + len(needle) + 28]
        if not any(marker in window for marker in markers):
            return False
    return True


def _storyboard_asset_preflight(storyboard: dict) -> list[dict]:
    """Code-owned checks that must pass before any paid image generation."""
    characters = {
        str(item.get("character_id", "")): item
        for item in get_characters(storyboard) if str(item.get("character_id", ""))
    }
    # Scene-level key_props is the canonical registry produced from the Source
    # Model. Shot declarations must not be allowed to authorize their own IDs.
    canonical_props: set[str] = set()
    legacy_shot_props: set[str] = set()
    for scene in storyboard.get("scenes", []):
        for prop in scene.get("key_props", []) if isinstance(scene.get("key_props"), list) else []:
            if isinstance(prop, dict) and prop.get("prop_id"):
                canonical_props.add(str(prop["prop_id"]))
        for shot in scene.get("shots", []):
            visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
            for prop in visual.get("key_props", []) if isinstance(visual.get("key_props"), list) else []:
                if isinstance(prop, dict) and prop.get("prop_id"):
                    legacy_shot_props.add(str(prop["prop_id"]))
    # Backward compatibility for legacy storyboards that predate a scene
    # registry. Once a canonical scene registry exists it is authoritative.
    known_props = canonical_props or legacy_shot_props

    issues: list[dict] = []
    for scene_index, scene in enumerate(storyboard.get("scenes", [])):
        for shot_index, shot in enumerate(scene.get("shots", [])):
            if not isinstance(shot, dict):
                continue
            visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
            shot_id = str(shot.get("shot_id", ""))
            path = f"$.scenes[{scene_index}].shots[{shot_index}]"
            visible_characters = [
                str(value) for value in visual.get("visible_character_ids", []) if str(value)
            ] if isinstance(visual.get("visible_character_ids"), list) else []
            unknown_characters = [value for value in visible_characters if value not in characters]
            if unknown_characters:
                issues.append({
                    "issue_id": "preflight_" + content_fingerprint(path, unknown_characters, length=16),
                    "category": "visible_entity_consistency", "severity": "critical", "blocking": True,
                    "shot_id": shot_id, "storyboard_path": path + ".visual.visible_character_ids",
                    "problem": "visible_character_ids contains unknown character IDs",
                    "instruction": "Use only creative_bible character IDs.",
                    "invalid_ids": unknown_characters, "evidence_valid": True,
                })
            prompts = shot.get("prompts", {}) if isinstance(shot.get("prompts"), dict) else {}
            authored_prompt = " ".join((
                _strip_generated_anchor_suffixes(str(prompts.get("image_cn", ""))),
                _strip_generated_anchor_suffixes(str(prompts.get("image_en", ""))),
                str(visual.get("description", "")), str(visual.get("composition", "")),
            ))
            for character_id in visible_characters:
                character = characters.get(character_id, {})
                names = [str(character.get(key, "")).strip() for key in ("name", "name_en")]
                names = [name for name in names if name and _plain_text(name) in _plain_text(authored_prompt)]
                if names and all(_name_only_offscreen(authored_prompt, name) for name in names):
                    issues.append({
                        "issue_id": "preflight_" + content_fingerprint(path, character_id, "offscreen", length=16),
                        "category": "visible_entity_consistency", "severity": "high", "blocking": True,
                        "shot_id": shot_id, "storyboard_path": path + ".visual.visible_character_ids",
                        "problem": f"{character_id} is declared visible but is only described as off-screen",
                        "instruction": "Remove the off-screen character ID and its image identity anchor.",
                        "character_id": character_id, "evidence_valid": True,
                    })
            explicit_prop_ids = [
                str(value) for value in shot.get("visible_prop_ids", []) if str(value)
            ] if isinstance(shot.get("visible_prop_ids"), list) else []
            key_prop_ids = [
                str(item.get("prop_id")) for item in visual.get("key_props", [])
                if isinstance(item, dict) and item.get("prop_id")
            ] if isinstance(visual.get("key_props"), list) else []
            if set(explicit_prop_ids) != set(key_prop_ids):
                issues.append({
                    "issue_id": "preflight_" + content_fingerprint(path, explicit_prop_ids, key_prop_ids, length=16),
                    "category": "asset_binding", "severity": "high", "blocking": True,
                    "shot_id": shot_id, "storyboard_path": path,
                    "problem": "visible_prop_ids and visual.key_props disagree",
                    "instruction": "Make the visible prop ID list and bound key-prop objects describe the same assets.",
                    "visible_prop_ids": explicit_prop_ids, "key_prop_ids": key_prop_ids,
                    "evidence_valid": True,
                })
            unknown_props = [value for value in explicit_prop_ids if value not in known_props]
            if unknown_props:
                issues.append({
                    "issue_id": "preflight_" + content_fingerprint(path, unknown_props, length=16),
                    "category": "asset_binding", "severity": "critical", "blocking": True,
                    "shot_id": shot_id, "storyboard_path": path + ".visible_prop_ids",
                    "problem": "visible_prop_ids contains unknown prop IDs",
                    "instruction": "Bind only source-model props present in scene or shot key_props.",
                    "invalid_ids": unknown_props, "evidence_valid": True,
                })
    return issues


def _storyboard_run_payload(storyboard: dict) -> dict:
    """Remove volatile generation metadata and media attachments from a run fingerprint."""
    value = copy.deepcopy(storyboard)
    value.pop("metadata", None)
    for scene in value.get("scenes", []):
        for shot in scene.get("shots", []):
            assets = shot.get("assets")
            if isinstance(assets, dict):
                assets.pop("image", None)
            status = shot.get("status")
            if isinstance(status, dict):
                status.pop("image", None)
    return value


def _requested_budget_value(value: Any) -> int | str:
    if value is None or value == "auto":
        return "auto"
    return int(value)


def _resume_invocation_contract(
    storyboard: dict,
    models: dict,
    capabilities: dict,
    foundation_candidates: int,
    max_auto_retries: int,
    max_steps: int | str | None,
    max_calls: int | str | None,
    size: str,
    target_aspect_ratio: float,
    aspect_mode: str,
    image_parallelism: int,
    confidence_calibration: dict | None = None,
) -> dict:
    """Fingerprint every run input except the explicitly migrated toolset version."""
    payload = {
        "schema_version": 1,
        "storyboard_fingerprint": content_fingerprint(
            _storyboard_run_payload(storyboard), length=32
        ),
        "models": copy.deepcopy(models),
        "provider_capabilities": copy.deepcopy(capabilities),
        "foundation_candidates": int(foundation_candidates),
        "max_auto_retries": int(max_auto_retries),
        "requested_max_steps": _requested_budget_value(max_steps),
        "requested_max_calls": _requested_budget_value(max_calls),
        "size": str(size),
        "target_aspect_ratio": float(target_aspect_ratio),
        "aspect_mode": str(aspect_mode),
        "image_parallelism": int(image_parallelism),
        "playbook_version": PLAYBOOK_VERSION,
        "visual_agent_version": VISUAL_AGENT_VERSION,
    }
    if isinstance(confidence_calibration, dict) and confidence_calibration:
        payload["vision_confidence_calibration"] = calibration_summary(
            confidence_calibration
        )
    return {**payload, "fingerprint": content_fingerprint(payload, length=32)}


def _resume_contract_from_state(state: VisualAgentState, manifest: dict) -> dict:
    budgets = state.get("budgets", {})
    storyboard = state.get("storyboard", {})
    models = manifest.get("models", {}) if isinstance(manifest, dict) else {}
    return _resume_invocation_contract(
        storyboard if isinstance(storyboard, dict) else {},
        models if isinstance(models, dict) else {},
        state.get("provider_capabilities", {}),
        int(budgets.get("foundation_candidates", 0) or 0),
        int(budgets.get("max_auto_retries", 0) or 0),
        budgets.get("requested_max_steps", "auto"),
        budgets.get("requested_max_calls", "auto"),
        str(state.get("size", budgets.get("effective_request_size", ""))),
        float(state.get("target_aspect_ratio", budgets.get("target_aspect_ratio", 0)) or 0),
        str(state.get("aspect_mode", budgets.get("aspect_mode", "cover"))),
        int(budgets.get("image_parallelism", 1) or 1),
        state.get("vision_confidence_calibration", {}),
    )


def _vision_config() -> dict:
    env = load_manju_env()
    try:
        max_attempts = int(env.get("MANJU_VISION_MAX_ATTEMPTS", "3"))
    except (TypeError, ValueError):
        max_attempts = 3
    return {
        "api_base": str(env.get("MANJU_VISION_API_BASE", "")),
        "api_key": str(env.get("MANJU_VISION_API_KEY", "")),
        "model": str(env.get("MANJU_VISION_MODEL", "")),
        "max_attempts": max(1, min(3, max_attempts)),
    }


def _model_names() -> dict:
    env = load_manju_env()
    return {
        "llm_model": str(env.get("LLM_MODEL", "")),
        "image_model": str(env.get("MANJU_IMAGE_MODEL", "")),
        "vision_model": str(env.get("MANJU_VISION_MODEL", "")),
    }


def _default_image_provider(prompt: str, output_path: str,
                            references: list[str], size: str) -> str | None:
    return generate_image_with_references(prompt, output_path, references, size)


def _extract_vision_text(result: dict) -> str:
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(result.get("output_text", ""))


def _vision_error_retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 429} or 500 <= exc.code <= 599
    if isinstance(exc, (ssl.SSLError, socket.timeout, TimeoutError,
                        ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return isinstance(reason, (ssl.SSLError, socket.timeout, TimeoutError, OSError))
    return isinstance(exc, (OSError, json.JSONDecodeError))


def _vision_failure(exc: BaseException, attempt: int, retryable: bool,
                    api_key: str = "") -> dict:
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return {
        "attempt": attempt,
        "error_type": type(exc).__name__,
        "retryable": retryable,
        "message": message[:300],
    }


def _default_vision_provider(task: str, image_paths: list[str], context: dict) -> dict | None:
    cfg = _vision_config()
    if not cfg["api_base"] or not cfg["api_key"] or not cfg["model"]:
        return None
    content: list[dict] = [{
        "type": "text",
        "text": (
            "Return JSON only. Inspect the supplied images using the evidence and criteria. "
            "For ranking return {ranking:[candidate_id...],summary:string}. For review return "
            "{issues:[{issue_id,shot_id,category,severity,blocking,problem,instruction,"
            "storyboard_path,reference_asset_ids,focus_asset_ids,correction_target,"
            "correction_contract_id,constraint_id,image_path,evidence:[{image_path,problem}],"
            "confidence,measurement}]}. A blocking failure requires visible evidence and confidence >= 0.75; "
            "use blocking:false when a constraint is unverifiable. "
            "correction_target must be one of prop_geometry, location_structure, character_identity, "
            "shot_composition, temporal_state, effect_alignment, artifact, or other. Use effect_alignment "
            "for rays, beams, reflections, gaze lines, emitters, or another visible effect whose source and "
            "target direction must align. focus_asset_ids must contain only "
            "the locked assets directly authoritative for the correction, while reference_asset_ids "
            "contains the evidence assets. Reuse correction_contract_id only when the same unresolved "
            "constraint appears in context.open_correction_contracts; leave it empty for a new constraint. "
            "Do not invent IDs. For scene review, a generated shot must be one continuous camera frame; "
            "triptychs, collages, contact sheets, stacked panels, split screens, insets or multiple time-state "
            "panels are blocking artifact findings. Evaluate source-declared prop scale against visible scene "
            "comparators and the attached scale contract; the frame-filling size of a canonical asset image is "
            "not scale evidence.\n"
            + json.dumps({"task": task, "context": context}, ensure_ascii=False)
        ),
    }]
    for path in image_paths:
        if os.path.isfile(path):
            content.append({"type": "image_url", "image_url": {"url": file_data_url(path)}})
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 4000,
    }).encode("utf-8")
    request = urllib.request.Request(
        join_api_url(cfg["api_base"], "chat/completions"), data=payload,
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
    )
    failures: list[dict] = []
    max_attempts = int(cfg.get("max_attempts", 3))
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
            parsed = parse_json_response(_extract_vision_text(result))
            if not isinstance(parsed, dict):
                raise json.JSONDecodeError("vision response is not a JSON object", "", 0)
            return {
                **parsed,
                "_manju_vision_meta": {
                    "task": task, "attempts": attempt,
                    "successes": 1, "failures": failures,
                },
            }
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError) as exc:
            retryable = _vision_error_retryable(exc)
            failures.append(_vision_failure(exc, attempt, retryable, cfg["api_key"]))
            if not retryable or attempt >= max_attempts:
                print(f"   ⚠ 视觉复核不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
                return {
                    "_manju_vision_unavailable": True,
                    "_manju_vision_meta": {
                        "task": task, "attempts": attempt,
                        "successes": 0, "failures": failures,
                    },
                }
            time.sleep(0.25 * attempt)
    return None


def _record_vision_result(state: VisualAgentState, task: str,
                          result: dict | None) -> dict | None:
    payload = dict(result) if isinstance(result, dict) else {}
    unavailable = result is None or payload.pop("_manju_vision_unavailable", False) is True
    meta = payload.pop("_manju_vision_meta", None)
    if not isinstance(meta, dict):
        meta = {
            "task": task, "attempts": 1,
            "successes": 0 if unavailable else 1,
            "failures": ([{
                "attempt": 1, "error_type": "provider_unavailable",
                "retryable": False, "message": "vision provider returned no usable result",
            }] if unavailable else []),
        }
    attempts = max(1, int(meta.get("attempts", 1) or 1))
    failures = [item for item in meta.get("failures", []) if isinstance(item, dict)]
    state.setdefault("counters", {})["vision_attempts"] = (
        int(state.get("counters", {}).get("vision_attempts", 0)) + attempts
    )
    state["counters"]["vision_failures"] = (
        int(state.get("counters", {}).get("vision_failures", 0)) + len(failures)
    )
    if not unavailable:
        state["counters"]["vision_calls"] = int(
            state.get("counters", {}).get("vision_calls", 0)
        ) + 1
    for failure in failures:
        state.setdefault("vision_failure_history", []).append({
            "task": task, "at": _now(), **failure,
        })
    return None if unavailable else payload


def _private_trace_path(state: VisualAgentState) -> str:
    return os.path.join(
        state["output_dir"], "stages", "visual_agent", "runs",
        state.get("run_id", "unknown"), "trace.jsonl",
    )


def _sync_public_trace(state: VisualAgentState) -> None:
    private = _private_trace_path(state)
    public = os.path.join(state["output_dir"], "visual_agent_trace.jsonl")
    os.makedirs(os.path.dirname(public), exist_ok=True)
    temporary = public + ".tmp"
    with open(temporary, "wb") as target:
        if os.path.isfile(private):
            with open(private, "rb") as source:
                shutil.copyfileobj(source, target)
    delay = 0.005
    for attempt in range(8):
        try:
            os.replace(temporary, public)
            break
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}
            if not retryable or attempt == 7:
                raise
            time.sleep(delay)
            delay = min(0.05, delay * 2)


def _activate_trace(state: VisualAgentState, *, reset_private: bool = False) -> None:
    private = _private_trace_path(state)
    os.makedirs(os.path.dirname(private), exist_ok=True)
    if reset_private:
        with open(private, "w", encoding="utf-8"):
            pass
    _sync_public_trace(state)


def _trace(state: VisualAgentState, event: str, payload: dict | None = None) -> None:
    private = _private_trace_path(state)
    maximum = 0
    if os.path.isfile(private):
        with open(private, encoding="utf-8") as handle:
            for line in handle:
                try:
                    maximum = max(maximum, int(json.loads(line).get("seq", 0)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
    state["trace_seq"] = maximum + 1
    timestamp = _now()
    safe_payload = _safe_trace_payload(payload or {})
    record = {
        "seq": state["trace_seq"], "at": timestamp, "event": event,
        "event_id": content_fingerprint(
            state.get("run_id", ""), state["trace_seq"], event, timestamp, safe_payload, length=32,
        ),
        "run_id": state.get("run_id", ""), "stage": state.get("stage", ""),
        "payload": safe_payload,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(private), exist_ok=True)
    with open(private, "a", encoding="utf-8") as handle:
        handle.write(line)
    _sync_public_trace(state)


def _safe_trace_payload(value: Any, key_name: str = "") -> Any:
    """Remove secrets and implicit-reasoning fields from persisted diagnostics."""
    lowered = key_name.lower()
    if any(token in lowered for token in (
        "api_key", "token", "secret", "password", "authorization",
        "chain_of_thought", "reasoning", "thought", "analysis",
    )):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(key): _safe_trace_payload(item, str(key))
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [_safe_trace_payload(item) for item in value[:100]]
    if isinstance(value, str):
        clean = value[:12000]
        env = load_manju_env()
        for secret_key in ("LLM_API_KEY", "MANJU_IMAGE_API_KEY", "MANJU_VISION_API_KEY"):
            secret = str(env.get(secret_key, ""))
            if secret:
                clean = clean.replace(secret, "[REDACTED]")
        return clean
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _state_progress(state: VisualAgentState) -> str:
    return content_fingerprint(
        state.get("stage"), state.get("foundation_phase_index"),
        sorted(state.get("locked_assets", {})), state.get("current_group_index"),
        {key: value.get("status") for key, value in state.get("group_states", {}).items()},
        state.get("status"), length=24,
    )


def _approval_fingerprint(state: VisualAgentState, stage: str, item_ids: list[str]) -> str:
    return content_fingerprint(
        state["run_id"], stage, item_ids,
        sorted(state.get("locked_assets", {})),
        state.get("foundation_phase_index", 0),
        state.get("current_group_index", 0), length=32,
    )


_PLACEHOLDER_REVIEW_TEXT = {
    "", "auto", "automatic", "automated", "ok", "okay", "yes", "approved", "pass",
    "scriptselected", "scriptapproved", "\u901a\u8fc7", "\u540c\u610f", "\u81ea\u52a8\u5ba1\u6279",
    "\u81ea\u52a8\u9009\u62e9", "\u811a\u672c\u9009\u62e9", "\u811a\u672c\u5ba1\u6279",
}
_PLACEHOLDER_REVIEW_PREFIXES = {
    "auto", "automatic", "automated", "scriptselected", "scriptapproved",
    "\u81ea\u52a8\u5ba1\u6279", "\u81ea\u52a8\u9009\u62e9", "\u811a\u672c\u9009\u62e9", "\u811a\u672c\u5ba1\u6279",
    "\u5df2\u5ba1\u9605\u6240\u6709\u56fe\u7247", "reviewedallimages",
}


def _is_placeholder_review_text(value: object) -> bool:
    return _core_is_placeholder_review_text(value)


def _approval_image_fingerprints(state: VisualAgentState, stage: str,
                                 extra: dict | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if stage.startswith("foundation_lock_"):
        summary = (extra or {}).get("candidate_summary", {})
        for details in summary.values() if isinstance(summary, dict) else []:
            for candidate in details.get("candidates", []) if isinstance(details, dict) else []:
                path = os.path.join(state["output_dir"], candidate.get("path", ""))
                if candidate.get("candidate_id") and os.path.isfile(path):
                    values[str(candidate["candidate_id"])] = _file_fingerprint(path)
    elif stage.startswith("manual_review_"):
        group_id = stage.removeprefix("manual_review_")
        generated = state.get("group_states", {}).get(group_id, {}).get("generated", {})
        for shot_id, relative in generated.items():
            path = os.path.join(state["output_dir"], relative)
            if os.path.isfile(path):
                values[str(shot_id)] = _file_fingerprint(path)
    return values


def _validate_human_decision(pending: dict, decision: dict, choice: str) -> None:
    validate_common_decision(pending, decision, choice)
    stage = str(pending.get("stage", ""))
    if choice == "approve" and stage.startswith("manual_review_"):
        blocking_issue_ids = {
            str(issue.get("issue_id", "")).strip()
            for issue in pending.get("issues", [])
            if isinstance(issue, dict) and issue.get("blocking") is True
            and str(issue.get("issue_id", "")).strip()
        }
        if blocking_issue_ids:
            supplied_reasons = decision.get("issue_override_reasons")
            if not isinstance(supplied_reasons, dict) or set(map(str, supplied_reasons)) != blocking_issue_ids:
                raise ValueError(
                    "manual semantic override requires one issue_override_reasons entry "
                    "for every blocking issue"
                )
            for issue_id in sorted(blocking_issue_ids):
                reason = str(supplied_reasons.get(issue_id, "")).strip()
                if _is_placeholder_review_text(reason) or len(reason) < 12:
                    raise ValueError(
                        f"manual semantic override requires a specific reason for blocking issue {issue_id}"
                    )
    if choice == "approve" and stage.startswith("foundation_lock_"):
        contracts = pending.get("reference_contracts", {})
        selections = decision.get("selections", {})
        checks = decision.get("reference_contract_checks", {})
        if not isinstance(contracts, dict):
            contracts = {}
        for asset_id, contract in contracts.items():
            if not isinstance(contract, dict) or contract.get("role") != "canonical_geometry_anchor":
                continue
            check = checks.get(asset_id) if isinstance(checks, dict) else None
            selected = str(selections.get(asset_id, "")) if isinstance(selections, dict) else ""
            required_true = (
                "single_object", "single_view", "clean_background",
                "no_grid_or_state_sequence",
            )
            if not isinstance(check, dict) or str(check.get("candidate_id", "")) != selected:
                raise ValueError(
                    f"foundation lock for {asset_id} requires a contract check for the selected candidate"
                )
            if any(check.get(field) is not True for field in required_true):
                raise ValueError(
                    f"foundation lock for {asset_id} requires all canonical geometry checks to be true"
                )
            scale_contract = contract.get("scale_contract", {})
            if isinstance(scale_contract, dict) and scale_contract.get("required") is True:
                scale_checks = (
                    "scale_evidence_present", "scale_relation_matches",
                    "scale_comparator_complete", "scale_comparator_in_focus",
                    "scale_comparator_contact_or_shared_plane",
                )
                if any(check.get(field) is not True for field in scale_checks):
                    raise ValueError(
                        f"foundation lock for {asset_id} requires complete, in-focus source scale evidence "
                        "with direct contact or a shared support plane"
                    )


def _write_approval_request(state: VisualAgentState, stage: str, item_ids: list[str],
                            maximum_paid_calls: int, extra: dict | None = None) -> None:
    ledger = _load_paid_ledger(state)
    ledger_state = [
        (key, item.get("status"), item.get("grant_id"))
        for key, item in sorted(ledger.get("jobs", {}).items())
    ]
    fingerprint = content_fingerprint(
        _approval_fingerprint(state, stage, item_ids), maximum_paid_calls,
        ledger_state, state.get("candidates", {}),
        state.get("group_states", {}), length=32,
    )
    request_id = f"{stage}_{fingerprint[:12]}"
    approvals_root = os.path.join(state["output_dir"], "approvals")
    approvals_dir = os.path.join(approvals_root, state["run_id"])
    os.makedirs(approvals_dir, exist_ok=True)
    image_fingerprints = _approval_image_fingerprints(state, stage, extra)
    request = {
        "request_id": request_id, "stage": stage, "created_at": _now(),
        "state_fingerprint": fingerprint, "item_ids": item_ids,
        "maximum_paid_calls": maximum_paid_calls,
        "reviewed_image_fingerprints": image_fingerprints,
        **(extra or {}),
    }
    request_path = os.path.join(approvals_dir, f"{request_id}.request.json")
    decision_path = os.path.join(approvals_dir, f"{request_id}.decision.json")
    atomic_write_json(request_path, request)
    if not os.path.exists(decision_path):
        atomic_write_json(decision_path, decision_template(request))
    state["pending_approval"] = {
        **request, "request_path": os.path.relpath(request_path, state["output_dir"]),
        "decision_path": os.path.relpath(decision_path, state["output_dir"]),
    }
    atomic_write_json(os.path.join(approvals_root, "current.json"), state["pending_approval"])
    state["status"] = "awaiting_approval"
    state["stop_reason"] = stage
    _trace(state, "approval_requested", {
        "request_id": request_id, "stage": stage,
        "maximum_paid_calls": maximum_paid_calls,
    })


def _candidate_by_id(state: VisualAgentState, asset_id: str, candidate_id: str) -> dict | None:
    for candidate in state.get("candidates", {}).get(asset_id, []):
        if candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def _validated_candidate_source(state: VisualAgentState, candidate: dict) -> str:
    relative = os.path.normpath(str(candidate.get("path", "")))
    if ".attempts" in relative.split(os.sep):
        raise ValueError("unpublished paid attempt cannot be locked")
    source = os.path.join(state["output_dir"], relative)
    if not os.path.isfile(source) or os.path.getsize(source) < 1:
        raise ValueError(f"candidate file is missing: {candidate.get('candidate_id')}")
    ledger = _load_paid_ledger(state)
    ledger_job_id = str(candidate.get("ledger_job_id", ""))
    entries = ledger.get("jobs", {})
    entry = entries.get(ledger_job_id) if ledger_job_id else None
    logical_id = f"foundation:{candidate.get('candidate_id', '')}"
    if not isinstance(entry, dict):
        matches = [
            item for item in entries.values()
            if isinstance(item, dict)
            and item.get("status") == "succeeded"
            and str(item.get("logical_job_id", "")) == logical_id
        ]
        matching_path = []
        for item in matches:
            binding_version = int(item.get("artifact_binding_version", 0) or 0)
            published = str(
                item.get("published_output_path", "")
                if binding_version >= 2 else item.get("output_path", "")
            )
            if os.path.normcase(os.path.normpath(published)) == os.path.normcase(relative):
                matching_path.append(item)
        if len(matching_path) == 1:
            entry = matching_path[0]
    if not isinstance(entry, dict) or entry.get("status") != "succeeded":
        raise ValueError("candidate is not bound to a succeeded paid ledger job")
    binding_version = int(entry.get("artifact_binding_version", 0) or 0)
    published_relative = str(
        entry.get("published_output_path", "")
        if binding_version >= 2 else entry.get("output_path", "")
    )
    if os.path.normcase(os.path.normpath(published_relative)) != os.path.normcase(relative):
        raise ValueError("candidate path does not match its published ledger artifact")
    expected_sha = str(
        entry.get("published_file_sha256", "")
        if binding_version >= 2 else entry.get("file_sha256", "")
    )
    if not expected_sha or _file_fingerprint(source) != expected_sha:
        raise ValueError("candidate fingerprint does not match its published ledger artifact")
    return source


def _candidate_is_published(state: VisualAgentState, candidate: dict) -> bool:
    try:
        _validated_candidate_source(state, candidate)
    except (OSError, ValueError):
        return False
    return True


def _lock_candidate(state: VisualAgentState, asset: dict, candidate: dict,
                    change_note: str) -> dict:
    source = _validated_candidate_source(state, candidate)
    locked_dir = os.path.join(
        state["output_dir"], "assets", "foundation", "locked", state["run_id"]
    )
    os.makedirs(locked_dir, exist_ok=True)
    existing_versions = [item.get("version", 0) for item in state.get("locked_assets", {}).values()
                         if item.get("asset_id") == asset["asset_id"]]
    pattern = re.compile(rf"^{re.escape(safe_filename(asset['asset_id']))}_v(\d+)_FINAL\.png$", re.I)
    for name in os.listdir(locked_dir):
        match = pattern.match(name)
        if match:
            version = int(match.group(1))
            existing_versions.append(version)
            existing_path = os.path.join(locked_dir, name)
            # Replaying an approval after an interrupted checkpoint adopts the
            # immutable copy it already created instead of manufacturing v002.
            if (
                os.path.isfile(existing_path)
                and _file_fingerprint(existing_path) == _file_fingerprint(source)
            ):
                return {
                    "asset_id": asset["asset_id"], "asset_type": asset["asset_type"],
                    "version": version, "status": "locked",
                    "candidate_id": candidate["candidate_id"],
                    "path": os.path.relpath(existing_path, state["output_dir"]),
                    "locked_at": datetime.fromtimestamp(
                        os.path.getmtime(existing_path)
                    ).astimezone().isoformat(timespec="seconds"),
                    "change_note": change_note or "human selected candidate",
                    "dependencies": list(asset.get("dependencies", [])),
                    "recovered_from_file": True,
                }
    version = max(existing_versions or [0]) + 1
    target = os.path.join(
        locked_dir, f"{safe_filename(asset['asset_id'])}_v{version:03d}_FINAL.png"
    )
    if os.path.exists(target):
        raise ValueError(f"locked target already exists: {target}")
    shutil.copy2(source, target)
    try:
        os.chmod(target, stat.S_IREAD)
    except OSError:
        pass
    return {
        "asset_id": asset["asset_id"], "asset_type": asset["asset_type"],
        "version": version, "status": "locked", "candidate_id": candidate["candidate_id"],
        "path": os.path.relpath(target, state["output_dir"]),
        "locked_at": _now(), "change_note": change_note or "human selected candidate",
        "dependencies": list(asset.get("dependencies", [])),
    }


def _apply_pending_decision(state: VisualAgentState) -> bool:
    pending = state.get("pending_approval")
    if not isinstance(pending, dict) or not pending:
        return False
    decision_path = os.path.join(state["output_dir"], pending["decision_path"])
    decision = read_json(decision_path)
    if not decision or decision.get("decision") in {None, "", "pending"}:
        return False
    if decision.get("request_id") != pending.get("request_id"):
        raise ValueError("approval request_id mismatch")
    if decision.get("state_fingerprint") != pending.get("state_fingerprint"):
        raise ValueError("stale approval fingerprint")
    choice = str(decision.get("decision", "")).lower()
    if choice == "accept":
        choice = "approve"
    stage = pending["stage"]
    if stage.startswith("manual_review_"):
        group_id = stage.removeprefix("manual_review_")
        group_state = state.get("group_states", {}).get(group_id, {})
        transfer = group_state.get("post_foundation_reset_transfer", {})
        transfer_status = str(transfer.get("status", "")) if isinstance(transfer, dict) else ""
        scale_reconstruction = group_state.get(
            "post_transfer_scale_evidence_reconstruction", {}
        )
        scale_status = (
            str(scale_reconstruction.get("status", ""))
            if isinstance(scale_reconstruction, dict) else ""
        )
        if choice == "approve" and (
            transfer_status in {"required", "blocked"}
            or scale_status in {"required", "blocked"}
        ):
            raise ValueError(
                "post-Foundation-reset blocking issues cannot be overridden; "
                + (
                    "choose reject"
                    if scale_status == "blocked"
                    or (transfer_status == "blocked" and scale_status != "required")
                    else "choose regenerate or reject"
                )
            )
        if choice == "regenerate" and scale_status == "blocked":
            raise ValueError(
                "the scale-evidence-priority reconstruction is still blocked; reject and stop"
            )
        if (
            choice == "regenerate"
            and transfer_status == "blocked"
            and scale_status != "required"
        ):
            raise ValueError(
                "the locked-assets-only transfer is still blocked; reject and stop until a new strategy exists"
            )
    _validate_human_decision(pending, decision, choice)
    if choice == "reject":
        state["status"] = "needs_review"
        state["stop_reason"] = f"approval_rejected:{stage}"
    elif stage in {"foundation_cost", "foundation_retry_cost"} and choice == "approve":
        grant_id = _register_approval_grant(state, pending)
        if stage == "foundation_cost":
            state["foundation_primary_grant_id"] = grant_id
        else:
            state["foundation_retry_grant_id"] = grant_id
        state["foundation_grant_id"] = grant_id
        state["foundation_budget_approved"] = True
        state["status"] = "running"
        state["stop_reason"] = ""
        state["stage"] = "foundation_generate"
    elif stage.startswith("foundation_lock_") and choice == "approve":
        phase = stage.removeprefix("foundation_lock_")
        assets = [item for item in state.get("foundation_assets", []) if item["phase"] == phase]
        foundation_reset = state.get("foundation_reset", {})
        reset_asset_ids = (
            set(map(str, foundation_reset.get("asset_ids", [])))
            if isinstance(foundation_reset, dict)
            and foundation_reset.get("status") == "candidate_lock" else set()
        )
        if reset_asset_ids:
            assets = [
                asset for asset in assets
                if str(asset.get("asset_id", "")) in reset_asset_ids
            ]
        selections = decision.get("selections")
        if not isinstance(selections, dict):
            raise ValueError("foundation lock approval requires selections")
        change_note = str(decision.get("change_note", "")).strip()
        if _is_placeholder_review_text(change_note) or len(change_note) < 8:
            raise ValueError("foundation lock requires a specific human change_note")
        for asset in assets:
            candidate_id = str(selections.get(asset["asset_id"], ""))
            candidate = _candidate_by_id(state, asset["asset_id"], candidate_id)
            if candidate is None:
                raise ValueError(f"invalid candidate selection for {asset['asset_id']}")
            state.setdefault("locked_assets", {})[asset["asset_id"]] = _lock_candidate(
                state, asset, candidate, change_note
            )
        primary_grant_id = str(state.get("foundation_primary_grant_id", ""))
        if primary_grant_id:
            state["foundation_grant_id"] = primary_grant_id
        state["foundation_retry_grant_id"] = ""
        state["status"] = "running"
        state["stop_reason"] = ""
        if reset_asset_ids:
            group_id = str(foundation_reset.get("group_id", ""))
            affected_shot_ids = sorted(set(map(
                str, foundation_reset.get("affected_shot_ids", [])
            )))
            group_state = state.setdefault("group_states", {}).setdefault(group_id, {})
            group_state["approved"] = False
            group_state["grant_id"] = ""
            group_state["status"] = "reference_reset_pending"
            group_state["pending_paid_operation"] = "reference_reset_retry"
            group_state["affected_shot_ids"] = affected_shot_ids
            foundation_reset["status"] = "reference_locked"
            foundation_reset["locked_assets"] = {
                asset_id: copy.deepcopy(state.get("locked_assets", {}).get(asset_id, {}))
                for asset_id in sorted(reset_asset_ids)
            }
            foundation_reset["locked_at"] = _now()
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "foundation_reference_reset_locked",
                "requires_new_paid_grant": True,
                "maximum_paid_calls": len(affected_shot_ids),
                "shot_ids": affected_shot_ids,
                "foundation_reset": copy.deepcopy(foundation_reset),
            }
            state["stage"] = "group_approval"
        else:
            state["foundation_phase_index"] = int(state.get("foundation_phase_index", 0)) + 1
            state["stage"] = (
                "foundation_generate"
                if state["foundation_phase_index"] < len(FOUNDATION_PHASES)
                else "foundation_complete"
            )
    elif stage.startswith("foundation_lock_") and choice == "regenerate":
        phase = stage.removeprefix("foundation_lock_")
        foundation_reset = state.get("foundation_reset", {})
        reset_asset_ids = (
            set(map(str, foundation_reset.get("asset_ids", [])))
            if isinstance(foundation_reset, dict)
            and foundation_reset.get("status") == "candidate_lock" else set()
        )
        for asset in state.get("foundation_assets", []):
            if asset["phase"] == phase and (
                not reset_asset_ids or str(asset.get("asset_id", "")) in reset_asset_ids
            ):
                state.setdefault("candidates", {}).pop(asset["asset_id"], None)
                state.setdefault("rankings", {}).pop(asset["asset_id"], None)
        if reset_asset_ids:
            for asset_id in reset_asset_ids:
                foundation_reset.setdefault("round_by_asset", {})[asset_id] = (
                    int(foundation_reset.get("round_by_asset", {}).get(asset_id, 1)) + 1
                )
            foundation_reset["status"] = "candidate_approval"
        state["foundation_budget_approved"] = False
        state["status"] = "running"
        state["stage"] = "foundation_retry_approval"
    elif stage.startswith("scene_group_cost_") and choice == "approve":
        group_id = stage.removeprefix("scene_group_cost_")
        group_state = state.setdefault("group_states", {}).setdefault(group_id, {})
        group_state["grant_id"] = _register_approval_grant(state, pending)
        group_state["approved"] = True
        group_state["status"] = "approved"
        if group_state.get("pending_paid_operation") == "post_foundation_reset_transfer":
            transfer = group_state.get("post_foundation_reset_transfer", {})
            if isinstance(transfer, dict):
                transfer["status"] = "approved"
                transfer["grant_id"] = group_state["grant_id"]
                transfer["approved_at"] = _now()
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "post_foundation_reset_transfer_approved",
            }
        elif group_state.get("pending_paid_operation") == "scale_evidence_reconstruction":
            scale_reconstruction = group_state.get(
                "post_transfer_scale_evidence_reconstruction", {}
            )
            if isinstance(scale_reconstruction, dict):
                scale_reconstruction["status"] = "approved"
                scale_reconstruction["grant_id"] = group_state["grant_id"]
                scale_reconstruction["approved_at"] = _now()
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "post_transfer_scale_evidence_reconstruction_approved",
            }
        elif group_state.get("pending_paid_operation") == "provider_escalation":
            escalation = group_state.get("provider_escalation", {})
            if not isinstance(escalation, dict) or not escalation.get("tasks"):
                raise ValueError("provider escalation approval is missing its reviewed tasks")
            escalation["status"] = "approved"
            escalation["grant_id"] = group_state["grant_id"]
            escalation["approved_at"] = _now()
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "provider_escalation_approved",
            }
        state["status"] = "running"
        state["stop_reason"] = ""
        state["stage"] = (
            "group_retry" if group_state.get("pending_paid_operation") in {
                "retry", "technical_retry", "reference_reset_retry",
                "post_foundation_reset_transfer", "scale_evidence_reconstruction",
                "provider_escalation",
            }
            else "group_generate"
        )
    elif stage.startswith("manual_review_") and choice == "approve":
        reason = str(decision.get("override_reason", "")).strip()
        if _is_placeholder_review_text(reason) or len(reason) < 12:
            raise ValueError("manual semantic override requires a specific override_reason")
        group_id = stage.removeprefix("manual_review_")
        group_state = state.setdefault("group_states", {}).setdefault(group_id, {})
        if any(issue.get("non_overridable") for issue in group_state.get("issues", [])):
            raise ValueError("technical image issues cannot be human-overridden")
        group_state["human_override"] = {
            "reason": reason, "reviewer": str(decision.get("reviewer", "")).strip(),
            "reviewed_item_ids": decision.get("reviewed_item_ids", []),
            "issue_override_reasons": {
                str(key): str(value).strip()
                for key, value in decision.get("issue_override_reasons", {}).items()
            } if isinstance(decision.get("issue_override_reasons"), dict) else {},
            "at": _now(),
        }
        group_state["status"] = "accepted"
        state["status"] = "running"
        state["stop_reason"] = ""
        state["stage"] = "group_finalize"
    elif stage.startswith("manual_review_") and choice == "regenerate":
        group_id = stage.removeprefix("manual_review_")
        group_state = state.setdefault("group_states", {}).setdefault(group_id, {})
        group_state["approved"] = False
        group_state["status"] = "planned"
        group_state["manual_regeneration"] = int(group_state.get("manual_regeneration", 0)) + 1
        transfer = group_state.get("post_foundation_reset_transfer", {})
        scale_reconstruction = group_state.get(
            "post_transfer_scale_evidence_reconstruction", {}
        )
        if (
            isinstance(scale_reconstruction, dict)
            and scale_reconstruction.get("status") == "required"
        ):
            scale_reconstruction["status"] = "approval_pending"
            scale_reconstruction["manual_decision_applied_at"] = _now()
            group_state["pending_paid_operation"] = "scale_evidence_reconstruction"
            shot_ids = sorted(set(map(str, scale_reconstruction.get("shot_ids", []))))
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "post_transfer_scale_evidence_reconstruction_pending_approval",
                "requires_new_paid_grant": True,
                "maximum_paid_calls": len(shot_ids),
                "shot_ids": shot_ids,
            }
        elif isinstance(transfer, dict) and transfer.get("status") == "required":
            transfer["status"] = "approval_pending"
            transfer["manual_decision_applied_at"] = _now()
            group_state["pending_paid_operation"] = "post_foundation_reset_transfer"
            shot_ids = sorted(set(map(str, transfer.get("shot_ids", []))))
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "post_foundation_reset_transfer_pending_approval",
                "requires_new_paid_grant": True,
                "maximum_paid_calls": len(shot_ids),
                "shot_ids": shot_ids,
            }
        else:
            group_state["pending_paid_operation"] = (
                "retry" if any(issue.get("blocking") and issue.get("shot_id")
                               for issue in group_state.get("issues", [])) else "initial"
            )
        state["status"] = "running"
        state["stage"] = "group_approval"
    else:
        raise ValueError(f"unsupported approval decision {choice!r} for {stage}")
    _trace(state, "approval_applied", {"request_id": pending["request_id"], "decision": choice})
    atomic_write_json(os.path.join(state["output_dir"], "approvals", "current.json"), {
        "run_id": state["run_id"],
        "status": "none",
        "last_applied_request_id": pending["request_id"],
        "last_decision": choice,
        "applied_at": _now(),
    })
    state["pending_approval"] = {}
    return True


def _location_parts(heading: str) -> tuple[str, str]:
    text = re.sub(r"^\s*(?:INT\.?|EXT\.?|内景|外景)\s*", "", heading, flags=re.I)
    parts = [part.strip() for part in re.split(r"\s+-\s+|－|—", text) if part.strip()]
    return (parts[0] if parts else text.strip() or "unspecified location",
            parts[1] if len(parts) > 1 else "")


def _build_inventory(storyboard: dict) -> dict:
    characters: list[dict] = []
    for index, character in enumerate(get_characters(storyboard), 1):
        character_id = str(character.get("character_id") or f"character_{index:03d}")
        characters.append({
            "character_id": character_id, "name": str(character.get("name", "")),
            "anchor": str(character.get("anchor_description", "")),
            "anchor_en": str(character.get("anchor_description_en", "")),
        })
    scenes: list[dict] = []
    structured_props: dict[str, dict] = {}
    for scene_index, scene in enumerate(storyboard.get("scenes", []), 1):
        scene_id = str(scene.get("scene_id") or scene_index)
        location, time_state = _location_parts(get_scene_heading(scene))
        visible_ids: set[str] = set()
        shot_ids: list[str] = []
        for shot in scene.get("shots", []):
            shot_ids.append(str(shot.get("shot_id", "")))
            visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
            visible_ids.update(str(item) for item in visual.get("visible_character_ids", []) if str(item))
            props = visual.get("key_props", [])
            if isinstance(props, list):
                for prop in props:
                    if isinstance(prop, dict):
                        prop_id = str(prop.get("prop_id") or content_fingerprint(prop, length=10))
                        structured_props[prop_id] = {
                            "asset_kind": str(prop.get("asset_kind") or "story_key_prop"),
                            "prop_id": prop_id, **prop,
                        }
        scenes.append({
            "scene_id": scene_id, "heading": get_scene_heading(scene),
            "location": location, "time_state": time_state,
            "visible_character_ids": sorted(visible_ids), "shot_ids": shot_ids,
        })
        scene_props = scene.get("key_props", [])
        if isinstance(scene_props, list):
            for prop in scene_props:
                if isinstance(prop, dict):
                    prop_id = str(prop.get("prop_id") or content_fingerprint(prop, length=10))
                    structured_props[prop_id] = {
                        "asset_kind": str(prop.get("asset_kind") or "story_key_prop"),
                        "prop_id": prop_id, **prop,
                    }
    return {"characters": characters, "scenes": scenes, "props": list(structured_props.values())}


def _structured_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        text = " ".join(value.split()).strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [text for item in value.values() for text in _structured_text_values(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _structured_text_values(item)]
    return []


def _declared_scale_contract(spec: dict) -> dict:
    """Extract generic source scale expressions without story-specific object terms."""
    patterns = (
        re.compile(r"[^,，。；;]{1,40}(?:大小|尺寸|尺度)[^,，。；;]{0,24}"),
        re.compile(r"\b[^,.;]{1,40}(?:-sized| sized|size of)[^,.;]{0,24}\b", re.I),
        re.compile(
            r"\b\d+(?:\.\d+)?\s*(?:mm|cm|m|in|inch|inches|ft|feet)\b|"
            r"\d+(?:\.\d+)?\s*(?:毫米|厘米|公分|米)"
        ),
    )
    cues: list[str] = []
    for text in _structured_text_values(spec):
        for pattern in patterns:
            for match in pattern.finditer(text):
                cue = " ".join(match.group(0).split()).strip(" ,，。；;")
                if cue and cue not in cues:
                    cues.append(cue[:160])
    required = bool(cues)
    return {
        "schema_version": 1,
        "role": "source_declared_scale",
        "required": required,
        "source_cues": cues[:8],
        "canonical_image_is_scale_evidence": False,
        "shot_policy": (
            "Preserve the declared real-world size relative to visible hands, pockets, furniture and "
            "other in-scene comparators; never enlarge the prop merely for readability."
            if required else "No explicit source scale constraint was detected."
        ),
    }


def _asset_scale_contract(asset: dict) -> dict:
    contract = asset.get("reference_contract", {})
    if isinstance(contract, dict) and isinstance(contract.get("scale_contract"), dict):
        return copy.deepcopy(contract["scale_contract"])
    spec = asset.get("spec", {})
    return _declared_scale_contract(spec if isinstance(spec, dict) else {})


def _scale_evidence_contract(asset: dict) -> dict:
    """Describe reviewable scale evidence without assuming a specific prop class."""
    if not _asset_scale_contract(asset).get("required"):
        return {}
    return {
        "schema_version": 2,
        "scale_evidence_mode": "size_appropriate_direct_contact",
        "minimum_comparator_visibility": "complete_and_identifiable",
        "contact_required": True,
        "same_focal_plane_required": True,
        "cropped_or_blurred_comparator_allowed": False,
        "handheld_policy": (
            "For a safely hand-held object, show the complete object resting on a fully visible open palm "
            "or stably held between clearly visible fingers."
        ),
        "non_handheld_policy": (
            "For a larger or non-hand-held object, use a complete familiar environment comparator in direct "
            "contact or on the same support plane; do not force the object into a hand."
        ),
    }


def _effective_reference_contract(asset: dict) -> dict:
    contract = asset.get("reference_contract", {})
    contract = copy.deepcopy(contract) if isinstance(contract, dict) else {}
    if str(asset.get("asset_type", "")) == "key_prop":
        contract["reference_contract_revision"] = 2
        contract.setdefault("role", "canonical_geometry_anchor")
        contract.setdefault("single_object", True)
        contract.setdefault("single_view", True)
        contract.setdefault("clean_background", True)
        contract.setdefault("forbid_grids_and_state_sequences", True)
        contract.setdefault("dynamic_state_source", "shot_prompt")
        contract["scale_contract"] = _asset_scale_contract(asset)
        scale_evidence = _scale_evidence_contract(asset)
        if scale_evidence:
            contract["scale_evidence_contract"] = scale_evidence
        else:
            contract.pop("scale_evidence_contract", None)
    return contract


def _backfill_foundation_reference_contracts(state: VisualAgentState) -> list[str]:
    """Persist effective key-prop contracts when resuming pre-contract runs."""
    updated: set[str] = set()

    def backfill(assets: Any) -> None:
        if not isinstance(assets, list):
            return
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("asset_type") != "key_prop":
                continue
            effective = _effective_reference_contract(asset)
            if asset.get("reference_contract") != effective:
                asset["reference_contract"] = effective
                asset_id = str(asset.get("asset_id", ""))
                if asset_id:
                    updated.add(asset_id)

    backfill(state.get("foundation_assets", []))
    visual_bible = state.get("visual_bible", {})
    if isinstance(visual_bible, dict):
        backfill(visual_bible.get("asset_specs", []))
    return sorted(updated)


def _build_foundation_assets(storyboard: dict, inventory: dict) -> list[dict]:
    assets: list[dict] = [{
        "asset_id": "style_001", "asset_type": "style_board", "phase": "style",
        "label": "project theme and style board", "dependencies": [],
        "spec": {"style_anchor": get_style_anchor(storyboard),
                 "aspect_ratio": storyboard.get("creative_bible", {}).get("aspect_ratio", "9:16")},
    }]
    emotional_shots = any(
        bool(get_visual(shot, "composition_emotion"))
        for scene in storyboard.get("scenes", []) for shot in scene.get("shots", [])
    )
    for character in inventory["characters"]:
        cid = character["character_id"]
        identity_id = f"char_{safe_filename(cid)}_identity"
        assets.append({
            "asset_id": identity_id, "asset_type": "character_identity",
            "phase": "character_identity", "label": f"identity sheet: {character['name'] or cid}",
            "dependencies": ["style_001"], "spec": character,
        })
        assets.append({
            "asset_id": f"char_{safe_filename(cid)}_turnaround",
            "asset_type": "character_turnaround", "phase": "character_turnaround",
            "label": f"front side back turnaround: {character['name'] or cid}",
            "dependencies": ["style_001", identity_id], "spec": character,
        })
        if emotional_shots:
            assets.append({
                "asset_id": f"char_{safe_filename(cid)}_expression_pose",
                "asset_type": "character_expression_pose", "phase": "character_expression_pose",
                "label": f"expression and pose sheet: {character['name'] or cid}",
                "dependencies": ["style_001", identity_id], "spec": character,
            })
    set_pieces = [
        item for item in inventory["props"]
        if str(item.get("asset_kind", "")) == "set_piece"
    ]
    location_ids: dict[str, str] = {}
    for scene in inventory["scenes"]:
        key = f"{scene['location']}|{scene['time_state']}"
        if key in location_ids:
            scene["location_asset_id"] = location_ids[key]
            continue
        asset_id = f"location_{content_fingerprint(key, length=10)}"
        location_ids[key] = asset_id
        scene["location_asset_id"] = asset_id
        assets.append({
            "asset_id": asset_id, "asset_type": "location_master", "phase": "location",
            "label": f"location master: {scene['location']} {scene['time_state']}",
            "dependencies": ["style_001"],
            "spec": {
                "location": scene["location"], "time_state": scene["time_state"],
                "required_set_pieces": set_pieces,
            },
        })
    for prop in inventory["props"]:
        asset_kind = str(prop.get("asset_kind") or "story_key_prop")
        if asset_kind in {"wardrobe", "set_piece"}:
            continue
        prop_id = str(prop["prop_id"])
        scale_contract = _declared_scale_contract(prop)
        assets.append({
            "asset_id": f"prop_{safe_filename(prop_id)}", "asset_type": "key_prop",
            "phase": "prop", "label": f"key prop: {prop.get('name') or prop_id}",
            "dependencies": ["style_001"], "spec": {**prop, "asset_kind": asset_kind},
            "reference_contract": {
                "reference_contract_revision": 2,
                "role": "canonical_geometry_anchor",
                "single_object": True,
                "single_view": True,
                "clean_background": True,
                "forbid_grids_and_state_sequences": True,
                "dynamic_state_source": "shot_prompt",
                "scale_contract": scale_contract,
                **({
                    "scale_evidence_contract": _scale_evidence_contract({
                        "reference_contract": {"scale_contract": scale_contract},
                    }),
                } if scale_contract.get("required") else {}),
            },
        })
    return assets


def _asset_prompt(
    asset: dict, storyboard: dict, candidate_number: int, generation_round: int = 1,
) -> str:
    asset_type = str(asset["asset_type"])
    # Approval and delivery rules are enforced by code. Inject only visual
    # rules relevant to this asset phase so sibling phases cannot contaminate
    # one another (for example, turnaround rules inside an identity prompt).
    playbook = [
        section for section in get_playbook_sections(["prompt"])
        if section.get("section_id") == "prompt_compilation"
    ]
    if asset_type in {
        "character_identity", "character_turnaround", "character_expression_pose",
    }:
        for section in get_playbook_sections(["character"]):
            rules = list(section.get("rules", []))
            if asset_type != "character_turnaround":
                rules = [rule for index, rule in enumerate(rules) if index != 1]
            playbook.append({**section, "rules": rules})
    elif asset_type == "location_master":
        playbook.extend(get_playbook_sections(["location"]))
    prompt_parts: list[str] = []
    if asset_type == "style_board":
        prompt_parts.extend([
            "STYLE-ONLY BOARD. Show no people, bodies, faces, silhouettes, garments or character sheets",
            "Convert every character, costume or wearable mentioned by the source style into abstract palette "
            "swatches, material samples or lighting accents; never depict the named person or garment",
            asset["label"], json.dumps(asset.get("spec", {}), ensure_ascii=False),
            f"coherent project style: {get_style_anchor(storyboard)}",
            "show representative environments, lighting, palette, materials and visual atmosphere; "
            "this board is not a character identity reference",
        ])
    else:
        prompt_parts.extend([
            asset["label"], json.dumps(asset.get("spec", {}), ensure_ascii=False),
            f"coherent project style: {get_style_anchor(storyboard)}",
        ])
    prompt_parts.append("clean professional reference sheet, consistent design, no watermark, no text labels")
    if asset_type == "character_identity":
        prompt_parts.append(
            "one canonical identity view plus face and material details; do not create front-side-back "
            "turnaround panels or expression grids because those are separate approved assets"
        )
    elif asset_type == "character_turnaround":
        prompt_parts.append("same character in front side and back views, neutral pose, identical costume and proportions")
    elif asset_type == "character_expression_pose":
        prompt_parts.append("same character with joy anger sadness fear surprise and standing sitting action poses")
    elif asset_type == "location_master":
        prompt_parts.append("wide spatial master, clear entrances and exits, foreground midground background, material and light logic")
    elif asset_type == "key_prop":
        scale_contract = _asset_scale_contract(asset)
        prompt_parts.append(
            "CANONICAL GEOMETRY ANCHOR: depict exactly one complete key prop in exactly one canonical "
            "three-quarter view on a plain clean background. No grid, no contact sheet, no inset, no multiple "
            "angles, no duplicated object, no before/after comparison and no color or state sequence. Stable "
            "silhouette, proportions, scale cues and materials are mandatory; physical_spec is authoritative. "
            "Preserve object class and topology, including solid/open construction, cutouts, notches, opacity, "
            "mounting and motion; never substitute a visually related but structurally different object. "
            "Dynamic color, glow, damage or transformation states belong to individual shot prompts and must "
            "not be represented as extra copies in this anchor"
        )
        if scale_contract.get("required"):
            scale_evidence = _scale_evidence_contract(asset)
            prompt_parts.append(
                "SOURCE SCALE CONTRACT: "
                + json.dumps(scale_contract, ensure_ascii=False)
                + ". SCALE EVIDENCE CONTRACT: "
                + json.dumps(scale_evidence, ensure_ascii=False)
                + ". Show one complete, identifiable natural comparator appropriate to the declared cue while "
                  "keeping exactly one instance of the key prop. Keep the comparator and the complete prop in "
                  "direct contact or on the same support plane, sharp and readable in the same focal plane. For "
                  "a safely hand-held prop, prefer the complete prop on a fully visible open palm or stably "
                  "between clearly visible fingers. For a larger or non-hand-held prop, use a complete familiar "
                  "environment element on the same support plane and do not force the prop into a hand. A cropped "
                  "edge, partial fingertip fragment, blurred body part, floating or detached comparator, or a "
                  "comparator at another depth or focal plane is invalid. The comparator is scale evidence only, "
                  "not a second designed asset, inset, label, ruler, duplicate view or state sequence. A large "
                  "frame-filling isolated product view is not scale evidence"
            )
            if generation_round >= 2:
                prompt_parts.append(
                    f"FOUNDATION RESET ROUND {generation_round}: prior scale evidence did not support reliable "
                    "shot-scale transfer. Make the complete comparator, physical contact or shared support plane, "
                    "and relative size unmistakable at normal viewing size; do not repeat weak cropped, blurred, "
                    "detached or different-depth evidence"
                )
    prompt_parts.append(f"design candidate {candidate_number}; vary design direction without changing required facts")
    prompt_parts.append("rules: " + json.dumps(playbook, ensure_ascii=False))
    return ". ".join(part for part in prompt_parts if part)


def _locked_reference_paths(state: VisualAgentState, asset_ids: list[str]) -> list[str]:
    paths: list[str] = []
    for asset_id in asset_ids:
        locked = state.get("locked_assets", {}).get(asset_id)
        if isinstance(locked, dict):
            path = os.path.join(state["output_dir"], locked.get("path", ""))
            if os.path.isfile(path):
                paths.append(path)
    return paths


def _foundation_asset_index(state: VisualAgentState) -> dict[str, dict]:
    return {
        str(asset.get("asset_id", "")): asset
        for asset in state.get("foundation_assets", [])
        if isinstance(asset, dict) and str(asset.get("asset_id", ""))
    }


def _revision_reference_strategy(
    state: VisualAgentState,
    asset_ids: list[str],
    shot: dict | None,
    issues: list[dict] | None,
    revision_attempt_number: int = 0,
    post_foundation_reset_transfer: bool = False,
    scale_evidence_reconstruction: bool = False,
    constraint_isolation_task: dict | None = None,
) -> dict:
    """Choose authoritative references from structured issue evidence."""
    asset_index = _foundation_asset_index(state)
    ordered = [
        asset_id for asset_id in dict.fromkeys(map(str, asset_ids))
        if asset_id in state.get("locked_assets", {})
    ]
    issue_items = [item for item in (issues or []) if isinstance(item, dict)]
    focus_ids = [
        str(asset_id)
        for issue in issue_items
        for asset_id in issue.get("focus_asset_ids", [])
        if str(asset_id) in ordered
    ]
    if isinstance(constraint_isolation_task, dict):
        focus_ids = [
            str(asset_id)
            for asset_id in constraint_isolation_task.get("focus_asset_ids", [])
            if str(asset_id) in ordered
        ] or focus_ids
    correction_targets = {
        str(issue.get("correction_target", "")).strip().lower()
        for issue in issue_items
    }
    categories = {
        str(issue.get("category", "")).strip().lower()
        for issue in issue_items
    }

    def ids_of_type(*asset_types: str) -> list[str]:
        wanted = set(asset_types)
        return [
            asset_id for asset_id in ordered
            if str(asset_index.get(asset_id, {}).get("asset_type", "")) in wanted
        ]

    visible_prop_ids = set(map(str, (shot or {}).get("visible_prop_ids", [])))
    visible_prop_assets = [
        asset_id for asset_id in ids_of_type("key_prop")
        if str(asset_index.get(asset_id, {}).get("spec", {}).get("prop_id", ""))
        in visible_prop_ids
    ]
    scale_evidence_ids = [
        asset_id for asset_id in dict.fromkeys([*focus_ids, *ordered])
        if asset_id in visible_prop_assets
        and _asset_scale_contract(asset_index.get(asset_id, {})).get("required") is True
    ]
    isolation_enabled = bool(
        isinstance(constraint_isolation_task, dict)
        and constraint_isolation_task.get("active_issue_id")
    )
    primary_role = "constraint_isolated_edit" if isolation_enabled else "previous_shot_edit"
    primary_ids: list[str] = []
    if isolation_enabled:
        primary_ids = list(dict.fromkeys(focus_ids))
    elif "prop_geometry" in correction_targets or categories.intersection({
        "prop_geometry", "prop_identity", "prop_continuity", "asset_binding",
    }):
        primary_role = "canonical_prop_geometry"
        primary_ids = [asset_id for asset_id in focus_ids if asset_id in visible_prop_assets]
        primary_ids = primary_ids or visible_prop_assets
    elif "location_structure" in correction_targets or categories.intersection({
        "location_assets", "location_structure", "spatial_continuity",
    }):
        primary_role = "locked_location_structure"
        location_ids = ids_of_type("location_master")
        primary_ids = [asset_id for asset_id in focus_ids if asset_id in location_ids]
        primary_ids = primary_ids or location_ids
    elif "character_identity" in correction_targets or categories.intersection({
        "identity", "character_identity", "visible_entity_consistency",
    }):
        primary_role = "locked_character_identity"
        character_ids = ids_of_type(
            "character_identity", "character_turnaround", "character_expression_pose"
        )
        primary_ids = [asset_id for asset_id in focus_ids if asset_id in character_ids]
        primary_ids = primary_ids or character_ids
    elif "temporal_state" in correction_targets and focus_ids:
        primary_role = "locked_state_identity"
        primary_ids = focus_ids
    elif "effect_alignment" in correction_targets and focus_ids:
        primary_role = "focused_effect_assets"
        primary_ids = focus_ids
    if not primary_ids and not isolation_enabled:
        primary_role = "previous_shot_edit"
    capabilities = state.get("provider_capabilities", {})
    single_reference_provider = bool(
        capabilities.get("reference_mode") == "single"
        or int(capabilities.get("max_references", 0) or 0) == 1
    )
    repeated_authoritative_geometry = bool(
        revision_attempt_number >= 2
        and primary_ids
        and correction_targets.intersection({"prop_geometry", "location_structure"})
    )
    if isolation_enabled:
        pass
    elif scale_evidence_reconstruction and scale_evidence_ids:
        primary_role = "scale_evidence_priority_reconstruction"
        primary_ids = scale_evidence_ids
    elif post_foundation_reset_transfer:
        primary_role = "post_reset_locked_assets_transfer"
        primary_ids = list(dict.fromkeys([*focus_ids, *ordered]))
    elif revision_attempt_number >= 3:
        primary_role = "clean_regeneration"
        primary_ids = list(dict.fromkeys([*focus_ids, *primary_ids])) or [
            asset_id for asset_id in ordered
            if str(asset_index.get(asset_id, {}).get("asset_type", "")) != "style_board"
        ][:1]
    elif single_reference_provider and repeated_authoritative_geometry:
        # A composite edit board gives a single-reference provider two competing
        # geometric anchors. Once the same structured contract has failed, use
        # the locked geometry alone and rebuild the scene from storyboard facts.
        primary_role = "clean_regeneration"
        primary_ids = list(dict.fromkeys([*focus_ids, *primary_ids]))
    elif revision_attempt_number >= 2 and primary_role == "previous_shot_edit" and focus_ids:
        primary_role = "focused_locked_assets"
        primary_ids = list(dict.fromkeys(focus_ids))
    canonical_only_reference = bool(
        primary_role == "clean_regeneration"
        and single_reference_provider
        and repeated_authoritative_geometry
        and primary_ids
    )
    prioritized = (
        list(dict.fromkeys([*primary_ids, *focus_ids]))
        if isolation_enabled else
        list(dict.fromkeys([*primary_ids, *focus_ids, *ordered]))
    )
    if shot:
        compiled = compile_shot_constraints(shot, asset_index)
        prioritized = prioritize_reference_assets(
            prioritized, compiled, focus_asset_ids=focus_ids
        )
    return {
        "primary_role": primary_role,
        "primary_asset_ids": primary_ids,
        "focus_asset_ids": list(dict.fromkeys(focus_ids)),
        "ordered_asset_ids": prioritized,
        "correction_targets": sorted(value for value in correction_targets if value),
        "issue_categories": sorted(value for value in categories if value),
        "revision_attempt_number": int(revision_attempt_number),
        "exclude_failed_shot_reference": (
            primary_role in {
                "clean_regeneration", "post_reset_locked_assets_transfer",
                "scale_evidence_priority_reconstruction",
            }
        ),
        "exclude_temporal_image_references": (
            isolation_enabled or post_foundation_reset_transfer or scale_evidence_reconstruction
        ),
        "locked_assets_only_reference": (
            post_foundation_reset_transfer or scale_evidence_reconstruction
        ),
        "scale_evidence_priority_reference": bool(
            scale_evidence_reconstruction and scale_evidence_ids
        ),
        "scale_evidence_asset_ids": scale_evidence_ids,
        "canonical_only_reference": canonical_only_reference,
        "constraint_isolation": isolation_enabled,
        "constraint_isolation_task": copy.deepcopy(
            constraint_isolation_task if isolation_enabled else {}
        ),
    }


def _compose_reference_board(state: VisualAgentState, reference_paths: list[str],
                             board_id: str, asset_ids: list[str] | None = None) -> str:
    if not reference_paths:
        return ""
    if len(reference_paths) == 1:
        return reference_paths[0]
    from PIL import Image, ImageOps

    # A board represents a hard reference contract. Silently dropping the
    # seventh asset used to remove the location master in common two-character
    # scenes. Per-shot boards are smaller, and every requested asset is kept.
    included = list(reference_paths)
    fingerprint = content_fingerprint(
        [(path, os.path.getsize(path), int(os.path.getmtime(path))) for path in included], length=12
    )
    boards_dir = os.path.join(
        state["output_dir"], "assets", "reference_boards", state["run_id"]
    )
    os.makedirs(boards_dir, exist_ok=True)
    target = os.path.join(boards_dir, f"{safe_filename(board_id)}_{fingerprint}.png")
    manifest_path = target + ".manju.json"
    manifest = {
        "board_id": board_id,
        "asset_ids": list(asset_ids or []),
        "sources": [os.path.relpath(path, state["output_dir"]) for path in included],
    }
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        atomic_write_json(manifest_path, manifest)
        return target
    tile_size = (512, 512)
    columns = 2 if len(included) <= 6 else 3
    rows = (len(included) + columns - 1) // columns
    board = Image.new("RGB", (tile_size[0] * columns, tile_size[1] * rows), "white")
    for index, path in enumerate(included):
        with Image.open(path) as source:
            tile = ImageOps.contain(source.convert("RGB"), tile_size)
        x = (index % columns) * tile_size[0] + (tile_size[0] - tile.width) // 2
        y = (index // columns) * tile_size[1] + (tile_size[1] - tile.height) // 2
        board.paste(tile, (x, y))
    board.save(target, format="PNG")
    atomic_write_json(manifest_path, manifest)
    return target


def _provider_references(state: VisualAgentState, asset_ids: list[str], board_id: str) -> list[str]:
    paths = _locked_reference_paths(state, asset_ids)
    capabilities = state.get("provider_capabilities", {})
    if capabilities.get("reference_mode") == "multi":
        return paths[:int(capabilities.get("max_references", 8))]
    existing_ids = [asset_id for asset_id in asset_ids if asset_id in state.get("locked_assets", {})]
    board = _compose_reference_board(state, paths, board_id, existing_ids)
    return [board] if board else []


def _compose_revision_reference_board(
    state: VisualAgentState,
    previous_shot_path: str,
    reference_paths: list[str],
    board_id: str,
    asset_ids: list[str],
    temporal_context: list[dict] | None = None,
    reference_strategy: dict | None = None,
) -> str:
    from PIL import Image, ImageOps

    temporal_context = [
        item for item in (temporal_context or [])
        if isinstance(item, dict) and os.path.isfile(str(item.get("path", "")))
    ]
    temporal_paths = [str(item["path"]) for item in temporal_context]
    strategy = dict(reference_strategy or {})
    primary_role = str(strategy.get("primary_role", "previous_shot_edit"))
    primary_asset_ids = list(map(str, strategy.get("primary_asset_ids", [])))
    path_by_asset = {
        asset_id: path for asset_id, path in zip(asset_ids, reference_paths)
    }
    primary_asset_paths = [
        path_by_asset[asset_id] for asset_id in primary_asset_ids
        if asset_id in path_by_asset and os.path.isfile(path_by_asset[asset_id])
    ]
    primary_path = (
        primary_asset_paths[0]
        if primary_role not in {"previous_shot_edit", "constraint_isolated_edit"}
        and primary_asset_paths
        else previous_shot_path
    )
    exclude_previous = bool(strategy.get("exclude_failed_shot_reference"))
    exclude_temporal = bool(strategy.get("exclude_temporal_image_references"))
    supporting_candidates = [
        *([] if exclude_previous else [previous_shot_path]),
        *primary_asset_paths[1:], *([] if exclude_temporal else temporal_paths), *reference_paths,
    ]
    supporting = [
        path for path in dict.fromkeys(supporting_candidates)
        if os.path.abspath(path) != os.path.abspath(primary_path)
    ]
    included = [primary_path] + supporting
    fingerprint = content_fingerprint(
        [(path, os.path.getsize(path), int(os.path.getmtime(path))) for path in included],
        primary_role, bool(strategy.get("scale_evidence_priority_reference")), length=12,
    )
    boards_dir = os.path.join(
        state["output_dir"], "assets", "reference_boards", state["run_id"]
    )
    os.makedirs(boards_dir, exist_ok=True)
    target = os.path.join(boards_dir, f"{safe_filename(board_id)}_{fingerprint}.png")
    manifest = {
        "board_id": board_id,
        "board_role": "targeted_revision",
        "primary_reference": os.path.relpath(primary_path, state["output_dir"]),
        "primary_reference_role": primary_role,
        "primary_shot_reference": os.path.relpath(previous_shot_path, state["output_dir"]),
        "edit_target_shot_reference": os.path.relpath(previous_shot_path, state["output_dir"]),
        "failed_shot_reference_included": not exclude_previous,
        "temporal_context": [{
            "group_id": str(item.get("group_id", "")),
            "shot_id": str(item.get("shot_id", "")),
            "path": os.path.relpath(str(item["path"]), state["output_dir"]),
            "role": "adjacent_continuity_reference",
        } for item in ([] if exclude_temporal else temporal_context)],
        "excluded_temporal_context": [{
            "group_id": str(item.get("group_id", "")),
            "shot_id": str(item.get("shot_id", "")),
            "path": os.path.relpath(str(item["path"]), state["output_dir"]),
            "role": "excluded_adjacent_image_reference",
        } for item in (temporal_context if exclude_temporal else [])],
        "temporal_image_references_excluded": exclude_temporal,
        "excluded_image_reference_paths": [
            os.path.relpath(path, state["output_dir"])
            for path in dict.fromkeys([
                *([previous_shot_path] if exclude_previous else []),
                *(temporal_paths if exclude_temporal else []),
            ])
        ],
        "supporting_asset_ids": list(asset_ids),
        "reference_strategy": strategy,
        "supporting_sources": [
            os.path.relpath(path, state["output_dir"]) for path in supporting
        ],
    }
    scale_evidence_priority = bool(strategy.get("scale_evidence_priority_reference"))
    if scale_evidence_priority:
        manifest["reference_layout"] = {
            "mode": "dominant_full_comparator_scale_evidence",
            "canvas_size": [1536, 1536],
            "primary_region": [0, 0, 1536, 1024],
            "supporting_region": [0, 1024, 1536, 512],
            "primary_canvas_fraction": 2 / 3,
            "scale_evidence_asset_ids": list(strategy.get("scale_evidence_asset_ids", [])),
        }
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        atomic_write_json(target + ".manju.json", manifest)
        return target

    if scale_evidence_priority:
        canvas_width, canvas_height = 1536, 1536
        primary_height, support_height = 1024, 512
        board = Image.new("RGB", (canvas_width, canvas_height), "white")
        with Image.open(primary_path) as source:
            primary = ImageOps.contain(source.convert("RGB"), (canvas_width, primary_height))
        board.paste(primary, (
            (canvas_width - primary.width) // 2,
            (primary_height - primary.height) // 2,
        ))
        if supporting:
            columns = min(5, max(1, (len(supporting) + 1) // 2))
            rows = (len(supporting) + columns - 1) // columns
            tile_width = canvas_width // columns
            tile_height = support_height // rows
            for index, path in enumerate(supporting):
                with Image.open(path) as source:
                    tile = ImageOps.contain(source.convert("RGB"), (tile_width, tile_height))
                column = index % columns
                row = index // columns
                x = column * tile_width + (tile_width - tile.width) // 2
                y = primary_height + row * tile_height + (tile_height - tile.height) // 2
                board.paste(tile, (x, y))
    else:
        support_height = 256
        canvas_height = max(1024, support_height * max(1, len(supporting)))
        primary_width = 1024
        support_width = 512
        board = Image.new("RGB", (primary_width + support_width, canvas_height), "white")
        with Image.open(primary_path) as source:
            primary = ImageOps.contain(source.convert("RGB"), (primary_width, canvas_height))
        board.paste(primary, (
            (primary_width - primary.width) // 2,
            (canvas_height - primary.height) // 2,
        ))
        for index, path in enumerate(supporting):
            with Image.open(path) as source:
                tile = ImageOps.contain(source.convert("RGB"), (support_width, support_height))
            x = primary_width + (support_width - tile.width) // 2
            y = index * support_height + (support_height - tile.height) // 2
            board.paste(tile, (x, y))
    board.save(target, format="PNG")
    atomic_write_json(target + ".manju.json", manifest)
    return target


def _revision_provider_references(
    state: VisualAgentState,
    asset_ids: list[str],
    board_id: str,
    previous_shot_path: str,
    temporal_context: list[dict] | None = None,
    shot: dict | None = None,
    issues: list[dict] | None = None,
    revision_attempt_number: int = 0,
    post_foundation_reset_transfer: bool = False,
    scale_evidence_reconstruction: bool = False,
    constraint_isolation_task: dict | None = None,
) -> tuple[list[str], dict]:
    strategy = _revision_reference_strategy(
        state, asset_ids, shot, issues, revision_attempt_number,
        post_foundation_reset_transfer, scale_evidence_reconstruction,
        constraint_isolation_task,
    )
    ordered_asset_ids = list(strategy["ordered_asset_ids"])
    existing_ids: list[str] = []
    locked_paths: list[str] = []
    for asset_id in ordered_asset_ids:
        locked = state.get("locked_assets", {}).get(asset_id)
        path = os.path.join(state["output_dir"], str((locked or {}).get("path", "")))
        if isinstance(locked, dict) and os.path.isfile(path):
            existing_ids.append(asset_id)
            locked_paths.append(path)
    temporal_context = [
        item for item in (temporal_context or [])
        if isinstance(item, dict) and os.path.isfile(str(item.get("path", "")))
    ]
    temporal_paths = [str(item["path"]) for item in temporal_context]
    capabilities = state.get("provider_capabilities", {})
    metadata = {
        "previous_shot_reference_path": os.path.relpath(
            previous_shot_path, state["output_dir"]
        ),
        "previous_shot_reference_role": "primary_edit_reference",
        "reference_strategy": strategy,
        "primary_reference_role": strategy["primary_role"],
        "primary_reference_asset_ids": strategy["primary_asset_ids"],
        "temporal_context_shot_ids": [str(item.get("shot_id", "")) for item in temporal_context],
        "temporal_context_paths": [
            os.path.relpath(path, state["output_dir"]) for path in temporal_paths
        ],
    }
    if strategy.get("exclude_failed_shot_reference"):
        metadata["previous_shot_reference_role"] = "excluded_nonconverging_source"
    elif strategy["primary_role"] != "previous_shot_edit":
        metadata["previous_shot_reference_role"] = "edit_target_reference"
    if strategy.get("canonical_only_reference"):
        primary_paths = _locked_reference_paths(state, strategy["primary_asset_ids"])
        if primary_paths:
            selected = primary_paths[0]
            metadata.update({
                "revision_reference_board": "",
                "provider_reference_mode": "canonical_locked_asset_only",
                "provider_reference_paths": [os.path.relpath(selected, state["output_dir"])],
                "excluded_image_reference_paths": [
                    os.path.relpath(path, state["output_dir"])
                    for path in dict.fromkeys([previous_shot_path, *temporal_paths])
                ],
            })
            return [selected], metadata
    if strategy.get("locked_assets_only_reference"):
        excluded_paths = list(dict.fromkeys([previous_shot_path, *temporal_paths]))
        if capabilities.get("reference_mode") == "multi":
            maximum = max(1, int(capabilities.get("max_references", 8)))
            selected = list(dict.fromkeys(locked_paths))[:maximum]
            metadata.update({
                "revision_reference_board": "",
                "provider_reference_mode": (
                    "scale_evidence_priority_locked_assets_multi"
                    if strategy.get("scale_evidence_priority_reference")
                    else "post_reset_locked_assets_only_multi"
                ),
                "provider_reference_paths": [
                    os.path.relpath(path, state["output_dir"]) for path in selected
                ],
                "excluded_image_reference_paths": [
                    os.path.relpath(path, state["output_dir"]) for path in excluded_paths
                ],
                "failed_shot_reference_included": False,
                "temporal_image_references_excluded": True,
            })
            return selected, metadata
    if capabilities.get("reference_mode") == "multi":
        maximum = max(1, int(capabilities.get("max_references", 8)))
        if strategy.get("constraint_isolation"):
            primary_paths = _locked_reference_paths(state, strategy["primary_asset_ids"])
            ordered_paths = [previous_shot_path, *primary_paths]
            metadata.update({
                "provider_reference_mode": "constraint_isolated_edit_multi",
                "provider_reference_paths": [
                    os.path.relpath(path, state["output_dir"])
                    for path in list(dict.fromkeys(ordered_paths))[:maximum]
                ],
                "temporal_image_references_excluded": True,
                "failed_shot_reference_included": True,
            })
        elif strategy["primary_role"] == "previous_shot_edit":
            ordered_paths = [previous_shot_path, *temporal_paths, *locked_paths]
        else:
            primary_paths = _locked_reference_paths(state, strategy["primary_asset_ids"])
            ordered_paths = [
                *primary_paths,
                *([] if strategy.get("exclude_failed_shot_reference") else [previous_shot_path]),
                *temporal_paths, *locked_paths,
            ]
            metadata["previous_shot_reference_role"] = (
                "excluded_nonconverging_source"
                if strategy.get("exclude_failed_shot_reference")
                else "edit_target_reference"
            )
        return list(dict.fromkeys(ordered_paths))[:maximum], metadata
    board = _compose_revision_reference_board(
        state, previous_shot_path, locked_paths, board_id, existing_ids, temporal_context,
        strategy,
    )
    board_relative = os.path.relpath(board, state["output_dir"])
    metadata["revision_reference_board"] = board_relative
    if strategy.get("constraint_isolation"):
        metadata.update({
            "provider_reference_mode": "constraint_isolated_edit_board",
            "provider_reference_paths": [board_relative],
            "temporal_image_references_excluded": True,
            "failed_shot_reference_included": True,
        })
    if strategy.get("locked_assets_only_reference"):
        metadata.update({
            "provider_reference_mode": (
                "scale_evidence_priority_locked_assets_board"
                if strategy.get("scale_evidence_priority_reference")
                else "post_reset_locked_assets_only_board"
            ),
            "provider_reference_paths": [board_relative],
            "excluded_image_reference_paths": [
                os.path.relpath(path, state["output_dir"])
                for path in dict.fromkeys([previous_shot_path, *temporal_paths])
            ],
            "failed_shot_reference_included": False,
            "temporal_image_references_excluded": True,
        })
    return [board], metadata


def _adjacent_shot_references(
    state: VisualAgentState,
    group_id: str,
    shot_id: str,
) -> list[dict]:
    sequence: list[tuple[str, str]] = []
    for group in state.get("scene_groups", []):
        candidate_group_id = str(group.get("group_id", ""))
        for shot in group.get("shots", []):
            sequence.append((candidate_group_id, str(shot.get("shot_id", ""))))
    try:
        target_index = sequence.index((group_id, shot_id))
    except ValueError:
        return []
    context: list[dict] = []
    for neighbor_index in (target_index - 1, target_index + 1):
        if not 0 <= neighbor_index < len(sequence):
            continue
        neighbor_group_id, neighbor_shot_id = sequence[neighbor_index]
        relative = str(
            state.get("group_states", {}).get(neighbor_group_id, {})
            .get("generated", {}).get(neighbor_shot_id, "")
        )
        path = os.path.join(state["output_dir"], relative)
        if relative and os.path.isfile(path):
            context.append({
                "group_id": neighbor_group_id,
                "shot_id": neighbor_shot_id,
                "path": path,
            })
    return context


def _build_scene_groups(state: VisualAgentState) -> list[dict]:
    storyboard = state["storyboard"]
    inventory_scenes = {item["scene_id"]: item for item in state["inventory"]["scenes"]}
    character_assets: dict[str, list[str]] = {}
    for asset in state["foundation_assets"]:
        if asset["asset_type"].startswith("character_"):
            cid = str(asset.get("spec", {}).get("character_id", ""))
            character_assets.setdefault(cid, []).append(asset["asset_id"])
    prop_assets = {
        str(item.get("spec", {}).get("prop_id", "")): item["asset_id"]
        for item in state["foundation_assets"] if item["asset_type"] == "key_prop"
    }
    groups: list[dict] = []
    for scene_index, scene in enumerate(storyboard.get("scenes", []), 1):
        scene_id = str(scene.get("scene_id") or scene_index)
        inventory = inventory_scenes.get(scene_id, {})
        shot_items: list[dict] = []
        group_references: list[str] = []
        location_asset_id = inventory.get("location_asset_id")
        scene_prop_ids = {
            str(item.get("prop_id")) for item in scene.get("key_props", [])
            if isinstance(item, dict) and item.get("prop_id")
        } if isinstance(scene.get("key_props"), list) else set()
        for shot in scene.get("shots", []):
            visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
            visible = [str(item) for item in visual.get("visible_character_ids", []) if str(item)]
            references = ["style_001"]
            if location_asset_id:
                references.append(location_asset_id)
            for character_id in visible:
                references.extend(character_assets.get(character_id, []))
            has_explicit_shot_props = isinstance(visual.get("key_props"), list)
            shot_prop_ids = {
                str(item.get("prop_id")) for item in visual.get("key_props", [])
                if isinstance(item, dict) and item.get("prop_id")
            } if has_explicit_shot_props else set()
            effective_prop_ids = shot_prop_ids if has_explicit_shot_props else scene_prop_ids
            for prop_id in sorted(effective_prop_ids):
                if prop_id in prop_assets:
                    references.append(prop_assets[prop_id])
            references = list(dict.fromkeys(references))
            group_references.extend(references)
            shot_items.append({
                "shot_id": str(shot.get("shot_id", "")),
                "storyboard_path": f"$.scenes[{scene_index - 1}].shots[{len(shot_items)}]",
                "visible_character_ids": visible,
                "visible_prop_ids": sorted(effective_prop_ids),
                "reference_asset_ids": references,
                "prompt": get_prompt(shot, "image_en") or get_prompt(shot, "image_cn"),
                "description": get_visual(shot, "description"),
            })
        groups.append({
            "group_id": f"scene_{safe_filename(scene_id)}", "scene_ids": [scene_id],
            "shot_ids": [item["shot_id"] for item in shot_items], "shots": shot_items,
            "reference_asset_ids": list(dict.fromkeys(group_references)),
            "location": inventory.get("location", ""), "time_state": inventory.get("time_state", ""),
        })
    return groups


def _shot_prompt(state: VisualAgentState, group: dict, shot: dict,
                 revision_instructions: list[str] | None = None,
                 revision_reference_policy: dict | None = None) -> str:
    policy = dict(revision_reference_policy or {})
    locked = [
        {"asset_id": asset_id, "version": state["locked_assets"][asset_id]["version"]}
        for asset_id in shot.get("reference_asset_ids", group["reference_asset_ids"])
        if asset_id in state.get("locked_assets", {})
    ]
    scale_contracts = []
    foundation_by_id = {
        str(asset.get("asset_id", "")): asset
        for asset in state.get("foundation_assets", []) if isinstance(asset, dict)
    }
    compiled_constraints = compile_shot_constraints(shot, foundation_by_id)
    conflicts = detect_constraint_conflicts(compiled_constraints)
    if conflicts:
        raise ValueError(
            "visual constraint preflight failed: "
            + json.dumps(conflicts, ensure_ascii=False, separators=(",", ":"))
        )
    state.setdefault("compiled_constraints_by_shot", {})[
        str(shot.get("shot_id", ""))
    ] = [item.to_dict() for item in compiled_constraints]
    for asset_id in shot.get("reference_asset_ids", group["reference_asset_ids"]):
        asset = foundation_by_id.get(str(asset_id))
        if not asset or asset.get("asset_type") != "key_prop":
            continue
        scale_contract = _asset_scale_contract(asset)
        if scale_contract.get("required"):
            scale_contracts.append({
                "asset_id": str(asset_id),
                "source_cues": scale_contract.get("source_cues", []),
                "shot_policy": scale_contract.get("shot_policy", ""),
            })
    parts = [
        prompt_constraint_envelope(compiled_constraints),
        "NARRATIVE PROMPT: " + str(shot.get("prompt") or shot.get("description", "")),
        f"required frame content: {shot.get('description', '')}",
        f"visible character ids only: {shot.get('visible_character_ids', [])}",
        f"visible prop ids only: {shot.get('visible_prop_ids', [])}",
        f"locked reference assets: {locked}",
        "preserve identity, costume, spatial layout, key props and project style",
        "canonical key-prop references define identity, silhouette and topology; their attached scale contract "
        "defines real-world size. Render exactly "
        "one visible instance for each bound prop unless the storyboard explicitly requires multiple instances; "
        "apply shot-specific color, glow, damage or progressive state to that same object and never interpret "
        "reference panels or state examples as additional objects",
        "APPEARANCE-ONLY STATE CONTRACT: shot-specific color, glow, emission, illumination, reflection, wear, "
        "damage and progressive visual state change appearance only by default. They must not change the locked "
        "silhouette, topology, solid-versus-open construction, thickness, volume, material class or support "
        "surface unless the storyboard explicitly requires a physical deformation. Light, reflection and emitted "
        "energy are optical effects, never added object geometry, holes, shells, layers, bumps, bases or thickness",
        "SINGLE CONTINUOUS CAMERA FRAME: output one scene at one moment. Never output a triptych, collage, "
        "contact sheet, stacked panels, split screen, inset, storyboard sequence, before/after comparison or "
        "multiple time-state panels",
        "vertical comic-drama readability, executable composition, no watermark",
        "document rule: when exact wording is not required, depict separate handwritten marks or "
        "character-like writing units rather than a solid bar; when a visual change depends on writing "
        "structure, preserve separate units and show the change through progressive opacity or coverage; "
        "when exact readable wording is required, reserve a stable clean overlay area for deterministic "
        "post-production text instead of inventing text; no watermark or unrelated logos",
    ]
    isolation_task = policy.get("constraint_isolation_task", {})
    if policy.get("constraint_isolation") and isinstance(isolation_task, dict):
        fallback_plan = compile_fallback_constraints(
            compiled_constraints, trigger="provider_escalation"
        )
        state.setdefault("fallback_constraints_by_shot", {})[
            str(shot.get("shot_id", ""))
        ] = fallback_plan
        parts.insert(1, isolation_prompt_envelope(isolation_task))
        parts.insert(2, fallback_constraint_envelope(
            compiled_constraints, trigger="provider_escalation"
        ))
    if scale_contracts:
        parts.extend([
            "SOURCE-DECLARED PROP SCALE CONTRACTS: "
            + json.dumps(scale_contracts, ensure_ascii=False),
            "The locked prop image is a geometry close-up, not evidence of frame occupancy. Preserve the "
            "declared real-world scale against visible hands, pockets, furniture and other scene comparators. "
            "Do not enlarge a small prop to make it easier to see, even in a close shot",
        ])
    if revision_instructions:
        primary_role = str(policy.get("primary_role", "previous_shot_edit"))
        if primary_role == "constraint_isolated_edit":
            edit_instruction = (
                "CONSTRAINT-ISOLATED TARGETED EDIT: use the current shot as the primary edit target. "
                "The focused locked asset is evidence only for the one active correction. Preserve every "
                "unaffected character, object, count, location, camera relationship and visual state. Do not "
                "redesign the full frame and do not import the reference asset as another visible instance"
            )
        elif primary_role == "scale_evidence_priority_reconstruction":
            edit_instruction = (
                "SCALE-EVIDENCE-PRIORITY RECONSTRUCTION: reconstruct the shot from storyboard facts and the "
                "supplied locked Foundation assets. The dominant full-comparator reference panel is the "
                "authoritative scale witness: preserve the target prop's relative size to that complete "
                "comparator, not the panel's size on the reference canvas. Treat the remaining panels only as "
                "identity, location and style anchors. Keep the scale-bound prop a subordinate scene detail; "
                "make it legible through local contrast, focus, reflection or restrained emission, never by "
                "increasing its physical dimensions, thickness or foreground prominence"
            )
        elif primary_role == "post_reset_locked_assets_transfer":
            edit_instruction = (
                "POST-FOUNDATION-RESET LOCKED-ASSETS-ONLY TRANSFER: reconstruct the shot from storyboard facts "
                "and the supplied locked Foundation assets. Treat those assets as the only image references and "
                "transfer their identity, geometry, scale, character, location and project style without copying "
                "any failed or neighboring shot pixels"
            )
        elif primary_role == "clean_regeneration":
            if policy.get("canonical_only_reference"):
                edit_instruction = (
                    "CANONICAL-ONLY CLEAN REGENERATION: reconstruct the shot from storyboard facts with the sole "
                    "supplied locked asset as the authoritative geometry and scale reference. The failed prior "
                    "shot and adjacent shot images are deliberately excluded; do not reproduce their scale, "
                    "shape, framing or other failed pixels. Preserve continuity from the shot description, "
                    "declared visible entities and locked asset facts"
                )
            else:
                edit_instruction = (
                    "CLEAN REGENERATION: reconstruct the shot from the locked foundation and temporal references. "
                    "The failed prior shot is deliberately excluded; do not reproduce its artifacts, duplicated "
                    "effects, impossible geometry or disconnected sources. Preserve storyboard facts and continuity "
                    "through the locked references rather than copying failed pixels"
                )
        elif primary_role == "previous_shot_edit":
            edit_instruction = (
                "EDIT the primary previous-shot reference; preserve all unaffected regions, identity, "
                "costume, location, props, framing and style, and modify only the listed corrections"
            )
        else:
            edit_instruction = (
                "Use the previous-shot panel as the composition edit target, but treat the primary locked "
                f"reference role {primary_role} as authoritative for the corrected identity, geometry or "
                "topology. Preserve every unaffected region of the edit target"
            )
        continuity_instruction = (
            "Preserve continuity from the current edit target and locked storyboard facts; adjacent shot "
            "images are deliberately excluded so they cannot compete with the active constraint"
            if policy.get("constraint_isolation") else
            "Preserve adjacent-shot continuity from storyboard descriptions and locked facts; no failed or "
            "adjacent shot image is an input to this locked-reference regeneration"
            if policy.get("canonical_only_reference")
            or policy.get("scale_evidence_priority_reference")
            or policy.get("exclude_temporal_image_references") else
            "use supporting temporal-context panels only to preserve adjacent-shot character, prop, "
            "location and action-state continuity; do not copy their framing over the primary edit"
        )
        parts.extend([
            edit_instruction,
            continuity_instruction,
            "translate each spatial correction into an explicit visible relationship with a source anchor, "
            "target anchor, screen-space direction, contrast against normal elements, and a forbidden "
            "interpretation; do not fall back to the conventional interpretation named as forbidden",
            "for any temporal or progressive transformation, show one coherent state or a continuous material "
            "gradient on the same object inside the single scene; never create separate before, transition and "
            "after panels or stacked time zones",
            "targeted corrections: " + "; ".join(revision_instructions),
        ])
    return ". ".join(part for part in parts if part)


def _normalize_visual_issues(raw: dict | None, state: VisualAgentState,
                             group: dict, generated: dict[str, str]) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        return []
    valid_shots = {item["shot_id"]: item for item in group["shots"]}
    valid_assets = set(group["reference_asset_ids"])
    allowed_contracts_by_shot: dict[str, set[str]] = {}
    group_state = state.get("group_states", {}).get(str(group.get("group_id", "")), {})
    for attempt in group_state.get("revision_attempt_history", []):
        if not isinstance(attempt, dict) or not str(attempt.get("artifact_path", "")):
            continue
        contract_id = str(attempt.get("correction_contract_id", ""))
        shot_key = str(attempt.get("shot_id", ""))
        if contract_id.startswith("constraint_") and shot_key:
            allowed_contracts_by_shot.setdefault(shot_key, set()).add(contract_id)
    issues: list[dict] = []
    used_ids: dict[str, int] = {}
    for item in raw["issues"]:
        if not isinstance(item, dict):
            continue
        shot_id = str(item.get("shot_id", ""))
        shot = valid_shots.get(shot_id)
        image_rel = generated.get(shot_id, "")
        references = item.get("reference_asset_ids", [])
        references = [str(value) for value in references if str(value) in valid_assets] if isinstance(references, list) else []
        focus_assets = item.get("focus_asset_ids", [])
        focus_assets = [
            str(value) for value in focus_assets if str(value) in valid_assets
        ] if isinstance(focus_assets, list) else []
        correction_target = str(item.get("correction_target", "other")).strip().lower()
        if correction_target not in {
            "prop_geometry", "location_structure", "character_identity",
            "shot_composition", "temporal_state", "effect_alignment", "artifact", "other",
        }:
            correction_target = "other"
        requested_path = str(item.get("storyboard_path", ""))
        evidence_valid = bool(
            shot and image_rel and os.path.isfile(os.path.join(state["output_dir"], image_rel))
            and requested_path == shot["storyboard_path"]
            and (references or str(item.get("category", "")) in {"file", "schema", "artifact"})
        )
        blocking = bool(item.get("blocking")) and evidence_valid
        severity = str(item.get("severity", "advisory"))
        if not evidence_valid:
            severity = "advisory"
        category = str(item.get("category", "visual_quality"))
        issue_fingerprint = content_fingerprint(
            group["group_id"], shot_id, category,
            str(item.get("problem", "")), str(item.get("instruction", "")),
            requested_path, references, focus_assets, correction_target,
            bool(item.get("blocking")),
            length=12,
        )
        canonical_base = "visual_{}_{}_{}_{}".format(
            safe_filename(group["group_id"]), safe_filename(shot_id or "unknown"),
            safe_filename(category or "visual_quality"), issue_fingerprint,
        )
        occurrence = used_ids.get(canonical_base, 0) + 1
        used_ids[canonical_base] = occurrence
        canonical_id = canonical_base if occurrence == 1 else f"{canonical_base}_{occurrence:02d}"
        requested_contract_id = str(item.get("correction_contract_id", ""))
        if requested_contract_id not in allowed_contracts_by_shot.get(shot_id, set()):
            requested_contract_id = ""
        normalized_issue = {
            "issue_id": canonical_id,
            "provider_issue_id": str(item.get("issue_id", "")),
            "group_id": group["group_id"], "shot_id": shot_id,
            "category": category,
            "severity": severity, "blocking": blocking,
            "problem": str(item.get("problem", "")),
            "instruction": str(item.get("instruction", "")),
            "storyboard_path": requested_path,
            "reference_asset_ids": references,
            "focus_asset_ids": focus_assets,
            "correction_target": correction_target,
            "correction_contract_id": requested_contract_id,
            "image_path": image_rel, "evidence_valid": evidence_valid,
        }
        if not normalized_issue["correction_contract_id"]:
            normalized_issue["correction_contract_id"] = _correction_contract(
                str(group.get("group_id", "")), shot_id, [normalized_issue]
            )["correction_contract_id"]
        verdict = normalize_issue_verdict({
            **item,
            **normalized_issue,
            "constraint_id": normalized_issue["correction_contract_id"],
        })
        verdict = calibrate_verdict(
            verdict, state.get("vision_confidence_calibration")
        )
        normalized_issue["constraint_verdict"] = verdict
        normalized_issue["blocking"] = bool(
            normalized_issue["blocking"]
            and blocking_verdict_is_actionable(verdict)
        )
        if not normalized_issue["blocking"] and verdict.get("verdict") == "unverifiable":
            normalized_issue["severity"] = "advisory"
        issues.append(normalized_issue)
    return issues


def _generated_image_fingerprints(
    state: VisualAgentState, generated: dict[str, str]
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for shot_id, relative in generated.items():
        path = os.path.join(state["output_dir"], str(relative))
        if relative and os.path.isfile(path):
            fingerprints[str(shot_id)] = _file_fingerprint(path)
    return fingerprints


def _issue_review_key(issue: dict) -> tuple[str, str]:
    return (
        str(issue.get("shot_id", "")),
        str(issue.get("category", "visual_quality")).strip().lower(),
    )


def _stabilize_new_blockers_on_unchanged_images(
    state: VisualAgentState,
    group: dict,
    generated: dict[str, str],
    semantic: list[dict],
    group_state: dict,
    vision_provider: VisionProvider,
) -> list[dict]:
    """Confirm newly discovered blockers when the reviewed pixels did not change."""
    history = [
        item for item in group_state.get("review_history", [])
        if isinstance(item, dict) and item.get("vision_available") is True
    ]
    if not history:
        return semantic
    previous = history[-1]
    previous_fingerprints = previous.get("image_fingerprints", {})
    current_fingerprints = _generated_image_fingerprints(state, generated)
    previous_blocking_keys = {
        _issue_review_key(issue)
        for issue in previous.get("issues", [])
        if isinstance(issue, dict) and issue.get("blocking") is True
    }
    candidates = [
        issue for issue in semantic
        if issue.get("blocking") is True
        and previous_fingerprints.get(str(issue.get("shot_id", "")))
        == current_fingerprints.get(str(issue.get("shot_id", "")))
        and _issue_review_key(issue) not in previous_blocking_keys
    ]
    if not candidates:
        return semantic

    shot_ids = list(dict.fromkeys(str(issue.get("shot_id", "")) for issue in candidates))
    candidate_paths = [
        os.path.join(state["output_dir"], generated.get(shot_id, ""))
        for shot_id in shot_ids if generated.get(shot_id)
    ]
    focus_ids = list(dict.fromkeys(
        str(asset_id)
        for issue in candidates
        for asset_id in [
            *issue.get("focus_asset_ids", []), *issue.get("reference_asset_ids", []),
        ]
    ))
    reference_paths = _locked_reference_paths(state, focus_ids)
    confirmation = _record_vision_result(
        state,
        "confirm_new_blockers_unchanged_images",
        vision_provider(
            "confirm_new_blockers_unchanged_images",
            candidate_paths + reference_paths,
            {
                "group": group,
                "generated_paths": generated,
                "candidate_issues": candidates,
                "image_fingerprints": {
                    shot_id: current_fingerprints.get(shot_id, "") for shot_id in shot_ids
                },
                "instruction": (
                    "The target pixels are unchanged since the previous review. Independently confirm only "
                    "blockers visibly supported by the supplied image and authoritative locked references. "
                    "Return an empty issues list when the new blocker is not reproducible."
                ),
            },
        ),
    )
    if confirmation is None or not isinstance(confirmation.get("issues"), list):
        for issue in candidates:
            issue["review_confirmation_status"] = "unavailable"
        return semantic
    confirmed = _normalize_visual_issues(confirmation, state, group, generated)
    confirmed_keys = {
        _issue_review_key(issue) for issue in confirmed if issue.get("blocking") is True
    }
    candidate_ids = {id(issue) for issue in candidates}
    stabilized: list[dict] = []
    for issue in semantic:
        if id(issue) not in candidate_ids:
            stabilized.append(issue)
            continue
        if _issue_review_key(issue) in confirmed_keys:
            issue["review_confirmation_status"] = "confirmed"
        else:
            issue["review_confirmation_status"] = "not_confirmed"
            issue["blocking"] = False
            issue["severity"] = "advisory"
        stabilized.append(issue)
    return stabilized


def _append_group_review_snapshot(
    state: VisualAgentState,
    group_state: dict,
    generated: dict[str, str],
    issues: list[dict],
    vision_available: bool,
) -> None:
    history = group_state.setdefault("review_history", [])
    history.append({
        "reviewed_at": _now(),
        "image_fingerprints": _generated_image_fingerprints(state, generated),
        "issues": copy.deepcopy(issues),
        "vision_available": vision_available,
    })
    if len(history) > 12:
        del history[:-12]


def _correction_contract(group_id: str, shot_id: str, issues: list[dict]) -> dict:
    """Build a stable, structured repair lineage without using story keywords."""
    valid = [item for item in issues if isinstance(item, dict)]
    focus_asset_ids = sorted({
        str(asset_id) for item in valid for asset_id in item.get("focus_asset_ids", [])
        if str(asset_id)
    })
    reference_asset_ids = sorted({
        str(asset_id) for item in valid for asset_id in item.get("reference_asset_ids", [])
        if str(asset_id)
    })
    contract = {
        "group_id": str(group_id),
        "shot_id": str(shot_id),
        "storyboard_paths": sorted({
            str(item.get("storyboard_path", "")) for item in valid
            if str(item.get("storyboard_path", ""))
        }),
        "issue_categories": sorted({
            str(item.get("category", "visual_quality")).strip().lower() for item in valid
        }),
        "authoritative_asset_ids": focus_asset_ids or reference_asset_ids,
        "focus_asset_ids": focus_asset_ids,
        "reference_asset_ids": reference_asset_ids,
        "observed_correction_targets": sorted({
            str(item.get("correction_target", "other")).strip().lower() for item in valid
        }),
        "issue_ids": sorted({
            str(item.get("issue_id", "")) for item in valid if str(item.get("issue_id", ""))
        }),
        "constraints": [{
            "issue_id": str(item.get("issue_id", "")),
            "correction_target": str(item.get("correction_target", "other")).strip().lower(),
            "problem": str(item.get("problem", "")),
            "instruction": str(item.get("instruction", "")),
            "focus_asset_ids": list(map(str, item.get("focus_asset_ids", []))),
            "constraint_verdict": copy.deepcopy(item.get("constraint_verdict", {})),
        } for item in sorted(valid, key=lambda value: str(value.get("issue_id", "")))],
    }
    declared_ids = {
        str(item.get("correction_contract_id", "")) for item in valid
        if str(item.get("correction_contract_id", "")).startswith("constraint_")
    }
    contract["correction_contract_id"] = (
        next(iter(declared_ids)) if len(declared_ids) == 1 else
        "constraint_" + content_fingerprint(
            contract["group_id"], contract["shot_id"], contract["storyboard_paths"],
            contract["issue_categories"], contract["authoritative_asset_ids"],
            length=20,
        )
    )
    return contract


def _append_revision_attempt(group_state: dict, attempt: dict) -> None:
    history = group_state.setdefault("revision_attempt_history", [])
    stable_id = str(attempt.get("ledger_job_id") or attempt.get("attempt_id", ""))
    if stable_id:
        for index, existing in enumerate(history):
            existing_id = str(existing.get("ledger_job_id") or existing.get("attempt_id", ""))
            if existing_id == stable_id:
                history[index] = copy.deepcopy(attempt)
                return
    history.append(copy.deepcopy(attempt))
    if len(history) > 40:
        del history[:-40]


def _reconcile_revision_attempt_history(state: VisualAgentState, ledger: dict) -> None:
    """Make revision history a deterministic projection of paid ledger facts."""
    group_states = state.get("group_states", {})
    if not isinstance(group_states, dict):
        return
    for ledger_key, entry in ledger.get("jobs", {}).items():
        if not isinstance(entry, dict):
            continue
        logical_id = str(entry.get("logical_job_id", ""))
        if not (
            str(entry.get("operation_kind", "")) == "shot_revision"
            or logical_id.startswith("retry:")
        ):
            continue
        group_id = str(entry.get("group_id", ""))
        shot_id = str(entry.get("shot_id", ""))
        if (not group_id or not shot_id) and logical_id.startswith("retry:"):
            match = re.fullmatch(r"retry:(.+):r\d+:(.+)", logical_id)
            if match:
                group_id = group_id or match.group(1)
                shot_id = shot_id or match.group(2)
        group_state = group_states.get(group_id)
        if not isinstance(group_state, dict):
            continue
        foundation_reset = state.get("foundation_reset", {})
        if (
            isinstance(foundation_reset, dict)
            and str(foundation_reset.get("group_id", "")) == group_id
            and shot_id in set(map(str, foundation_reset.get("affected_shot_ids", [])))
            and str(entry.get("started_at", ""))
            and str(entry.get("started_at", "")) < str(foundation_reset.get("prepared_at", ""))
        ):
            continue
        existing = next((
            item for item in group_state.get("revision_attempt_history", [])
            if isinstance(item, dict)
            and str(item.get("ledger_job_id", "")) == str(ledger_key)
        ), {})
        status = str(entry.get("status", ""))
        if status == "succeeded":
            provider_outcome = "recovered" if entry.get("recovered_from_file") else "succeeded"
            artifact_path = str(
                entry.get("published_output_path") or entry.get("output_path", "")
            )
            artifact_sha = str(
                entry.get("published_file_sha256") or entry.get("file_sha256", "")
            )
        elif status == "produced":
            provider_outcome = "recovered" if entry.get("recovered_from_file") else "produced"
            artifact_path = str(entry.get("attempt_output_path", ""))
            artifact_sha = str(entry.get("file_sha256", ""))
        elif status == "failed":
            provider_outcome = "failed"
            artifact_path = ""
            artifact_sha = ""
        else:
            provider_outcome = "uncertain"
            artifact_path = ""
            artifact_sha = ""
        attempt = {
            **copy.deepcopy(existing),
            "attempt_id": str(existing.get("attempt_id", "")) or content_fingerprint(
                state.get("run_id", ""), logical_id, entry.get("grant_id", ""),
                ledger_key, length=24,
            ),
            "logical_job_id": logical_id,
            "ledger_job_id": str(entry.get("ledger_job_id", ledger_key)),
            "grant_id": str(entry.get("grant_id", "")),
            "group_id": group_id,
            "shot_id": shot_id,
            "revision_attempt_number": int(entry.get("revision_attempt_number", 0) or 0),
            "strategy": str(entry.get("revision_strategy", "")),
            "correction_contract_id": str(entry.get("correction_contract_id", "")),
            "provider_attempted": True,
            "provider_outcome": provider_outcome,
            "error": str(entry.get("error", "")),
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha,
            "recorded_at": str(entry.get("completed_at") or entry.get("started_at") or _now()),
        }
        _append_revision_attempt(group_state, attempt)


def _sync_blocked_quality_gate(state: VisualAgentState) -> None:
    group_states = state.get("group_states", {})
    blocking = [
        issue for group_state in group_states.values()
        if isinstance(group_state, dict)
        for issue in group_state.get("issues", [])
        if isinstance(issue, dict) and issue.get("blocking") is True
    ]
    blocking_ids = sorted({
        str(issue.get("issue_id", "")) for issue in blocking if str(issue.get("issue_id", ""))
    })
    expected_group_ids = {
        str(group.get("group_id", "")) for group in state.get("scene_groups", [])
        if str(group.get("group_id", ""))
    }
    reviewed_group_ids = {
        str(group_id) for group_id, group_state in group_states.items()
        if isinstance(group_state, dict) and any(
            isinstance(snapshot, dict) and snapshot.get("vision_available") is True
            for snapshot in group_state.get("review_history", [])
        )
    }
    unreviewed = sorted(expected_group_ids.difference(reviewed_group_ids))
    complete = bool(expected_group_ids) and not unreviewed
    state["quality_gate"] = {
        "mode": "vision_blocked" if reviewed_group_ids else "technical_failure",
        "quality_outcome": "blocked",
        "passed_without_override": False,
        "accepted": False,
        "automated_review_status": "completed" if complete else "partially_completed",
        "automated_review_completed": complete,
        "verification_mode": "automated_vision" if complete else "automated_vision_partial",
        "vision_calls": int(state.get("counters", {}).get("vision_calls", 0)),
        "vision_attempts": int(state.get("counters", {}).get("vision_attempts", 0)),
        "vision_failures": int(state.get("counters", {}).get("vision_failures", 0)),
        "blocking_issue_ids": blocking_ids,
        "blocking_status": "blocked",
        "observed_blocking_issue_count": len(blocking),
        "blocking_issue_count": len(blocking),
        "overridden_blocking_issue_count": 0,
        "unverified_checks": [f"scene_group_review:{group_id}" for group_id in unreviewed],
    }


def _revision_round_from_logical_id(logical_id: str) -> int:
    match = re.match(r"^retry:[^:]+:r(\d+):", str(logical_id))
    return int(match.group(1)) if match else 0


def _scene_revision_rounds(
    state: VisualAgentState, group_id: str, group_state: dict,
) -> set[int]:
    """Collect durable artifact rounds independently from the local retry budget."""
    rounds = {max(0, int(group_state.get("retry_count", 0) or 0))}
    for history_key in ("revision_attempt_history", "revision_attempt_history_archive"):
        for attempt in group_state.get(history_key, []):
            if not isinstance(attempt, dict):
                continue
            round_number = _revision_round_from_logical_id(
                str(attempt.get("logical_job_id", ""))
            )
            if round_number:
                rounds.add(round_number)
    ledger = _load_paid_ledger(state)
    for entry in ledger.get("jobs", {}).values():
        if not isinstance(entry, dict):
            continue
        logical_id = str(entry.get("logical_job_id", ""))
        if not (
            str(entry.get("group_id", "")) == str(group_id)
            or logical_id.startswith(f"retry:{group_id}:")
        ):
            continue
        round_number = _revision_round_from_logical_id(logical_id)
        if round_number:
            rounds.add(round_number)
    reset = state.get("foundation_reset", {})
    if (
        isinstance(reset, dict)
        and str(reset.get("group_id", "")) == str(group_id)
    ):
        reset_round = int(reset.get("shot_revision_round", 0) or 0)
        if reset_round:
            rounds.add(reset_round)
    return rounds


def _next_scene_revision_round(
    state: VisualAgentState, group_id: str, group_state: dict,
) -> int:
    return max(_scene_revision_rounds(state, group_id, group_state), default=0) + 1


def _repair_post_foundation_reset_revision_collision(
    state: VisualAgentState, ledger: dict,
) -> dict:
    """Restore the latest valid post-reset shot after a regressed logical round."""
    reset = state.get("foundation_reset", {})
    if not isinstance(reset, dict):
        return {}
    group_id = str(reset.get("group_id", ""))
    reset_round = int(reset.get("shot_revision_round", 0) or 0)
    prepared_at = str(reset.get("prepared_at", ""))
    affected = set(map(str, reset.get("affected_shot_ids", [])))
    group_state = state.get("group_states", {}).get(group_id)
    if not group_id or reset_round < 1 or not prepared_at or not affected or not isinstance(group_state, dict):
        return {}

    active_history = [
        item for item in group_state.get("revision_attempt_history", [])
        if isinstance(item, dict)
    ]
    def is_collision(item: dict) -> bool:
        ledger_job_id = str(item.get("ledger_job_id", ""))
        ledger_entry = ledger.get("jobs", {}).get(ledger_job_id, {})
        historical_entry = bool(
            isinstance(ledger_entry, dict)
            and str(ledger_entry.get("started_at", ""))
            and str(ledger_entry.get("started_at", "")) < prepared_at
        )
        return bool(
            str(item.get("shot_id", "")) in affected
            and 0 < _revision_round_from_logical_id(item.get("logical_job_id", "")) < reset_round
            and str(item.get("recorded_at", "")) >= prepared_at
            and item.get("provider_outcome") == "recovered"
            and item.get("provider_attempted") is False
            and historical_entry
        )

    collisions = [item for item in active_history if is_collision(item)]
    if not collisions:
        return {}

    valid_by_shot: dict[str, list[tuple[int, str, dict]]] = {}
    for item in active_history:
        shot_id = str(item.get("shot_id", ""))
        round_number = _revision_round_from_logical_id(item.get("logical_job_id", ""))
        relative = str(item.get("artifact_path", ""))
        path = os.path.join(state["output_dir"], relative) if relative else ""
        expected_sha = str(item.get("artifact_sha256", ""))
        valid_file = bool(path and os.path.isfile(path) and os.path.getsize(path) > 0)
        if valid_file and expected_sha and _file_fingerprint(path) != expected_sha:
            valid_file = False
        if shot_id in affected and round_number >= reset_round and valid_file:
            valid_by_shot.setdefault(shot_id, []).append((
                round_number, str(item.get("recorded_at", "")), item,
            ))

    repairable_shots = {
        str(item.get("shot_id", "")) for item in collisions
        if valid_by_shot.get(str(item.get("shot_id", "")))
    }
    invalidated = [
        item for item in collisions if str(item.get("shot_id", "")) in repairable_shots
    ]
    if not invalidated:
        return {}
    invalidated_ids = {
        str(item.get("ledger_job_id") or item.get("attempt_id", ""))
        for item in invalidated
    }
    repaired_at = _now()
    group_state["revision_attempt_history"] = [
        item for item in active_history
        if str(item.get("ledger_job_id") or item.get("attempt_id", ""))
        not in invalidated_ids
    ]
    group_state.setdefault("revision_attempt_history_archive", []).extend([
        {
            **copy.deepcopy(item),
            "invalidated_reason": "post_foundation_reset_revision_round_collision",
            "invalidated_at": repaired_at,
        }
        for item in invalidated
    ])
    restored: dict[str, str] = {}
    for shot_id in sorted(repairable_shots):
        latest = max(valid_by_shot[shot_id], key=lambda value: (value[0], value[1]))[2]
        relative = str(latest.get("artifact_path", ""))
        group_state.setdefault("generated", {})[shot_id] = relative
        restored[shot_id] = relative

    remaining_rounds = [
        _revision_round_from_logical_id(item.get("logical_job_id", ""))
        for item in group_state.get("revision_attempt_history", [])
        if isinstance(item, dict) and str(item.get("shot_id", "")) in affected
    ]
    local_retry_count = max(
        [round_number - reset_round + 1 for round_number in remaining_rounds
         if round_number >= reset_round]
        or [0]
    )
    group_state["retry_count"] = local_retry_count
    reset["shot_retry_count"] = local_retry_count
    current_fingerprints = _generated_image_fingerprints(
        state, group_state.get("generated", {})
    )
    matching_reviews = [
        snapshot for snapshot in group_state.get("review_history", [])
        if isinstance(snapshot, dict)
        and isinstance(snapshot.get("image_fingerprints"), dict)
        and snapshot.get("image_fingerprints") == current_fingerprints
    ]
    restored_review = max(
        matching_reviews, key=lambda item: str(item.get("reviewed_at", "")), default={}
    )
    resumed_grant_id = ""
    restored_approval_pointer = False
    if restored_review:
        group_state["issues"] = copy.deepcopy(restored_review.get("issues", []))
        group_state["vision_available"] = restored_review.get("vision_available")
        state["issues"] = [
            issue for candidate in state.get("group_states", {}).values()
            if isinstance(candidate, dict)
            for issue in candidate.get("issues", []) if isinstance(issue, dict)
        ]
        grant_id = str(group_state.get("grant_id", ""))
        grant = ledger.get("grants", {}).get(grant_id, {})
        remaining_calls = (
            int(grant.get("maximum_paid_calls", 0) or 0)
            - int(grant.get("used_calls", 0) or 0)
            if isinstance(grant, dict) else 0
        )
        if group_state.get("approved") is True and remaining_calls >= len(repairable_shots):
            resumed_grant_id = grant_id
            group_state["status"] = "approved"
            group_state["pending_paid_operation"] = "retry"
            state["status"] = "running"
            state["stage"] = "group_retry"
            state["stop_reason"] = ""
            state["pending_approval"] = {}
            state["action"] = ""
            state["action_args"] = {}
            state["no_progress"] = 0
            _sync_blocked_quality_gate(state)
            request_path = os.path.join(
                state["output_dir"], "approvals", state["run_id"],
                f"{safe_filename(grant_id)}.request.json",
            )
            request = read_json(request_path)
            if (
                isinstance(request, dict)
                and str(request.get("request_id", "")) == grant_id
                and str(request.get("stage", "")) == str(grant.get("stage", ""))
                and str(request.get("state_fingerprint", ""))
                == str(grant.get("state_fingerprint", ""))
            ):
                atomic_write_json(
                    os.path.join(state["output_dir"], "approvals", "current.json"),
                    request,
                )
                restored_approval_pointer = True
    return {
        "invalidated_attempt_count": len(invalidated),
        "collision_ledger_job_ids": sorted(filter(None, invalidated_ids)),
        "restored_shot_paths": restored,
        "restored_local_retry_count": local_retry_count,
        "restored_reviewed_at": str(restored_review.get("reviewed_at", "")),
        "resumed_unused_grant_id": resumed_grant_id,
        "restored_approval_pointer": restored_approval_pointer,
    }


def _active_failed_revision_jobs(state: VisualAgentState, group_id: str) -> list[dict]:
    ledger = _load_paid_ledger(state)
    failed = [
        copy.deepcopy(item) for item in ledger.get("jobs", {}).values()
        if isinstance(item, dict)
        and item.get("status") == "failed"
        and _is_actionable_paid_entry(item)
        and str(item.get("group_id", "")) == str(group_id)
        and (
            str(item.get("operation_kind", "")) == "shot_revision"
            or str(item.get("logical_job_id", "")).startswith(f"retry:{group_id}:")
        )
    ]
    latest_round = max(
        (_revision_round_from_logical_id(item.get("logical_job_id", "")) for item in failed),
        default=0,
    )
    if latest_round:
        failed = [
            item for item in failed
            if _revision_round_from_logical_id(item.get("logical_job_id", "")) == latest_round
        ]
    return sorted(failed, key=lambda item: str(item.get("shot_id", "")))


def _mark_partial_revision_review_stale(
    state: VisualAgentState, group_state: dict, revised_shot_ids: list[str],
) -> None:
    revised = sorted({
        *map(str, group_state.get("issues_stale_after_revision_shot_ids", [])),
        *map(str, revised_shot_ids),
    })
    if not revised:
        _sync_blocked_quality_gate(state)
        return
    _sync_blocked_quality_gate(state)
    group_state["issues_stale"] = True
    group_state["issues_stale_after_revision_shot_ids"] = revised
    quality_gate = state.setdefault("quality_gate", {})
    quality_gate["last_observed_blocking_issue_count"] = quality_gate.get(
        "observed_blocking_issue_count", quality_gate.get("blocking_issue_count")
    )
    quality_gate["blocking_issue_count"] = None
    quality_gate["blocking_status"] = "stale_after_partial_revision"
    quality_gate["automated_review_status"] = "stale_after_partial_revision"
    quality_gate["automated_review_completed"] = False
    quality_gate["stale_after_revision_shot_ids"] = revised
    quality_gate["unverified_checks"] = [
        f"scene_group_review:{group_state.get('group_id', '') or 'current'}:{shot_id}"
        for shot_id in revised
    ]


def _prepare_technical_retry_transition(
    state: VisualAgentState, trigger: str,
) -> dict | None:
    group = _current_group(state)
    if not group:
        return None
    group_id = str(group.get("group_id", ""))
    group_state = state.setdefault("group_states", {}).setdefault(group_id, {})
    failed = _active_failed_revision_jobs(state, group_id)
    if not failed:
        return None
    retry_round = max(
        (_revision_round_from_logical_id(item.get("logical_job_id", "")) for item in failed),
        default=int(group_state.get("retry_count", 0) or 0),
    )
    failed_shot_ids = sorted({
        str(item.get("shot_id", "")) for item in failed if str(item.get("shot_id", ""))
    })
    ledger = _load_paid_ledger(state)
    failed_grant_ids = {str(item.get("grant_id", "")) for item in failed}
    succeeded_unreviewed = sorted({
        str(item.get("shot_id", ""))
        for item in ledger.get("jobs", {}).values()
        if isinstance(item, dict)
        and item.get("status") == "succeeded"
        and str(item.get("grant_id", "")) in failed_grant_ids
        and str(item.get("group_id", "")) == group_id
        and _revision_round_from_logical_id(item.get("logical_job_id", "")) == retry_round
        and str(item.get("shot_id", "")) not in failed_shot_ids
    })
    group_state["group_id"] = group_id
    group_state["approved"] = False
    group_state["status"] = "technical_retry_pending"
    group_state["pending_paid_operation"] = "technical_retry"
    group_state["technical_retry"] = {
        "revision_round": retry_round,
        "shot_ids": failed_shot_ids,
        "failed_ledger_job_ids": [str(item.get("ledger_job_id", "")) for item in failed],
        "failed_logical_job_ids": [str(item.get("logical_job_id", "")) for item in failed],
        "failed_jobs_by_shot": {
            str(item.get("shot_id", "")): {
                "logical_job_id": str(item.get("logical_job_id", "")),
                "revision_attempt_number": int(item.get("revision_attempt_number", 0) or 0),
                "revision_strategy": str(item.get("revision_strategy", "")),
                "correction_contract_id": str(item.get("correction_contract_id", "")),
                "correction_contract": copy.deepcopy(
                    item.get("finalization_payload", {}).get("correction_contract", {})
                    if isinstance(item.get("finalization_payload"), dict) else {}
                ),
                "constraint_isolation_task": copy.deepcopy(
                    item.get("finalization_payload", {}).get("constraint_isolation_task", {})
                    if isinstance(item.get("finalization_payload"), dict) else {}
                ),
            }
            for item in failed if str(item.get("shot_id", ""))
        },
        "succeeded_unreviewed_shot_ids": succeeded_unreviewed,
        "maximum_paid_calls": len(failed_shot_ids),
        "trigger": trigger,
        "prepared_at": _now(),
    }
    state["status"] = "running"
    state["stop_reason"] = ""
    state["stage"] = "group_approval"
    state["pending_approval"] = {}
    state["action"] = ""
    state["action_args"] = {}
    state["no_progress"] = 0
    state["repair_plan"] = {
        "schema_version": "1.0",
        "status": "technical_retry_approval_required",
        "source_run_id": state.get("run_id", ""),
        "created_at": _now(),
        "requires_new_paid_grant": True,
        "maximum_paid_calls": len(failed_shot_ids),
        "estimated_shot_repair_calls_after_new_strategy": len(failed_shot_ids),
        "group_ids": [group_id],
        "shot_ids": failed_shot_ids,
        "groups": [{
            "group_id": group_id,
            "shot_ids": failed_shot_ids,
            "maximum_paid_calls": len(failed_shot_ids),
            "issues": copy.deepcopy(group_state.get("issues", [])),
            "technical_failures": failed,
        }],
        "convergence": {
            "status": "technical_retry_pending",
            "reason": "provider attempt failed before a reviewable artifact was published",
        },
        "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
    }
    _mark_partial_revision_review_stale(state, group_state, succeeded_unreviewed)
    transition = {
        "scope": group_id,
        "operation": "technical_retry",
        "revision_round": retry_round,
        "failed_shot_ids": failed_shot_ids,
        "succeeded_unreviewed_shot_ids": succeeded_unreviewed,
        "missing_paid_calls": len(failed_shot_ids),
        "trigger": trigger,
    }
    _trace(state, "technical_failure_retry_transition", transition)
    return transition


def _latest_shot_revision_role(
    state: VisualAgentState, group_state: dict, shot_id: str
) -> str:
    relative = str(group_state.get("generated", {}).get(shot_id, ""))
    if not relative:
        return ""
    metadata = read_json(os.path.join(state["output_dir"], relative) + ".manju.json")
    if not isinstance(metadata, dict):
        return ""
    production = metadata.get("production", {})
    if not isinstance(production, dict):
        return ""
    return str(production.get("primary_reference_role", ""))


def _next_contract_attempt_number(group_state: dict, shot_id: str, contract_id: str) -> int:
    completed = {
        str(item.get("logical_job_id") or item.get("attempt_id", ""))
        for item in group_state.get("revision_attempt_history", [])
        if isinstance(item, dict)
        and str(item.get("shot_id", "")) == str(shot_id)
        and str(item.get("correction_contract_id", "")) == str(contract_id)
        and item.get("provider_outcome") in {"succeeded", "recovered"}
        and str(item.get("logical_job_id") or item.get("attempt_id", ""))
    }
    return len(completed) + 1


def _scale_reference_reset_scope(
    state: VisualAgentState,
    blocking: list[dict],
    exhausted_shot_ids: list[str],
) -> dict | None:
    assets = {
        str(asset.get("asset_id", "")): asset
        for asset in state.get("foundation_assets", []) if isinstance(asset, dict)
    }
    exhausted_set = set(map(str, exhausted_shot_ids))
    exhausted_by_asset: dict[str, set[str]] = {}
    for issue in blocking:
        shot_id = str(issue.get("shot_id", ""))
        if shot_id not in exhausted_set:
            continue
        candidate_ids = list(issue.get("focus_asset_ids", [])) or list(
            issue.get("reference_asset_ids", [])
        )
        for asset_id in map(str, candidate_ids):
            asset = assets.get(asset_id)
            if (
                not asset or asset.get("asset_type") != "key_prop"
                or not _asset_scale_contract(asset).get("required")
            ):
                continue
            exhausted_by_asset.setdefault(asset_id, set()).add(shot_id)
    reset_asset_ids = sorted(
        asset_id for asset_id, shot_ids in exhausted_by_asset.items()
        if len(shot_ids) >= 2
    )
    if not reset_asset_ids:
        return None
    affected_shot_ids = sorted({
        str(issue.get("shot_id", ""))
        for issue in blocking
        if str(issue.get("shot_id", ""))
        and set(map(str, (
            list(issue.get("focus_asset_ids", []))
            or list(issue.get("reference_asset_ids", []))
        ))).intersection(reset_asset_ids)
    })
    return {
        "asset_ids": reset_asset_ids,
        "exhausted_shot_ids": sorted(exhausted_set),
        "affected_shot_ids": affected_shot_ids,
        "scale_contracts": {
            asset_id: _asset_scale_contract(assets[asset_id]) for asset_id in reset_asset_ids
        },
        "reason": (
            "multiple reviewed clean-regeneration failures share a source-declared scale asset; "
            "the locked reference contract must be reset before more shot calls"
        ),
    }


def _prepare_post_foundation_reset_transfer(
    state: VisualAgentState,
    group: dict,
    group_state: dict,
    blocking: list[dict],
    trigger: str,
) -> dict | None:
    """Classify reviewed post-reset failures without requesting another Foundation reset."""
    reset = state.get("foundation_reset", {})
    group_id = str(group.get("group_id", ""))
    if (
        not isinstance(reset, dict)
        or reset.get("status") != "shot_review"
        or str(reset.get("group_id", "")) != group_id
        or not blocking
    ):
        return None
    reset_asset_ids = sorted(set(map(str, reset.get("asset_ids", []))))
    reset_locked = reset.get("locked_assets", {})
    current_locked = state.get("locked_assets", {})
    if not reset_asset_ids or not isinstance(reset_locked, dict):
        return None
    for asset_id in reset_asset_ids:
        snapshot = reset_locked.get(asset_id)
        current = current_locked.get(asset_id)
        if not isinstance(snapshot, dict) or not isinstance(current, dict):
            return None
        comparable = [
            key for key in ("path", "candidate_id", "version")
            if snapshot.get(key) not in {None, ""}
        ]
        if not comparable or any(current.get(key) != snapshot.get(key) for key in comparable):
            return None

    reset_asset_set = set(reset_asset_ids)
    reset_related_issues: list[dict] = []
    for issue in blocking:
        if not isinstance(issue, dict):
            continue
        issue_asset_ids = set(map(str, [
            *list(issue.get("focus_asset_ids") or []),
            *list(issue.get("reference_asset_ids") or []),
        ]))
        if issue_asset_ids.intersection(reset_asset_set):
            reset_related_issues.append(copy.deepcopy(issue))
    if not reset_related_issues:
        return None

    blocking_issues = [copy.deepcopy(issue) for issue in blocking if isinstance(issue, dict)]
    shot_ids = sorted({
        str(issue.get("shot_id", "")) for issue in blocking_issues
        if str(issue.get("shot_id", ""))
    })
    reset_related_shot_ids = sorted({
        str(issue.get("shot_id", "")) for issue in reset_related_issues
        if str(issue.get("shot_id", ""))
    })
    if not shot_ids or not reset_related_shot_ids:
        return None

    existing = group_state.get("post_foundation_reset_transfer", {})
    if isinstance(existing, dict) and existing.get("status") not in {None, "", "required"}:
        return None
    planned_at = (
        str(existing.get("planned_at", ""))
        if isinstance(existing, dict) else ""
    ) or _now()
    reference_policy = {
        "mode": "locked_assets_only",
        "exclude_failed_shot_reference": True,
        "exclude_temporal_image_references": True,
        "failed_shot_reference_included": False,
    }
    marker = {
        "schema_version": 1,
        "status": "required",
        "group_id": group_id,
        "shot_ids": shot_ids,
        "reset_related_shot_ids": reset_related_shot_ids,
        "reset_asset_ids": reset_asset_ids,
        "reference_policy": copy.deepcopy(reference_policy),
        "manual_decision_required": "regenerate",
        "planned_at": planned_at,
        "trigger": trigger,
    }
    group_state["post_foundation_reset_transfer"] = marker
    state["repair_plan"] = {
        "schema_version": "1.0",
        "status": "post_foundation_reset_transfer_required",
        "source_run_id": state.get("run_id", ""),
        "created_at": planned_at,
        "requires_new_paid_grant": False,
        "maximum_paid_calls": 0,
        "group_ids": [group_id],
        "shot_ids": shot_ids,
        "reset_related_shot_ids": reset_related_shot_ids,
        "issues": blocking_issues,
        "reset_related_issues": reset_related_issues,
        "groups": [{
            "group_id": group_id,
            "shot_ids": shot_ids,
            "reset_related_shot_ids": reset_related_shot_ids,
            "maximum_paid_calls": 0,
            "issues": blocking_issues,
        }],
        "reference_policy": reference_policy,
        "manual_decision_required": "regenerate",
        "allowed_manual_decisions": ["regenerate", "reject"],
        "foundation_reset_evidence": copy.deepcopy(reset),
        "convergence": {
            "status": "post_foundation_reset_transfer_required",
            "reason": (
                "reviewed shots still block after a newly locked Foundation reset; transfer all current "
                "locked assets without failed or adjacent shot image references before considering another reset"
            ),
            "reset_asset_ids": reset_asset_ids,
            "reset_related_shot_ids": reset_related_shot_ids,
            "reference_policy": copy.deepcopy(reference_policy),
        },
        "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
    }
    transition = {
        "scope": group_id,
        "operation": "post_foundation_reset_transfer",
        "shot_ids": shot_ids,
        "reset_related_shot_ids": reset_related_shot_ids,
        "asset_ids": reset_asset_ids,
        "trigger": trigger,
    }
    _trace(state, "post_foundation_reset_transfer_classified", transition)
    return transition


def _close_blocked_post_foundation_reset_transfer(
    state: VisualAgentState,
    group: dict,
    group_state: dict,
    blocking: list[dict],
    trigger: str,
) -> dict | None:
    """Make a reviewed locked-assets-only transfer failure terminal until a new strategy exists."""
    transfer = group_state.get("post_foundation_reset_transfer", {})
    group_id = str(group.get("group_id", ""))
    if (
        not isinstance(transfer, dict)
        or transfer.get("status") not in {"shot_review", "blocked"}
        or str(transfer.get("group_id", group_id)) != group_id
        or not blocking
    ):
        return None
    blocked_at = str(transfer.get("blocked_at", "")) or _now()
    transfer.update({
        "status": "blocked",
        "blocked_at": blocked_at,
        "manual_decision_required": "reject",
        "blocking_shot_ids": sorted({
            str(issue.get("shot_id", "")) for issue in blocking
            if isinstance(issue, dict) and str(issue.get("shot_id", ""))
        }),
    })
    blocking_issues = [copy.deepcopy(issue) for issue in blocking if isinstance(issue, dict)]
    state["repair_plan"] = {
        **copy.deepcopy(state.get("repair_plan", {})),
        "status": "post_foundation_reset_transfer_blocked",
        "updated_at": blocked_at,
        "requires_new_paid_grant": False,
        "maximum_paid_calls": 0,
        "shot_ids": list(transfer["blocking_shot_ids"]),
        "issues": blocking_issues,
        "manual_decision_required": "reject",
        "allowed_manual_decisions": ["reject"],
        "convergence": {
            "status": "post_foundation_reset_transfer_blocked",
            "reason": (
                "the reviewed locked-assets-only transfer remains blocked; do not override or spend another "
                "shot grant until a distinct, evidence-backed strategy is defined"
            ),
            "reference_policy": copy.deepcopy(transfer.get("reference_policy", {})),
        },
    }
    transition = {
        "scope": group_id,
        "operation": "post_foundation_reset_transfer_blocked",
        "shot_ids": list(transfer["blocking_shot_ids"]),
        "trigger": trigger,
    }
    _trace(state, "post_foundation_reset_transfer_blocked", transition)
    return transition


def _post_transfer_scale_evidence_scope(
    state: VisualAgentState,
    group_state: dict,
    blocking: list[dict],
) -> dict | None:
    """Return a structured one-shot recovery scope for unresolved scale assets."""
    transfer = group_state.get("post_foundation_reset_transfer", {})
    if (
        not isinstance(transfer, dict)
        or transfer.get("status") not in {"shot_review", "blocked"}
        or not blocking
    ):
        return None
    reset_asset_ids = set(map(str, transfer.get("reset_asset_ids", [])))
    reset = state.get("foundation_reset", {})
    if not reset_asset_ids and isinstance(reset, dict):
        reset_asset_ids = set(map(str, reset.get("asset_ids", [])))
    reset_locked = reset.get("locked_assets", {}) if isinstance(reset, dict) else {}
    current_locked = state.get("locked_assets", {})
    assets = _foundation_asset_index(state)
    if not reset_asset_ids or not isinstance(reset_locked, dict):
        return None

    eligible_assets: set[str] = set()
    for asset_id in reset_asset_ids:
        asset = assets.get(asset_id)
        snapshot = reset_locked.get(asset_id)
        current = current_locked.get(asset_id)
        if (
            not isinstance(asset, dict)
            or asset.get("asset_type") != "key_prop"
            or _asset_scale_contract(asset).get("required") is not True
            or not isinstance(snapshot, dict)
            or not isinstance(current, dict)
        ):
            continue
        comparable = [
            key for key in ("path", "candidate_id", "version")
            if snapshot.get(key) not in {None, ""}
        ]
        path = os.path.join(state["output_dir"], str(current.get("path", "")))
        if (
            comparable
            and all(current.get(key) == snapshot.get(key) for key in comparable)
            and os.path.isfile(path)
            and os.path.getsize(path) > 0
        ):
            eligible_assets.add(asset_id)
    if not eligible_assets:
        return None

    assets_by_shot: dict[str, list[str]] = {}
    eligible_issues: list[dict] = []
    for issue in blocking:
        if (
            not isinstance(issue, dict)
            or str(issue.get("correction_target", "")).strip().lower() != "prop_geometry"
        ):
            return None
        shot_id = str(issue.get("shot_id", ""))
        issue_assets = set(map(str, [
            *list(issue.get("focus_asset_ids") or []),
            *list(issue.get("reference_asset_ids") or []),
        ])).intersection(eligible_assets)
        if not shot_id or not issue_assets:
            return None
        assets_by_shot.setdefault(shot_id, [])
        assets_by_shot[shot_id] = sorted(set(assets_by_shot[shot_id]).union(issue_assets))
        eligible_issues.append(copy.deepcopy(issue))
    if not eligible_issues:
        return None
    return {
        "shot_ids": sorted(assets_by_shot),
        "asset_ids": sorted({
            asset_id for values in assets_by_shot.values() for asset_id in values
        }),
        "asset_ids_by_shot": assets_by_shot,
        "issues": eligible_issues,
    }


def _prepare_post_transfer_scale_evidence_reconstruction(
    state: VisualAgentState,
    group: dict,
    group_state: dict,
    blocking: list[dict],
    trigger: str,
) -> dict | None:
    """Offer one distinct locked-reference strategy after transfer non-convergence."""
    existing = group_state.get("post_transfer_scale_evidence_reconstruction", {})
    if isinstance(existing, dict) and existing.get("status") not in {None, "", "required"}:
        return None
    scope = _post_transfer_scale_evidence_scope(state, group_state, blocking)
    if scope is None:
        return None
    group_id = str(group.get("group_id", ""))
    planned_at = (
        str(existing.get("planned_at", "")) if isinstance(existing, dict) else ""
    ) or _now()
    reference_policy = {
        "mode": "scale_evidence_priority_locked_assets",
        "exclude_failed_shot_reference": True,
        "exclude_temporal_image_references": True,
        "failed_shot_reference_included": False,
        "dominant_full_comparator_reference": True,
        "primary_canvas_fraction": 2 / 3,
    }
    marker = {
        "schema_version": 1,
        "status": "required",
        "group_id": group_id,
        "shot_ids": scope["shot_ids"],
        "scale_evidence_asset_ids": scope["asset_ids"],
        "scale_evidence_asset_ids_by_shot": scope["asset_ids_by_shot"],
        "reference_policy": copy.deepcopy(reference_policy),
        "manual_decision_required": "regenerate",
        "planned_at": planned_at,
        "trigger": trigger,
    }
    group_state["post_transfer_scale_evidence_reconstruction"] = marker
    state["repair_plan"] = {
        "schema_version": "1.0",
        "status": "post_transfer_scale_evidence_reconstruction_required",
        "source_run_id": state.get("run_id", ""),
        "created_at": planned_at,
        "requires_new_paid_grant": False,
        "maximum_paid_calls": 0,
        "group_ids": [group_id],
        "shot_ids": scope["shot_ids"],
        "issues": scope["issues"],
        "groups": [{
            "group_id": group_id,
            "shot_ids": scope["shot_ids"],
            "maximum_paid_calls": 0,
            "issues": scope["issues"],
        }],
        "scale_evidence_asset_ids": scope["asset_ids"],
        "scale_evidence_asset_ids_by_shot": scope["asset_ids_by_shot"],
        "reference_policy": reference_policy,
        "manual_decision_required": "regenerate",
        "allowed_manual_decisions": ["regenerate", "reject"],
        "convergence": {
            "status": "post_transfer_scale_evidence_reconstruction_required",
            "reason": (
                "all remaining reviewed blockers structurally target reset key props with required, locked "
                "scale-evidence contracts; one dominant-comparator reconstruction may be approved"
            ),
            "reference_policy": copy.deepcopy(reference_policy),
        },
        "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
    }
    _sync_blocked_quality_gate(state)
    transition = {
        "scope": group_id,
        "operation": "scale_evidence_reconstruction",
        "shot_ids": scope["shot_ids"],
        "asset_ids": scope["asset_ids"],
        "trigger": trigger,
    }
    _trace(state, "post_transfer_scale_evidence_reconstruction_classified", transition)
    return transition


def _close_blocked_post_transfer_scale_evidence_reconstruction(
    state: VisualAgentState,
    group: dict,
    group_state: dict,
    blocking: list[dict],
    trigger: str,
) -> dict | None:
    marker = group_state.get("post_transfer_scale_evidence_reconstruction", {})
    group_id = str(group.get("group_id", ""))
    if (
        not isinstance(marker, dict)
        or marker.get("status") not in {"shot_review", "blocked"}
        or str(marker.get("group_id", group_id)) != group_id
        or not blocking
    ):
        return None
    blocked_at = str(marker.get("blocked_at", "")) or _now()
    shot_ids = sorted({
        str(issue.get("shot_id", "")) for issue in blocking
        if isinstance(issue, dict) and str(issue.get("shot_id", ""))
    })
    marker.update({
        "status": "blocked",
        "blocked_at": blocked_at,
        "manual_decision_required": "reject",
        "blocking_shot_ids": shot_ids,
    })
    state["repair_plan"] = {
        **copy.deepcopy(state.get("repair_plan", {})),
        "status": "post_transfer_scale_evidence_reconstruction_blocked",
        "updated_at": blocked_at,
        "requires_new_paid_grant": False,
        "maximum_paid_calls": 0,
        "shot_ids": shot_ids,
        "issues": [copy.deepcopy(issue) for issue in blocking if isinstance(issue, dict)],
        "manual_decision_required": "reject",
        "allowed_manual_decisions": ["reject"],
        "convergence": {
            "status": "post_transfer_scale_evidence_reconstruction_blocked",
            "reason": (
                "the one approved dominant-comparator reconstruction remains blocked; do not override or "
                "repeat it without another distinct, evidence-backed strategy"
            ),
            "reference_policy": copy.deepcopy(marker.get("reference_policy", {})),
        },
    }
    _sync_blocked_quality_gate(state)
    transition = {
        "scope": group_id,
        "operation": "scale_evidence_reconstruction_blocked",
        "shot_ids": shot_ids,
        "trigger": trigger,
    }
    _trace(state, "post_transfer_scale_evidence_reconstruction_blocked", transition)
    return transition


def _apply_scene_convergence_gate(
    state: VisualAgentState,
    group: dict,
    group_state: dict,
    blocking: list[dict],
) -> bool:
    """Stop ordinary auto/manual retry loops after a clean regeneration also fails."""
    if state.get("vision_recheck_only") or state.get("vision_repair_mode") or not blocking:
        return False
    blocking_by_shot: dict[str, list[dict]] = {}
    for issue in blocking:
        shot_id = str(issue.get("shot_id", ""))
        if shot_id:
            blocking_by_shot.setdefault(shot_id, []).append(issue)
    contracts = {
        shot_id: _correction_contract(str(group.get("group_id", "")), shot_id, issues)
        for shot_id, issues in blocking_by_shot.items()
    }
    evidence: dict[str, dict] = {}
    exhausted: list[str] = []
    for shot_id in sorted(blocking_by_shot):
        contract = contracts[shot_id]
        attempts = [
            item for item in group_state.get("revision_attempt_history", [])
            if isinstance(item, dict)
            and str(item.get("shot_id", "")) == shot_id
            and str(item.get("correction_contract_id", "")) == contract["correction_contract_id"]
            and item.get("provider_outcome") in {"succeeded", "recovered"}
        ]
        clean_attempts = [
            item for item in attempts if item.get("strategy") == "clean_regeneration"
            and str(item.get("artifact_path", ""))
        ]
        if clean_attempts:
            executed = list(dict.fromkeys(
                str(item.get("strategy", "")) for item in attempts if str(item.get("strategy", ""))
            ))
            evidence[shot_id] = {
                "lineage_status": "verified",
                "correction_contract": contract,
                "executed_strategies": executed,
                "clean_attempt_id": clean_attempts[-1].get("attempt_id", ""),
            }
            exhausted.append(shot_id)
            continue
        legacy_clean = _latest_shot_revision_role(state, group_state, shot_id) == "clean_regeneration"
        legacy_reviews = sum(
            1 for snapshot in group_state.get("review_history", [])
            if isinstance(snapshot, dict) and any(
                isinstance(issue, dict) and issue.get("blocking") is True
                and str(issue.get("shot_id", "")) == shot_id
                for issue in snapshot.get("issues", [])
            )
        )
        if not group_state.get("revision_attempt_history") and legacy_clean and legacy_reviews >= 3:
            evidence[shot_id] = {
                "lineage_status": "legacy_unverified",
                "correction_contract": contract,
                "executed_strategies": ["clean_regeneration"],
                "clean_attempt_id": "",
            }
            exhausted.append(shot_id)
    if not exhausted:
        return False
    exhausted_set = set(exhausted)
    exhausted_issues = [
        copy.deepcopy(issue) for issue in blocking
        if str(issue.get("shot_id", "")) in exhausted_set
    ]
    reference_reset = _scale_reference_reset_scope(state, blocking, exhausted)
    reset_required = reference_reset is not None
    affected_shot_ids = (
        reference_reset["affected_shot_ids"] if reference_reset else exhausted
    )
    affected_shot_set = set(affected_shot_ids)
    deferred_issues = [
        copy.deepcopy(issue) for issue in blocking
        if str(issue.get("shot_id", "")) not in affected_shot_set
    ] if reset_required else []
    deferred_blocking_shot_ids = sorted({
        str(issue.get("shot_id", "")) for issue in deferred_issues
        if str(issue.get("shot_id", ""))
    })
    plan_issues = [
        copy.deepcopy(issue) for issue in blocking
        if str(issue.get("shot_id", "")) in affected_shot_set
    ] if reset_required else exhausted_issues
    state["repair_plan"] = {
        "schema_version": "1.0",
        "status": "foundation_reference_reset_required" if reset_required else "non_converging",
        "source_run_id": state.get("run_id", ""),
        "created_at": _now(),
        "requires_new_paid_grant": False,
        "maximum_paid_calls": 0,
        "estimated_shot_repair_calls_after_new_strategy": (
            len(affected_shot_ids) if reset_required else 0
        ),
        "estimated_foundation_candidate_calls": (
            len(reference_reset["asset_ids"])
            * int(state.get("budgets", {}).get("foundation_candidates", 0) or 0)
            if reference_reset else 0
        ),
        "group_ids": [str(group.get("group_id", ""))],
        "shot_ids": affected_shot_ids,
        "deferred_blocking_shot_ids": deferred_blocking_shot_ids,
        "deferred_issues": deferred_issues,
        "separate_repair_required": bool(deferred_blocking_shot_ids),
        "groups": [{
            "group_id": str(group.get("group_id", "")),
            "shot_ids": affected_shot_ids,
            "maximum_paid_calls": 0,
            "issues": plan_issues,
            "deferred_blocking_shot_ids": deferred_blocking_shot_ids,
            "deferred_issues": deferred_issues,
        }],
        "convergence": {
            "status": (
                "foundation_reference_reset_required" if reset_required else "strategy_exhausted"
            ),
            "exhausted_shot_ids": exhausted,
            "last_strategy": "clean_regeneration",
            "evidence_by_shot": evidence,
            "reference_reset": reference_reset or {},
            "post_reset_review": {
                "required": reset_required,
                "scope": "entire_scene_group" if reset_required else "",
                "group_id": str(group.get("group_id", "")) if reset_required else "",
                "shot_ids": list(map(str, group.get("shot_ids", []))) if reset_required else [],
            },
            "reason": reference_reset["reason"] if reference_reset else (
                "a reviewed clean-regeneration artifact remains blocked; executed strategies are "
                "reported from persisted attempt evidence only"
            ),
        },
        "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
    }
    group_state["status"] = "blocked"
    group_state["pending_paid_operation"] = ""
    group_state["approved"] = False
    state["status"] = "needs_review"
    state["stop_reason"] = (
        "foundation_reference_reset_required" if reset_required else "scene_group_non_converging"
    )
    state["stage"] = state["stop_reason"]
    state["pending_approval"] = {}
    _sync_blocked_quality_gate(state)
    return True


def _reclassify_scale_reference_nonconvergence(
    state: VisualAgentState, trigger: str,
) -> dict | None:
    if state.get("stop_reason") != "scene_group_non_converging":
        return None
    group = _current_group(state)
    if not group:
        return None
    group_state = state.get("group_states", {}).get(str(group.get("group_id", "")), {})
    blocking = [
        issue for issue in group_state.get("issues", [])
        if isinstance(issue, dict) and issue.get("blocking") is True
    ]
    if not blocking or not _apply_scene_convergence_gate(state, group, group_state, blocking):
        return None
    if state.get("stop_reason") != "foundation_reference_reset_required":
        return None
    reference_reset = state.get("repair_plan", {}).get(
        "convergence", {}
    ).get("reference_reset", {})
    transition = {
        "scope": str(group.get("group_id", "")),
        "operation": "foundation_reference_reset",
        "asset_ids": list(reference_reset.get("asset_ids", [])),
        "affected_shot_ids": list(reference_reset.get("affected_shot_ids", [])),
        "trigger": trigger,
    }
    _trace(state, "scale_reference_nonconvergence_reclassified", transition)
    return transition


def _current_phase_assets(state: VisualAgentState) -> tuple[str, list[dict]]:
    index = int(state.get("foundation_phase_index", 0))
    reset = state.get("foundation_reset", {})
    reset_asset_ids = set(map(str, reset.get("asset_ids", []))) if isinstance(reset, dict) else set()
    while index < len(FOUNDATION_PHASES):
        phase = FOUNDATION_PHASES[index]
        assets = [item for item in state.get("foundation_assets", []) if item["phase"] == phase]
        if reset_asset_ids and reset.get("status") in {
            "candidate_approval", "candidate_generation", "candidate_ranking", "candidate_lock",
        }:
            assets = [item for item in assets if str(item.get("asset_id", "")) in reset_asset_ids]
        if assets:
            state["foundation_phase_index"] = index
            return phase, assets
        index += 1
    state["foundation_phase_index"] = len(FOUNDATION_PHASES)
    return "", []


def _prepare_foundation_reference_reset(
    state: VisualAgentState, trigger: str,
) -> dict:
    existing = state.get("foundation_reset", {})
    if isinstance(existing, dict) and existing.get("status") in {
        "candidate_approval", "candidate_generation", "candidate_ranking", "candidate_lock",
        "reference_locked", "shot_regeneration",
    }:
        return copy.deepcopy(existing)
    plan = state.get("repair_plan", {})
    if not isinstance(plan, dict) or plan.get("status") != "foundation_reference_reset_required":
        raise ValueError("the current repair plan does not authorize a foundation reference reset")
    convergence = plan.get("convergence", {})
    scope = convergence.get("reference_reset", {}) if isinstance(convergence, dict) else {}
    asset_ids = sorted(set(map(str, scope.get("asset_ids", []))))
    affected_shot_ids = sorted(set(map(str, scope.get("affected_shot_ids", []))))
    group_ids = sorted(set(map(str, plan.get("group_ids", []))))
    if not asset_ids or not affected_shot_ids or len(group_ids) != 1:
        raise ValueError("foundation reference reset scope is incomplete or ambiguous")
    group_id = group_ids[0]
    affected_shot_set = set(affected_shot_ids)
    group_state = state.setdefault("group_states", {}).setdefault(group_id, {})
    deferred_issues = [
        copy.deepcopy(issue) for issue in group_state.get("issues", [])
        if isinstance(issue, dict) and issue.get("blocking") is True
        and str(issue.get("shot_id", "")) not in affected_shot_set
    ]
    deferred_blocking_shot_ids = sorted({
        str(issue.get("shot_id", "")) for issue in deferred_issues
        if str(issue.get("shot_id", ""))
    })
    plan["deferred_blocking_shot_ids"] = deferred_blocking_shot_ids
    plan["deferred_issues"] = deferred_issues
    plan["separate_repair_required"] = bool(deferred_blocking_shot_ids)
    plan_convergence = plan.setdefault("convergence", {})
    scene_group = next((
        item for item in state.get("scene_groups", [])
        if isinstance(item, dict) and str(item.get("group_id", "")) == group_id
    ), {})
    plan_convergence["post_reset_review"] = {
        "required": True,
        "scope": "entire_scene_group",
        "group_id": group_id,
        "shot_ids": list(map(str, scene_group.get("shot_ids", []))),
    }
    for group_plan in plan.get("groups", []):
        if isinstance(group_plan, dict) and str(group_plan.get("group_id", "")) == group_id:
            group_plan["deferred_blocking_shot_ids"] = deferred_blocking_shot_ids
            group_plan["deferred_issues"] = copy.deepcopy(deferred_issues)
    _backfill_foundation_reference_contracts(state)
    assets = {
        str(asset.get("asset_id", "")): asset
        for asset in state.get("foundation_assets", []) if isinstance(asset, dict)
    }
    missing = [asset_id for asset_id in asset_ids if asset_id not in assets]
    if missing:
        raise ValueError("foundation reset assets are missing: " + ", ".join(missing))
    phases = {str(assets[asset_id].get("phase", "")) for asset_id in asset_ids}
    if len(phases) != 1:
        raise ValueError("one foundation reset operation must target a single phase")
    phase = next(iter(phases))
    phase_index = FOUNDATION_PHASES.index(phase)
    round_by_asset: dict[str, int] = {}
    candidate_history = state.setdefault("foundation_candidate_history", {})
    previous_locked_assets: dict[str, dict] = {}
    for asset_id in asset_ids:
        prior_candidates = copy.deepcopy(state.get("candidates", {}).get(asset_id, []))
        if prior_candidates:
            candidate_history.setdefault(asset_id, []).extend(prior_candidates)
        round_by_asset[asset_id] = max(
            [int(item.get("round", 0) or 0) for item in prior_candidates] or [0]
        ) + 1
        state.setdefault("candidates", {})[asset_id] = []
        state.setdefault("rankings", {}).pop(asset_id, None)
        previous_locked_assets[asset_id] = copy.deepcopy(
            state.get("locked_assets", {}).get(asset_id, {})
        )
    shot_revision_round = _next_scene_revision_round(
        state, group_id, group_state
    )
    reset = {
        "schema_version": 1,
        "status": "candidate_approval",
        "asset_ids": asset_ids,
        "phase": phase,
        "group_id": group_id,
        "affected_shot_ids": affected_shot_ids,
        "deferred_blocking_shot_ids": sorted(set(map(
            str, plan.get("deferred_blocking_shot_ids", [])
        ))),
        "deferred_issues": copy.deepcopy(plan.get("deferred_issues", [])),
        "post_reset_review_scope": "entire_scene_group",
        "round_by_asset": round_by_asset,
        "previous_locked_assets": previous_locked_assets,
        "previous_group_retry_count": int(group_state.get("retry_count", 0) or 0),
        "shot_revision_round": shot_revision_round,
        "shot_retry_count": 0,
        "trigger": trigger,
        "prepared_at": _now(),
    }
    active_history = [
        item for item in group_state.get("revision_attempt_history", [])
        if isinstance(item, dict)
    ]
    archived_history = [
        {**copy.deepcopy(item), "archived_for_foundation_reset_at": reset["prepared_at"]}
        for item in active_history if str(item.get("shot_id", "")) in affected_shot_ids
    ]
    if archived_history:
        group_state.setdefault("revision_attempt_history_archive", []).extend(archived_history)
    group_state["revision_attempt_history"] = [
        item for item in active_history if str(item.get("shot_id", "")) not in affected_shot_ids
    ]
    group_state["retry_count"] = 0
    state["foundation_reset"] = reset
    state["foundation_phase_index"] = phase_index
    state["foundation_budget_approved"] = False
    state["foundation_retry_grant_id"] = ""
    state["status"] = "running"
    state["stop_reason"] = ""
    state["stage"] = "foundation_retry_approval"
    state["pending_approval"] = {}
    state["action"] = ""
    state["action_args"] = {}
    state["no_progress"] = 0
    _trace(state, "foundation_reference_reset_prepared", reset)
    return copy.deepcopy(reset)


def _current_group(state: VisualAgentState) -> dict | None:
    index = int(state.get("current_group_index", 0))
    groups = state.get("scene_groups", [])
    return groups[index] if 0 <= index < len(groups) else None


def _tool_inspect_storyboard(state: VisualAgentState) -> dict:
    errors = validate_storyboard(state["storyboard"])
    if errors:
        raise ValueError("storyboard validation failed: " + "; ".join(errors[:8]))
    preflight = _storyboard_asset_preflight(state["storyboard"])
    state["preflight_issues"] = preflight
    if preflight:
        state["issues"] = preflight
        state["status"] = "needs_review"
        state["stop_reason"] = "storyboard_asset_binding_invalid"
        state["stage"] = "blocked_upstream"
        state["quality_gate"] = {
            "mode": "code_preflight_blocked",
            "unverified_checks": ["foundation_generation", "shot_generation", "visual_semantics"],
        }
        return {"preflight_issue_count": len(preflight), "paid_calls_allowed": False}
    state["inventory"] = _build_inventory(state["storyboard"])
    state["stage"] = "inspected"
    return {"characters": len(state["inventory"]["characters"]),
            "scenes": len(state["inventory"]["scenes"]),
            "props": len(state["inventory"]["props"])}


def _tool_retrieve_playbook(state: VisualAgentState) -> dict:
    tags = {
        "new": ["storyboard", "image_plan"], "inspected": ["image_plan", "foundation"],
        "planned": ["foundation", "approval"], "foundation_generate": ["foundation", "prompt"],
        "foundation_rank": ["vision_review", "foundation"],
        "group_generate": ["shot_generation", "prompt"],
        "group_review": ["vision_review", "review"],
    }.get(state.get("stage", ""), ["image_plan"])
    sections = get_playbook_sections(tags)
    state.setdefault("visual_bible", {})["retrieved_playbook"] = sections
    return {"section_ids": [item["section_id"] for item in sections]}


def _tool_build_visual_bible(state: VisualAgentState) -> dict:
    if not state.get("inventory"):
        raise ValueError("inspect_storyboard must run first")
    assets = _build_foundation_assets(state["storyboard"], state["inventory"])
    candidate_count = int(state["budgets"]["foundation_candidates"])
    state["foundation_assets"] = assets
    state["visual_bible"] = {
        "version": "1", "playbook_version": PLAYBOOK_VERSION,
        "style_anchor": get_style_anchor(state["storyboard"]),
        "aspect_ratio": state["storyboard"].get("creative_bible", {}).get("aspect_ratio", "9:16"),
        "asset_specs": assets, "locked_assets": {},
    }
    maximum_image_calls = len(assets) * candidate_count
    state["budgets"].update({
        "foundation_max_image_calls": maximum_image_calls,
        "scene_first_pass_image_calls": sum(
            len(scene.get("shots", [])) for scene in state["storyboard"].get("scenes", [])
        ),
    })
    if state["budgets"].get("requested_max_calls") == "auto":
        scene_count = len(state["storyboard"].get("scenes", []))
        state["budgets"]["effective_max_calls"] = min(
            120, max(40, 16 + 3 * len(assets) + 6 * scene_count)
        )
    if state["budgets"].get("requested_max_steps") == "auto":
        scene_count = len(state["storyboard"].get("scenes", []))
        state["budgets"]["effective_max_steps"] = min(
            180, max(60, 28 + 4 * len(assets) + 8 * scene_count)
        )
    state["stage"] = "planned"
    return {"asset_count": len(assets), "maximum_image_calls": maximum_image_calls}


def _tool_request_foundation_approval(state: VisualAgentState) -> dict:
    if state.get("stage") == "foundation_retry_approval":
        phase, assets = _current_phase_assets(state)
        stage = "foundation_retry_cost"
        target = int(state["budgets"]["foundation_candidates"])
        maximum = sum(max(0, target - len([
            item for item in state.get("candidates", {}).get(asset["asset_id"], [])
            if _candidate_is_published(state, item)
        ])) for asset in assets)
        item_ids = [item["asset_id"] for item in assets]
        extra = {"phase": phase, "reason": "all prior candidates rejected"}
        reset = state.get("foundation_reset", {})
        if isinstance(reset, dict) and reset.get("status") == "candidate_approval":
            extra = {
                "phase": phase,
                "reason": "foundation reference scale contract reset",
                "foundation_reset": copy.deepcopy(reset),
            }
    else:
        if not state.get("foundation_assets"):
            raise ValueError("visual plan must exist before approval")
        stage = "foundation_cost"
        maximum = int(state["budgets"]["foundation_max_image_calls"])
        item_ids = [item["asset_id"] for item in state["foundation_assets"]]
        extra = {"phases": list(FOUNDATION_PHASES), "candidates_per_asset": state["budgets"]["foundation_candidates"]}
    _write_approval_request(state, stage, item_ids, maximum, extra)
    return {"approval_stage": stage, "maximum_paid_calls": maximum}


def _tool_generate_foundation_candidates(state: VisualAgentState,
                                         image_provider: ImageProvider) -> dict:
    if not state.get("foundation_budget_approved"):
        raise ValueError("foundation paid budget has not been approved")
    if not state.get("paid_authorized"):
        state["status"] = "awaiting_approval"
        state["stop_reason"] = "paid_calls_not_authorized"
        return {"generated": 0, "reason": "run again with --image-api"}
    grant_id = str(state.get("foundation_grant_id", ""))
    if not grant_id:
        raise ValueError("foundation approval grant is missing")
    phase, assets = _current_phase_assets(state)
    if not assets:
        state["stage"] = "foundation_complete"
        return {"phase": "complete", "generated": 0}
    count = int(state["budgets"]["foundation_candidates"])
    reset = state.get("foundation_reset", {})
    if isinstance(reset, dict) and reset.get("status") == "candidate_approval":
        reset["status"] = "candidate_generation"
    generated_count = 0
    jobs: list[dict] = []
    for asset in assets:
        existing = state.setdefault("candidates", {}).setdefault(asset["asset_id"], [])
        valid_existing = [
            item for item in existing
            if _candidate_is_published(state, item)
        ]
        reset_round = _active_foundation_candidate_round(state, asset["asset_id"])
        if not reset_round and len(valid_existing) >= count:
            state["candidates"][asset["asset_id"]] = valid_existing[:count]
            continue
        if reset_round:
            round_number = reset_round
            valid_existing = [
                item for item in valid_existing if int(item.get("round", 0) or 0) == reset_round
            ]
        elif valid_existing and len({item.get("round") for item in valid_existing}) == 1:
            round_number = int(valid_existing[0].get("round", 1))
        else:
            round_number = max([int(item.get("round", 0)) for item in existing] or [0]) + 1
            valid_existing = []
        if len(valid_existing) >= count:
            continue
        candidate_dir = os.path.join(
            state["output_dir"], "assets", "foundation", "candidates",
            state["run_id"], safe_filename(asset["asset_id"])
        )
        os.makedirs(candidate_dir, exist_ok=True)
        references = _provider_references(state, asset.get("dependencies", []), asset["asset_id"])
        state["candidates"][asset["asset_id"]] = list(valid_existing)
        for number in range(1, count + 1):
            candidate_id = f"{asset['asset_id']}_r{round_number:02d}_c{number:02d}"
            if any(item.get("candidate_id") == candidate_id for item in valid_existing):
                continue
            path = os.path.join(
                candidate_dir, f"{safe_filename(candidate_id)}_{state['run_id'][:8]}.png"
            )
            prompt = _asset_prompt(asset, state["storyboard"], number, round_number)
            jobs.append({
                "job_id": f"foundation:{candidate_id}", "asset_id": asset["asset_id"],
                "candidate_id": candidate_id, "round": round_number, "number": number,
                "operation_kind": "foundation_candidate",
                "prompt": prompt, "output_path": path,
                "references": references, "size": state["size"],
            })
    results = _run_paid_image_jobs(
        state, image_provider, jobs, int(state["budgets"].get("image_parallelism", 4)),
        grant_id,
    )
    failures: list[str] = []
    for item in results:
        result = item.get("result")
        if not result:
            failures.append(f"{item['candidate_id']}: {item.get('error', 'failed')}")
            continue
        metadata = read_json(str(result) + ".manju.json") or {}
        foundation_metadata = metadata.get("foundation_image", {})
        candidates = state["candidates"].setdefault(item["asset_id"], [])
        if not any(value.get("candidate_id") == item["candidate_id"] for value in candidates):
            candidates.append({
                "candidate_id": item["candidate_id"], "asset_id": item["asset_id"],
                "round": item["round"], "number": item["number"],
                "path": os.path.relpath(result, state["output_dir"]),
                "prompt_fingerprint": content_fingerprint(item["prompt"], length=16),
                "image_metadata": foundation_metadata,
                "ledger_job_id": item.get("ledger_job_id", ""),
            })
            generated_count += 1
    if failures:
        state["foundation_budget_approved"] = False
        state["stage"] = "foundation_retry_approval"
        return {
            "phase": phase, "generated": generated_count,
            "failed": failures[:20], "requires_new_paid_approval": True,
            "parallelism": state["budgets"].get("image_parallelism", 4),
        }
    primary_grant_id = str(state.get("foundation_primary_grant_id", ""))
    if state.get("foundation_retry_grant_id") and primary_grant_id:
        state["foundation_grant_id"] = primary_grant_id
        state["foundation_retry_grant_id"] = ""
    if isinstance(reset, dict) and reset.get("status") == "candidate_generation":
        reset["status"] = "candidate_ranking"
    state["stage"] = "foundation_rank"
    return {"phase": phase, "generated": generated_count,
            "parallelism": state["budgets"].get("image_parallelism", 4)}


def _tool_rank_foundation_candidates(state: VisualAgentState,
                                     vision_provider: VisionProvider) -> dict:
    phase, assets = _current_phase_assets(state)
    unavailable = False
    for asset in assets:
        candidates = state.get("candidates", {}).get(asset["asset_id"], [])
        candidate_paths = [os.path.join(state["output_dir"], item["path"]) for item in candidates]
        dependency_paths = _locked_reference_paths(state, asset.get("dependencies", []))
        paths = candidate_paths + dependency_paths
        context = {
            "asset": asset, "candidate_ids": [item["candidate_id"] for item in candidates],
            "reference_contract": _effective_reference_contract(asset),
            "scale_contract": _asset_scale_contract(asset),
            "image_order": {
                "candidates": [item["candidate_id"] for item in candidates],
                "locked_dependencies": asset.get("dependencies", []),
            },
            "criteria": get_playbook_sections(["foundation", "vision_review", asset["asset_type"]]),
        }
        result = _record_vision_result(
            state, "rank_foundation_candidates",
            vision_provider("rank_foundation_candidates", paths, context),
        )
        if result is None:
            unavailable = True
            result = {
                "ranking": [],
                "unranked_candidate_ids": [item["candidate_id"] for item in candidates],
                "summary": "vision API unavailable; candidates are unranked and a human must compare them",
                "vision_available": False,
            }
        else:
            valid_ids = {item["candidate_id"] for item in candidates}
            ranking = result.get("ranking", []) if isinstance(result, dict) else []
            ranking = [str(item) for item in ranking if str(item) in valid_ids]
            result["ranking"] = ranking + [item for item in valid_ids if item not in ranking]
            result["vision_available"] = True
        state.setdefault("rankings", {})[asset["asset_id"]] = result
    reset = state.get("foundation_reset", {})
    if isinstance(reset, dict) and reset.get("status") == "candidate_ranking":
        reset["status"] = "candidate_lock"
    state["stage"] = "foundation_lock"
    return {"phase": phase, "vision_unavailable": unavailable}


def _tool_request_foundation_lock(state: VisualAgentState) -> dict:
    phase, assets = _current_phase_assets(state)
    if not assets:
        state["stage"] = "foundation_complete"
        return {"phase": "complete"}
    candidate_summary = {
        asset["asset_id"]: {
            "candidates": state.get("candidates", {}).get(asset["asset_id"], []),
            "ranking": state.get("rankings", {}).get(asset["asset_id"], {}),
            "reference_contract": _effective_reference_contract(asset),
        } for asset in assets
    }
    _write_approval_request(
        state, f"foundation_lock_{phase}", [item["asset_id"] for item in assets], 0,
         {"phase": phase, "candidate_summary": candidate_summary,
          "reference_contracts": {
              asset["asset_id"]: _effective_reference_contract(asset) for asset in assets
              if _effective_reference_contract(asset)
          },
         "instructions": (
             "Set decision=approve and selections[asset_id]=candidate_id. For an asset with a "
             "canonical_geometry_anchor contract, reject every candidate that contains multiple object "
             "instances, multiple views, grids, insets, or state/color sequences; approve only one complete "
             "object in one canonical view on a clean background. When scale_contract.required=true, also "
             "confirm scale_evidence_present and scale_relation_matches against its source_cues, plus "
             "scale_comparator_complete, scale_comparator_in_focus and "
             "scale_comparator_contact_or_shared_plane. Reject cropped or blurred comparator fragments, detached "
             "comparators, and comparators on a different depth or focal plane; a frame-filling isolated product "
             "view alone is not scale evidence."
         )},
    )
    return {"phase": phase, "asset_count": len(assets)}


def _tool_build_scene_groups(state: VisualAgentState) -> dict:
    required = {item["asset_id"] for item in state.get("foundation_assets", [])}
    missing = sorted(required.difference(state.get("locked_assets", {})))
    if missing:
        raise ValueError("foundation assets are not locked: " + ", ".join(missing[:8]))
    state["scene_groups"] = _build_scene_groups(state)
    state["group_states"] = {
        group["group_id"]: {"status": "planned", "approved": False, "retry_count": 0,
                            "generated": {}, "issues": [], "revision_attempt_history": [],
                            "pending_paid_operation": "initial"}
        for group in state["scene_groups"]
    }
    state["current_group_index"] = 0
    state["stage"] = "group_approval" if state["scene_groups"] else "ready_finalize"
    return {"scene_group_count": len(state["scene_groups"])}


def _tool_request_scene_group_approval(state: VisualAgentState) -> dict:
    group = _current_group(state)
    if group is None:
        state["stage"] = "ready_finalize"
        return {"complete": True}
    group_state = state["group_states"][group["group_id"]]
    operation = group_state.get("pending_paid_operation", "initial")
    if operation == "technical_retry":
        technical_retry = group_state.get("technical_retry", {})
        item_ids = sorted(set(map(str, technical_retry.get("shot_ids", []))))
        maximum = len(item_ids)
    elif operation == "provider_escalation":
        escalation = group_state.get("provider_escalation", {})
        tasks = [
            item for item in escalation.get("tasks", []) if isinstance(item, dict)
        ] if isinstance(escalation, dict) else []
        item_ids = sorted({
            str(item.get("shot_id", "")) for item in tasks if str(item.get("shot_id", ""))
        })
        maximum = len(tasks)
        if maximum != len(item_ids):
            raise ValueError("provider escalation must contain exactly one task per shot")
    elif operation == "reference_reset_retry":
        item_ids = sorted(set(map(str, group_state.get("affected_shot_ids", []))))
        maximum = len(item_ids)
    elif operation == "post_foundation_reset_transfer":
        transfer = group_state.get("post_foundation_reset_transfer", {})
        item_ids = sorted(set(map(str, transfer.get("shot_ids", []))))
        maximum = len(item_ids)
    elif operation == "scale_evidence_reconstruction":
        scale_reconstruction = group_state.get(
            "post_transfer_scale_evidence_reconstruction", {}
        )
        item_ids = sorted(set(map(str, scale_reconstruction.get("shot_ids", []))))
        maximum = len(item_ids)
    elif operation == "retry":
        item_ids = sorted({
            str(item.get("shot_id")) for item in group_state.get("issues", [])
            if item.get("blocking") and item.get("shot_id")
        })
        maximum = len(item_ids)
    else:
        generated = group_state.get("generated", {})
        missing = sum(1 for shot_id in group["shot_ids"] if not (
            generated.get(shot_id) and os.path.isfile(
                os.path.join(state["output_dir"], generated.get(shot_id, ""))
            )
        ))
        maximum = missing + len(group["shot_ids"]) * int(state["budgets"]["max_auto_retries"])
        item_ids = group["shot_ids"]
    if maximum < 1:
        raise ValueError("scene group approval contains no payable image jobs")
    _write_approval_request(
        state, f"scene_group_cost_{group['group_id']}", item_ids, maximum,
        {"scene_group": group, "operation": operation,
          "technical_retry": copy.deepcopy(group_state.get("technical_retry", {})),
          "provider_escalation": copy.deepcopy(group_state.get("provider_escalation", {})),
          "foundation_reset": copy.deepcopy(state.get("foundation_reset", {})),
          "post_foundation_reset_transfer": copy.deepcopy(
              group_state.get("post_foundation_reset_transfer", {})
          ),
          "post_transfer_scale_evidence_reconstruction": copy.deepcopy(
              group_state.get("post_transfer_scale_evidence_reconstruction", {})
          ),
          "included_retry_passes": state["budgets"]["max_auto_retries"]},
    )
    return {"group_id": group["group_id"], "maximum_paid_calls": maximum}


def _tool_generate_scene_group(state: VisualAgentState, image_provider: ImageProvider) -> dict:
    group = _current_group(state)
    if group is None:
        state["stage"] = "ready_finalize"
        return {"complete": True}
    group_state = state["group_states"][group["group_id"]]
    if not group_state.get("approved"):
        raise ValueError("scene group paid budget has not been approved")
    if not state.get("paid_authorized"):
        state["status"] = "awaiting_approval"
        state["stop_reason"] = "paid_calls_not_authorized"
        return {"generated": 0, "reason": "run again with --image-api"}
    grant_id = str(group_state.get("grant_id", ""))
    if not grant_id:
        raise ValueError("scene group approval grant is missing")
    version = 1 + int(group_state.get("manual_regeneration", 0))
    generated = group_state.setdefault("generated", {})
    generated_count = 0
    shots_dir = os.path.join(
        state["output_dir"], "assets", "shots", state["run_id"], safe_filename(group["group_id"])
    )
    os.makedirs(shots_dir, exist_ok=True)
    jobs: list[dict] = []
    for shot in group["shots"]:
        shot_id = shot["shot_id"]
        existing = generated.get(shot_id)
        if existing and os.path.isfile(os.path.join(state["output_dir"], existing)):
            continue
        output_path = os.path.join(
            shots_dir, f"shot_{safe_filename(shot_id)}_v{version:03d}_{state['run_id'][:8]}.png"
        )
        prompt = _shot_prompt(state, group, shot)
        references = _provider_references(
            state, shot.get("reference_asset_ids", group["reference_asset_ids"]),
            f"{group['group_id']}_{safe_filename(shot_id)}_foundation",
        )
        jobs.append({
            "job_id": f"group:{group['group_id']}:v{version:03d}:{shot_id}",
            "shot_id": shot_id, "group_id": group["group_id"], "prompt": prompt,
            "operation_kind": "shot_initial",
            "visible_character_ids": list(shot.get("visible_character_ids", [])),
            "visible_prop_ids": list(shot.get("visible_prop_ids", [])),
            "reference_asset_ids": list(shot.get("reference_asset_ids", [])),
            "output_path": output_path, "references": references, "size": state["size"],
        })
    failures: list[str] = []
    for item in _run_paid_image_jobs(
        state, image_provider, jobs, int(state["budgets"].get("image_parallelism", 4)),
        grant_id,
    ):
        result = item.get("result")
        if not result:
            failures.append(f"{item['shot_id']}: {item.get('error', 'failed')}")
            continue
        generated[item["shot_id"]] = os.path.relpath(result, state["output_dir"])
        generated_count += 1
    if failures:
        group_state["approved"] = False
        group_state["status"] = "planned"
        group_state["pending_paid_operation"] = "initial"
        state["stage"] = "group_approval"
        return {
            "group_id": group["group_id"], "generated": generated_count,
            "failed": failures[:20], "requires_new_paid_approval": True,
        }
    group_state["status"] = "generated"
    group_state["pending_paid_operation"] = ""
    state["stage"] = "group_review"
    return {"group_id": group["group_id"], "generated": generated_count,
            "parallelism": state["budgets"].get("image_parallelism", 4)}


def _technical_image_issues(state: VisualAgentState, group: dict,
                            generated: dict[str, str]) -> list[dict]:
    issues: list[dict] = []
    shots = {item["shot_id"]: item for item in group["shots"]}
    for shot_id in group["shot_ids"]:
        relative = generated.get(shot_id, "")
        path = os.path.join(state["output_dir"], relative)
        if not relative or not os.path.isfile(path) or os.path.getsize(path) < 1:
            shot = shots[shot_id]
            issues.append({
                "issue_id": f"file_{group['group_id']}_{safe_filename(shot_id)}",
                "group_id": group["group_id"], "shot_id": shot_id,
                "category": "file", "severity": "critical", "blocking": True,
                "problem": "generated image is missing or empty",
                "instruction": "regenerate a valid image file",
                "storyboard_path": shot["storyboard_path"],
                "reference_asset_ids": [], "image_path": relative,
                "evidence_valid": True, "non_overridable": True,
            })
            continue
        try:
            from PIL import Image
            with Image.open(path) as image:
                width, height = image.size
            target_ratio = float(state.get("target_aspect_ratio", 9 / 16))
            if height < 1 or abs((width / height) - target_ratio) / target_ratio > 0.02:
                shot = shots[shot_id]
                issues.append({
                    "issue_id": f"aspect_{group['group_id']}_{safe_filename(shot_id)}",
                    "group_id": group["group_id"], "shot_id": shot_id,
                    "category": "aspect_ratio", "severity": "critical", "blocking": True,
                    "problem": f"image dimensions {width}x{height} do not match target aspect ratio",
                    "instruction": "normalize or regenerate the image at the storyboard aspect ratio",
                    "storyboard_path": shot["storyboard_path"],
                    "reference_asset_ids": [], "image_path": relative,
                    "evidence_valid": True, "non_overridable": True,
                })
            metadata = read_json(path + ".manju.json") or {}
            dimensions = metadata.get("dimensions", {}) if isinstance(metadata, dict) else {}
            if (dimensions.get("method") == "contain_on_blurred_canvas"
                    and float(dimensions.get("padding_fraction", 0) or 0) > 0.08):
                shot = shots[shot_id]
                issues.append({
                    "issue_id": f"padding_{group['group_id']}_{safe_filename(shot_id)}",
                    "group_id": group["group_id"], "shot_id": shot_id,
                    "category": "aspect_padding", "severity": "advisory", "blocking": False,
                    "problem": "contain_blur introduced a visually significant padded area",
                    "instruction": "Prefer native aspect output or cover mode if edge content can be cropped safely.",
                    "storyboard_path": shot["storyboard_path"],
                    "reference_asset_ids": [], "image_path": relative,
                    "evidence_valid": True, "non_overridable": False,
                })
        except Exception as exc:
            shot = shots[shot_id]
            issues.append({
                "issue_id": f"decode_{group['group_id']}_{safe_filename(shot_id)}",
                "group_id": group["group_id"], "shot_id": shot_id,
                "category": "file", "severity": "critical", "blocking": True,
                "problem": f"generated image cannot be decoded: {exc}",
                "instruction": "regenerate a decodable image file",
                "storyboard_path": shot["storyboard_path"],
                "reference_asset_ids": [], "image_path": relative,
                "evidence_valid": True, "non_overridable": True,
            })
    return issues


def _advance_verification_group(state: VisualAgentState) -> None:
    """Advance across the exact review/repair scope without revisiting completed groups."""
    state["current_group_index"] = int(state.get("current_group_index", 0)) + 1
    if state.get("vision_recheck_only"):
        selected = set(map(str, state.get("vision_recheck_group_ids", [])))
        next_stage = "group_review"
    elif state.get("vision_repair_mode"):
        selected = set(map(str, state.get("repair_group_ids", [])))
        next_stage = "group_approval"
    else:
        selected = set()
        next_stage = "group_review"
    while state["current_group_index"] < len(state.get("scene_groups", [])):
        candidate = state["scene_groups"][state["current_group_index"]]
        if not selected or str(candidate.get("group_id", "")) in selected:
            break
        state["current_group_index"] += 1
    state["stage"] = (
        next_stage
        if state["current_group_index"] < len(state.get("scene_groups", []))
        else "vision_recheck_finalize"
    )


def _tool_inspect_scene_group(state: VisualAgentState,
                              vision_provider: VisionProvider) -> dict:
    group = _current_group(state)
    if group is None:
        state["stage"] = "ready_finalize"
        return {"complete": True}
    group_state = state["group_states"][group["group_id"]]
    generated = group_state.get("generated", {})
    technical = _technical_image_issues(state, group, generated)
    generated_paths = [os.path.join(state["output_dir"], generated.get(shot_id, ""))
                       for shot_id in group["shot_ids"] if generated.get(shot_id)]
    reference_paths = _locked_reference_paths(state, group["reference_asset_ids"])
    paths = generated_paths + reference_paths
    open_contracts: dict[str, dict] = {}
    for attempt in group_state.get("revision_attempt_history", []):
        if not isinstance(attempt, dict) or not str(attempt.get("artifact_path", "")):
            continue
        contract_id = str(attempt.get("correction_contract_id", ""))
        if not contract_id.startswith("constraint_"):
            continue
        open_contracts[contract_id] = {
            "correction_contract_id": contract_id,
            "shot_id": str(attempt.get("shot_id", "")),
            "contract": copy.deepcopy(attempt.get("correction_contract", {})),
            "latest_strategy": str(attempt.get("strategy", "")),
        }
    foundation_by_id = {
        str(asset.get("asset_id", "")): asset
        for asset in state.get("foundation_assets", []) if isinstance(asset, dict)
    }
    scale_review_contracts = []
    for asset_id in group.get("reference_asset_ids", []):
        asset = foundation_by_id.get(str(asset_id))
        if not asset or asset.get("asset_type") != "key_prop":
            continue
        scale_contract = _asset_scale_contract(asset)
        if scale_contract.get("required"):
            scale_review_contracts.append({
                "asset_id": str(asset_id),
                **scale_contract,
            })
    context = {
        "group": group,
        "locked_assets": [state["locked_assets"].get(asset_id) for asset_id in group["reference_asset_ids"]],
        "generated_paths": generated,
        "image_order": {
            "generated_shot_ids": [shot_id for shot_id in group["shot_ids"] if generated.get(shot_id)],
            "locked_reference_asset_ids": [
                asset_id for asset_id in group["reference_asset_ids"]
                if asset_id in state.get("locked_assets", {})
            ],
        },
        "criteria": get_playbook_sections(["vision_review", "shot_generation", "review"]),
        "hard_constraints": {
            "single_continuous_frame": True,
            "forbidden_layouts": [
                "triptych", "collage", "contact_sheet", "stacked_panels",
                "split_screen", "inset", "multi_time_state_panels",
            ],
            "multi_panel_is_blocking_category": "artifact",
            "canonical_asset_frame_occupancy_is_not_scale_evidence": True,
            "scale_contracts": scale_review_contracts,
        },
        "open_correction_contracts": list(open_contracts.values()),
        "constraint_verdict_protocol": {
            "version": "4.0",
            "allowed_verdicts": ["pass", "fail", "unverifiable"],
            "blocking_requires": ["fail", "non_empty_evidence", "confidence_gte_0.75"],
            "confidence_calibration": calibration_summary(
                state.get("vision_confidence_calibration")
            ),
        },
        "compiled_constraints_by_shot": {
            str(shot.get("shot_id", "")): [
                item.to_dict() for item in compile_shot_constraints(shot, foundation_by_id)
            ]
            for shot in group.get("shots", []) if isinstance(shot, dict)
        },
    }
    raw = _record_vision_result(
        state, "review_scene_group",
        vision_provider("review_scene_group", paths, context),
    )
    if raw is None or not isinstance(raw.get("issues"), list):
        group_state["vision_available"] = False
        group_state["issues"] = technical
        _append_group_review_snapshot(
            state, group_state, generated, technical, vision_available=False
        )
        state["issues"] = [issue for item in state["group_states"].values()
                           for issue in item.get("issues", [])]
        if state.get("vision_recheck_only"):
            group_state["status"] = "blocked" if any(
                item.get("blocking") for item in technical
            ) else "unverified"
            _advance_verification_group(state)
            return {"group_id": group["group_id"], "vision_unavailable": True,
                    "technical_issue_count": len(technical), "recheck_continues": True}
        if state.get("vision_repair_mode"):
            group_state["status"] = "blocked" if any(
                item.get("blocking") for item in technical
            ) else "unverified"
            _advance_verification_group(state)
            return {"group_id": group["group_id"], "vision_unavailable": True,
                    "technical_issue_count": len(technical), "repair_review_continues": True}
        state["stage"] = "manual_review"
        return {"group_id": group["group_id"], "vision_unavailable": True,
                "technical_issue_count": len(technical)}
    group_state["vision_available"] = True
    semantic = _normalize_visual_issues(raw, state, group, generated)
    semantic = _stabilize_new_blockers_on_unchanged_images(
        state, group, generated, semantic, group_state, vision_provider
    )
    issues = technical + semantic
    group_state["issues"] = issues
    group_state.pop("issues_stale", None)
    group_state.pop("issues_stale_after_revision_shot_ids", None)
    quality_gate = state.setdefault("quality_gate", {})
    quality_gate.pop("stale_after_revision_shot_ids", None)
    if quality_gate.get("automated_review_status") == "stale_after_partial_revision":
        quality_gate["automated_review_status"] = "review_in_progress"
    if quality_gate.get("blocking_status") == "stale_after_partial_revision":
        quality_gate["blocking_status"] = "review_in_progress"
    _append_group_review_snapshot(
        state, group_state, generated, issues, vision_available=True
    )
    state["issues"] = [issue for item in state["group_states"].values()
                       for issue in item.get("issues", [])]
    blocking = [item for item in issues if item.get("blocking")]
    if blocking:
        _sync_blocked_quality_gate(state)
    escalation = group_state.get("provider_escalation", {})
    if (
        isinstance(escalation, dict)
        and escalation.get("status") == "shot_review"
    ):
        if blocking:
            escalation["status"] = "blocked"
            escalation["blocked_at"] = _now()
            escalation["blocking_issue_ids"] = [
                str(item.get("issue_id", "")) for item in blocking
                if str(item.get("issue_id", ""))
            ]
            group_state["status"] = "blocked"
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "provider_escalation_blocked",
                "requires_new_paid_grant": False,
                "maximum_paid_calls": 0,
                "blocking_issue_ids": list(escalation["blocking_issue_ids"]),
                "manual_action": (
                    "change provider capabilities or define a distinct reviewed strategy; "
                    "do not repeat the same paid constraint-isolation pass"
                ),
            }
            if state.get("vision_repair_mode"):
                _advance_verification_group(state)
            else:
                state["status"] = "needs_review"
                state["stop_reason"] = "provider_escalation_blocked"
                state["stage"] = "provider_escalation_blocked"
            return {
                "group_id": group["group_id"],
                "issue_count": len(issues),
                "blocking_count": len(blocking),
                "provider_escalation_blocked": True,
                "requires_new_paid_grant": False,
            }
        escalation["status"] = "verified"
        escalation["verified_at"] = _now()
        state["repair_plan"] = {
            **copy.deepcopy(state.get("repair_plan", {})),
            "status": "provider_escalation_verified",
            "requires_new_paid_grant": False,
            "maximum_paid_calls": 0,
        }
    scale_reconstruction_blocked = (
        _close_blocked_post_transfer_scale_evidence_reconstruction(
            state, group, group_state, blocking, "scene_group_review"
        )
    )
    if scale_reconstruction_blocked:
        state["stage"] = "manual_review"
        return {
            "group_id": group["group_id"],
            "issue_count": len(issues),
            "blocking_count": len(blocking),
            "scale_evidence_reconstruction_blocked": True,
            "requires_new_paid_grant": False,
        }
    scale_reconstruction = _prepare_post_transfer_scale_evidence_reconstruction(
        state, group, group_state, blocking, "scene_group_review"
    )
    if scale_reconstruction:
        state["stage"] = "manual_review"
        return {
            "group_id": group["group_id"],
            "issue_count": len(issues),
            "blocking_count": len(blocking),
            "scale_evidence_reconstruction_required": True,
            "requires_new_paid_grant": False,
        }
    post_reset_blocked = _close_blocked_post_foundation_reset_transfer(
        state, group, group_state, blocking, "scene_group_review"
    )
    if post_reset_blocked:
        state["stage"] = "manual_review"
        return {
            "group_id": group["group_id"],
            "issue_count": len(issues),
            "blocking_count": len(blocking),
            "post_foundation_reset_transfer_blocked": True,
            "requires_new_paid_grant": False,
        }
    post_reset_transfer = _prepare_post_foundation_reset_transfer(
        state, group, group_state, blocking, "scene_group_review"
    )
    if post_reset_transfer:
        state["stage"] = "manual_review"
        return {
            "group_id": group["group_id"],
            "issue_count": len(issues),
            "blocking_count": len(blocking),
            "post_foundation_reset_transfer_required": True,
            "requires_new_paid_grant": False,
        }
    if _apply_scene_convergence_gate(state, group, group_state, blocking):
        return {
            "group_id": group["group_id"],
            "issue_count": len(issues),
            "blocking_count": len(blocking),
            "non_converging": True,
            "requires_new_paid_grant": False,
        }
    if state.get("vision_recheck_only"):
        group_state["status"] = "blocked" if blocking else "accepted"
        _advance_verification_group(state)
    elif state.get("vision_repair_mode") and blocking:
        group_state["status"] = "blocked"
        _advance_verification_group(state)
    elif blocking and int(group_state.get("retry_count", 0)) < int(state["budgets"]["max_auto_retries"]):
        state["stage"] = "group_retry"
    elif blocking:
        state["stage"] = "manual_review"
    else:
        group_state["status"] = "accepted"
        state["stage"] = "group_finalize"
        scale_reconstruction = group_state.get(
            "post_transfer_scale_evidence_reconstruction", {}
        )
        if (
            isinstance(scale_reconstruction, dict)
            and scale_reconstruction.get("status") == "shot_review"
        ):
            scale_reconstruction["status"] = "verified"
            scale_reconstruction["verified_at"] = _now()
            state["repair_plan"] = {
                **copy.deepcopy(state.get("repair_plan", {})),
                "status": "post_transfer_scale_evidence_reconstruction_verified",
                "requires_new_paid_grant": False,
                "maximum_paid_calls": 0,
            }
        foundation_reset = state.get("foundation_reset", {})
        if (
            isinstance(foundation_reset, dict)
            and foundation_reset.get("status") == "shot_review"
            and str(foundation_reset.get("group_id", "")) == str(group.get("group_id", ""))
        ):
            foundation_reset["status"] = "verified"
            foundation_reset["verified_at"] = _now()
    return {"group_id": group["group_id"], "issue_count": len(issues),
            "blocking_count": len(blocking)}


def _tool_revise_scene_group(state: VisualAgentState, image_provider: ImageProvider) -> dict:
    group = _current_group(state)
    if group is None:
        raise ValueError("no current scene group")
    if not state.get("paid_authorized"):
        state["status"] = "awaiting_approval"
        state["stop_reason"] = "paid_calls_not_authorized"
        return {"revised": 0, "reason": "run again with --image-api"}
    group_state = state["group_states"][group["group_id"]]
    grant_id = str(group_state.get("grant_id", ""))
    if not group_state.get("approved") or not grant_id:
        raise ValueError("scene group retry budget has not been approved")
    technical_retry = (
        group_state.get("technical_retry", {})
        if group_state.get("pending_paid_operation") == "technical_retry" else {}
    )
    technical_shot_ids = set(map(str, technical_retry.get("shot_ids", [])))
    reference_reset_retry = group_state.get("pending_paid_operation") == "reference_reset_retry"
    post_reset_transfer_retry = (
        group_state.get("pending_paid_operation") == "post_foundation_reset_transfer"
    )
    post_reset_transfer = group_state.get("post_foundation_reset_transfer", {})
    post_reset_transfer_shot_ids = set(map(
        str,
        post_reset_transfer.get("shot_ids", [])
        if isinstance(post_reset_transfer, dict) else [],
    ))
    scale_evidence_retry = (
        group_state.get("pending_paid_operation") == "scale_evidence_reconstruction"
    )
    scale_evidence_reconstruction = group_state.get(
        "post_transfer_scale_evidence_reconstruction", {}
    )
    scale_evidence_shot_ids = set(map(
        str,
        scale_evidence_reconstruction.get("shot_ids", [])
        if isinstance(scale_evidence_reconstruction, dict) else [],
    ))
    provider_escalation_retry = (
        group_state.get("pending_paid_operation") == "provider_escalation"
    )
    provider_escalation = group_state.get("provider_escalation", {})
    escalation_tasks_by_shot = {
        str(item.get("shot_id", "")): item
        for item in provider_escalation.get("tasks", [])
        if isinstance(provider_escalation, dict)
        and isinstance(item, dict)
        and str(item.get("shot_id", ""))
    }
    foundation_reset = state.get("foundation_reset", {})
    reset_applies_to_group = bool(
        isinstance(foundation_reset, dict)
        and str(foundation_reset.get("group_id", "")) == str(group.get("group_id", ""))
        and int(foundation_reset.get("shot_revision_round", 0) or 0) > 0
    )
    reference_reset_shot_ids = (
        set(map(str, group_state.get("affected_shot_ids", [])))
        if reference_reset_retry else set()
    )
    failed_jobs_by_shot = (
        technical_retry.get("failed_jobs_by_shot", {})
        if isinstance(technical_retry.get("failed_jobs_by_shot"), dict) else {}
    )
    blocking = [item for item in group_state.get("issues", []) if item.get("blocking")]
    by_shot: dict[str, list[dict]] = {}
    for issue in blocking:
        shot_key = str(issue.get("shot_id", ""))
        escalation_task = escalation_tasks_by_shot.get(shot_key)
        escalation_technical_retry = bool(
            technical_shot_ids
            and str(failed_jobs_by_shot.get(shot_key, {}).get("revision_strategy", ""))
            == "constraint_isolated_edit"
        )
        if provider_escalation_retry or escalation_technical_retry:
            if (
                not isinstance(escalation_task, dict)
                or str(issue.get("issue_id", ""))
                != str(escalation_task.get("active_issue_id", ""))
            ):
                continue
        if technical_shot_ids and str(issue.get("shot_id", "")) not in technical_shot_ids:
            continue
        if reference_reset_shot_ids and str(issue.get("shot_id", "")) not in reference_reset_shot_ids:
            continue
        if (
            post_reset_transfer_retry
            and str(issue.get("shot_id", "")) not in post_reset_transfer_shot_ids
        ):
            continue
        if (
            scale_evidence_retry
            and str(issue.get("shot_id", "")) not in scale_evidence_shot_ids
        ):
            continue
        by_shot.setdefault(issue["shot_id"], []).append(issue)
    if not by_shot:
        if (
            technical_shot_ids or reference_reset_shot_ids
            or post_reset_transfer_retry or scale_evidence_retry
            or provider_escalation_retry
        ):
            raise ValueError("targeted retry is missing the original blocking issue evidence")
        state["stage"] = "group_finalize"
        return {"revised": 0}
    current_retry_count = int(group_state.get("retry_count", 0) or 0)
    local_retry_count = (
        current_retry_count if technical_shot_ids else
        1 if reference_reset_retry else
        current_retry_count + 1
    )
    retry_number = (
        int(technical_retry.get("revision_round", 0) or 0)
        if technical_shot_ids else
        int(foundation_reset.get("shot_revision_round", 0) or 0)
        if reference_reset_retry else
        _next_scene_revision_round(state, str(group["group_id"]), group_state)
    )
    if retry_number < 1:
        raise ValueError("scene group retry round is invalid")
    shots = {item["shot_id"]: item for item in group["shots"]}
    revised = 0
    jobs: list[dict] = []
    for shot_id, shot_issues in by_shot.items():
        shot = shots.get(shot_id)
        if not shot:
            continue
        output_path = os.path.join(
            state["output_dir"], "assets", "shots", state["run_id"],
            safe_filename(group["group_id"]),
            f"shot_{safe_filename(shot_id)}_retry{retry_number:02d}_{state['run_id'][:8]}.png",
        )
        asset_ids = shot.get("reference_asset_ids", group["reference_asset_ids"])
        original_failed_job = (
            failed_jobs_by_shot.get(shot_id, {}) if technical_shot_ids else {}
        )
        constraint_isolation_task = (
            copy.deepcopy(escalation_tasks_by_shot.get(shot_id, {}))
            if provider_escalation_retry
            or str(original_failed_job.get("revision_strategy", ""))
            == "constraint_isolated_edit"
            else {}
        )
        if constraint_isolation_task and (
            str(constraint_isolation_task.get("active_issue_id", ""))
            != str(shot_issues[0].get("issue_id", ""))
        ):
            raise ValueError("provider escalation active issue no longer matches reviewed evidence")
        contract = _correction_contract(str(group["group_id"]), shot_id, shot_issues)
        stored_contract = original_failed_job.get("correction_contract", {})
        if (
            isinstance(stored_contract, dict)
            and str(stored_contract.get("correction_contract_id", "")).startswith("constraint_")
            and str(stored_contract.get("correction_contract_id", ""))
            == str(original_failed_job.get("correction_contract_id", ""))
        ):
            contract = copy.deepcopy(stored_contract)
        contract_attempt_number = (
            int(original_failed_job.get("revision_attempt_number", 0) or 0)
            if technical_shot_ids else _next_contract_attempt_number(
                group_state, shot_id, contract["correction_contract_id"]
            )
        )
        if reference_reset_shot_ids:
            contract_attempt_number = 1
        if contract_attempt_number < 1:
            raise ValueError(f"technical retry is missing its original attempt number: {shot_id}")
        scale_evidence_strategy = scale_evidence_retry or bool(
            technical_shot_ids
            and str(original_failed_job.get("revision_strategy", ""))
            == "scale_evidence_priority_reconstruction"
        )
        reference_policy = _revision_reference_strategy(
            state, asset_ids, shot, shot_issues, contract_attempt_number,
            post_reset_transfer_retry or bool(
                technical_shot_ids
                and str(original_failed_job.get("revision_strategy", ""))
                == "post_reset_locked_assets_transfer"
            ),
            scale_evidence_strategy,
            constraint_isolation_task or None,
        )
        instructions = [
            issue.get("instruction") or issue.get("problem", "")
            for issue in shot_issues
        ]
        prompt = _shot_prompt(
            state, group, shot, instructions, reference_policy
        )
        previous_relative = str(group_state.get("generated", {}).get(shot_id, ""))
        previous_path = os.path.join(state["output_dir"], previous_relative)
        if not previous_relative or not os.path.isfile(previous_path):
            raise ValueError(f"previous generated shot is missing for targeted revision: {shot_id}")
        temporal_context = _adjacent_shot_references(
            state, str(group.get("group_id", "")), shot_id
        )
        references, revision_metadata = _revision_provider_references(
            state, asset_ids,
            f"{group['group_id']}_{safe_filename(shot_id)}_retry{retry_number:02d}",
            previous_path,
            temporal_context,
            shot,
            shot_issues,
            contract_attempt_number,
            post_reset_transfer_retry or bool(
                technical_shot_ids
                and str(original_failed_job.get("revision_strategy", ""))
                == "post_reset_locked_assets_transfer"
            ),
            scale_evidence_strategy,
            constraint_isolation_task or None,
        )
        jobs.append({
            "job_id": str(original_failed_job.get("logical_job_id", "")) or (
                f"retry:{group['group_id']}:r{retry_number:02d}:{shot_id}"
            ),
            "shot_id": shot_id, "group_id": group["group_id"], "prompt": prompt,
            "operation_kind": "shot_revision",
            "revision_attempt_number": contract_attempt_number,
            "revision_strategy": str(reference_policy.get("primary_role", "")),
            "correction_contract_id": contract["correction_contract_id"],
            "correction_contract": contract,
            "constraint_isolation_task": constraint_isolation_task,
            "provider_escalation_task_id": str(
                constraint_isolation_task.get("task_id", "")
            ),
            "deferred_issue_ids": list(
                map(str, constraint_isolation_task.get("deferred_issue_ids", []))
            ),
            "visible_character_ids": list(shot.get("visible_character_ids", [])),
            "visible_prop_ids": list(shot.get("visible_prop_ids", [])),
            "reference_asset_ids": list(shot.get("reference_asset_ids", [])),
            "output_path": output_path, "references": references, "size": state["size"],
            **revision_metadata,
        })
    failures: list[str] = []
    for item in _run_paid_image_jobs(
        state, image_provider, jobs, int(state["budgets"].get("image_parallelism", 4)),
        grant_id,
    ):
        result = item.get("result")
        strategy = str(
            item.get("revision_strategy")
            or item.get("reference_strategy", {}).get("primary_role", "")
        )
        provider_attempted = bool(item.get("provider_attempted"))
        attempt = {
            "attempt_id": content_fingerprint(
                state.get("run_id", ""), item.get("job_id", ""), grant_id,
                item.get("ledger_job_id", ""), length=24,
            ),
            "logical_job_id": str(item.get("job_id", "")),
            "ledger_job_id": str(item.get("ledger_job_id", "")),
            "grant_id": grant_id,
            "group_id": str(item.get("group_id", "")),
            "shot_id": str(item.get("shot_id", "")),
            "revision_attempt_number": int(item.get("revision_attempt_number", 0) or 0),
            "strategy": strategy,
            "correction_contract_id": str(item.get("correction_contract_id", "")),
            "correction_contract": copy.deepcopy(item.get("correction_contract", {})),
            "provider_attempted": provider_attempted,
            "provider_outcome": "recovered" if item.get("recovered") else (
                "succeeded" if result else "failed" if provider_attempted else "not_started"
            ),
            "error": str(item.get("error", "")),
            "artifact_path": "",
            "artifact_sha256": "",
            "recorded_at": _now(),
        }
        if not result:
            failures.append(f"{item['shot_id']}: {item.get('error', 'failed')}")
            _append_revision_attempt(group_state, attempt)
            continue
        relative_result = os.path.relpath(result, state["output_dir"])
        group_state["generated"][item["shot_id"]] = relative_result
        attempt["artifact_path"] = relative_result
        attempt["artifact_sha256"] = _file_fingerprint(str(result))
        _append_revision_attempt(group_state, attempt)
        revised += 1
    if failures:
        group_state["approved"] = False
        group_state["retry_count"] = max(current_retry_count, local_retry_count)
        if reset_applies_to_group:
            foundation_reset["shot_retry_count"] = group_state["retry_count"]
        if (
            isinstance(post_reset_transfer, dict)
            and any(
                str(item.get("revision_strategy", ""))
                == "post_reset_locked_assets_transfer" for item in jobs
            )
        ):
            post_reset_transfer["status"] = "technical_retry"
        if (
            isinstance(scale_evidence_reconstruction, dict)
            and any(
                str(item.get("revision_strategy", ""))
                == "scale_evidence_priority_reconstruction" for item in jobs
            )
        ):
            scale_evidence_reconstruction["status"] = "technical_retry"
        if (
            isinstance(provider_escalation, dict)
            and any(
                str(item.get("revision_strategy", "")) == "constraint_isolated_edit"
                for item in jobs
            )
        ):
            provider_escalation["status"] = "technical_retry"
        transition = _prepare_technical_retry_transition(
            state, "revision_provider_failure"
        )
        if transition is None:
            group_state["status"] = "blocked"
            group_state["pending_paid_operation"] = ""
            state["status"] = "needs_review"
            state["stop_reason"] = "revision_failure_without_ledger_evidence"
            state["stage"] = "revision_failure_without_ledger_evidence"
            state["pending_approval"] = {}
            _sync_blocked_quality_gate(state)
            return {
                "group_id": group["group_id"], "revised": revised,
                "failed": failures[:20], "technical_failure": True,
                "requires_new_paid_approval": False,
            }
        return {
            "group_id": group["group_id"], "revised": revised,
            "failed": failures[:20], "technical_failure": True,
            "requires_new_paid_approval": True,
            "technical_retry_shot_ids": transition["failed_shot_ids"],
            "maximum_paid_calls": transition["missing_paid_calls"],
        }
    group_state["retry_count"] = max(current_retry_count, local_retry_count)
    if reset_applies_to_group:
        foundation_reset["shot_retry_count"] = group_state["retry_count"]
    group_state["status"] = "revised"
    group_state["pending_paid_operation"] = ""
    group_state.pop("technical_retry", None)
    if reference_reset_retry and isinstance(foundation_reset, dict):
        foundation_reset["status"] = "shot_review"
        foundation_reset["regenerated_shot_ids"] = sorted(reference_reset_shot_ids)
        foundation_reset["regenerated_at"] = _now()
    if (
        isinstance(provider_escalation, dict)
        and any(
            str(item.get("revision_strategy", "")) == "constraint_isolated_edit"
            for item in jobs
        )
    ):
        regenerated = set(map(str, provider_escalation.get("regenerated_shot_ids", [])))
        regenerated.update(str(item.get("shot_id", "")) for item in jobs)
        provider_escalation["status"] = "shot_review"
        provider_escalation["regenerated_shot_ids"] = sorted(
            shot_id for shot_id in regenerated if shot_id
        )
        provider_escalation["regenerated_at"] = _now()
        state["repair_plan"] = {
            **copy.deepcopy(state.get("repair_plan", {})),
            "status": "provider_escalation_review",
            "requires_new_paid_grant": False,
        }
    if (
        isinstance(post_reset_transfer, dict)
        and any(
            str(item.get("revision_strategy", "")) == "post_reset_locked_assets_transfer"
            for item in jobs
        )
    ):
        regenerated = set(map(str, post_reset_transfer.get("regenerated_shot_ids", [])))
        regenerated.update(str(item.get("shot_id", "")) for item in jobs)
        post_reset_transfer["status"] = "shot_review"
        post_reset_transfer["regenerated_shot_ids"] = sorted(
            shot_id for shot_id in regenerated if shot_id
        )
        post_reset_transfer["regenerated_at"] = _now()
        state["repair_plan"] = {
            **copy.deepcopy(state.get("repair_plan", {})),
            "status": "post_foundation_reset_transfer_review",
            "requires_new_paid_grant": False,
        }
    if (
        isinstance(scale_evidence_reconstruction, dict)
        and any(
            str(item.get("revision_strategy", ""))
            == "scale_evidence_priority_reconstruction" for item in jobs
        )
    ):
        regenerated = set(map(str, scale_evidence_reconstruction.get("regenerated_shot_ids", [])))
        regenerated.update(str(item.get("shot_id", "")) for item in jobs)
        scale_evidence_reconstruction["status"] = "shot_review"
        scale_evidence_reconstruction["regenerated_shot_ids"] = sorted(
            shot_id for shot_id in regenerated if shot_id
        )
        scale_evidence_reconstruction["regenerated_at"] = _now()
        state["repair_plan"] = {
            **copy.deepcopy(state.get("repair_plan", {})),
            "status": "post_transfer_scale_evidence_reconstruction_review",
            "requires_new_paid_grant": False,
        }
    _mark_partial_revision_review_stale(
        state, group_state, [str(item.get("shot_id", "")) for item in jobs]
    )
    state["stage"] = "group_review"
    return {"group_id": group["group_id"], "revised": revised, "retry_number": retry_number,
            "parallelism": state["budgets"].get("image_parallelism", 4)}


def _attach_group_images(state: VisualAgentState, group: dict) -> None:
    generated = state["group_states"][group["group_id"]].get("generated", {})
    shot_map = {
        str(shot.get("shot_id", "")): shot
        for scene in state["storyboard"].get("scenes", []) for shot in scene.get("shots", [])
    }
    for shot_id, relative in generated.items():
        path = os.path.join(state["output_dir"], relative)
        if shot_id not in shot_map or not os.path.isfile(path) or os.path.getsize(path) < 1:
            raise ValueError(f"cannot attach invalid shot image: {shot_id}")
        shot_map[shot_id].setdefault("assets", {})["image"] = os.path.relpath(
            path, os.path.dirname(os.path.abspath(state["storyboard_path"]))
        )
        shot_map[shot_id].setdefault("status", {})["image"] = "completed"


def _tool_finalize_scene_group(state: VisualAgentState) -> dict:
    group = _current_group(state)
    if group is None:
        state["stage"] = "ready_finalize"
        return {"complete": True}
    group_state = state["group_states"][group["group_id"]]
    if group_state.get("status") != "accepted":
        raise ValueError("scene group is not accepted")
    _attach_group_images(state, group)
    group_state["finalized_at"] = _now()
    foundation_reset = state.get("foundation_reset", {})
    if (
        isinstance(foundation_reset, dict)
        and foundation_reset.get("status") == "verified"
        and str(foundation_reset.get("group_id", "")) == str(group.get("group_id", ""))
    ):
        foundation_reset["status"] = "completed"
        foundation_reset["completed_at"] = _now()
    state["current_group_index"] = int(state.get("current_group_index", 0)) + 1
    if state.get("vision_repair_mode"):
        repair_group_ids = set(map(str, state.get("repair_group_ids", [])))
        while state["current_group_index"] < len(state["scene_groups"]):
            candidate = state["scene_groups"][state["current_group_index"]]
            if str(candidate.get("group_id", "")) in repair_group_ids:
                break
            state["current_group_index"] += 1
    state["stage"] = (
        "group_approval" if state["current_group_index"] < len(state["scene_groups"])
        else "ready_finalize"
    )
    return {"group_id": group["group_id"], "accepted": True}


def _tool_request_manual_review(state: VisualAgentState) -> dict:
    group = _current_group(state)
    if group is None:
        raise ValueError("no current scene group")
    group_state = state["group_states"][group["group_id"]]
    issues = group_state.get("issues", [])
    blocking_issue_ids = [
        str(issue.get("issue_id")) for issue in issues
        if isinstance(issue, dict) and issue.get("blocking") is True
        and str(issue.get("issue_id", "")).strip()
    ]
    transfer = group_state.get("post_foundation_reset_transfer", {})
    transfer_status = str(transfer.get("status", "")) if isinstance(transfer, dict) else ""
    transfer_required = bool(
        isinstance(transfer, dict) and transfer.get("status") == "required"
    )
    transfer_blocked = transfer_status == "blocked"
    scale_reconstruction = group_state.get(
        "post_transfer_scale_evidence_reconstruction", {}
    )
    scale_status = (
        str(scale_reconstruction.get("status", ""))
        if isinstance(scale_reconstruction, dict) else ""
    )
    scale_required = scale_status == "required"
    scale_blocked = scale_status == "blocked"
    _write_approval_request(
        state, f"manual_review_{group['group_id']}", group["shot_ids"], 0,
        {"issues": issues, "blocking_issue_ids": blocking_issue_ids,
         "allowed_decisions": (
             ["regenerate", "reject"]
             if scale_required or transfer_required else
             ["reject"] if scale_blocked or transfer_blocked else []
         ),
         "post_foundation_reset_transfer": (
             copy.deepcopy(transfer) if transfer_required or transfer_blocked else {}
         ),
         "post_transfer_scale_evidence_reconstruction": (
             copy.deepcopy(scale_reconstruction) if scale_required or scale_blocked else {}
         ),
         "vision_available": group_state.get("vision_available", False),
         "unverified_checks": ([] if group_state.get("vision_available") is True else [
             "character identity and visible cast", "prop identity and state continuity",
             "location topology and entrance/exit continuity", "storyboard action and composition",
             "unwanted readable text or glyph-like pseudo-writing",
         ]),
          "instructions": (
              "Inspect every current blocking issue and image fingerprint. A single distinct "
              "scale-evidence-priority reconstruction is available; it keeps failed and adjacent shots "
              "excluded and makes the locked full-comparator asset dominant. Choose regenerate to request "
              "only the listed shot calls, or reject to stop. Blocking issues cannot be overridden."
              if scale_required else
              "The reviewed scale-evidence-priority reconstruction remains blocked. It cannot be overridden "
              "or repeated without another distinct approved strategy; choose reject and stop for diagnosis."
              if scale_blocked else
              "Inspect every listed check and image fingerprint. Post-Foundation-reset blocking issues cannot "
              "be overridden; choose regenerate to request the locked-assets-only transfer, or reject to stop."
              if transfer_required else
              "The reviewed locked-assets-only transfer remains blocked. It cannot be overridden or repeated "
              "without a distinct approved strategy; choose reject and stop for diagnosis."
              if transfer_blocked else
              "Inspect every listed check and image fingerprint. Approve with a specific non-empty "
              "override_reason; when blocking issues exist, also provide one issue_override_reasons entry for "
              "every blocking issue ID. Otherwise choose regenerate."
          )},
    )
    return {"group_id": group["group_id"], "issue_count": len(group_state.get("issues", []))}


def _build_vision_repair_plan(state: VisualAgentState, blocking: list[dict]) -> dict:
    groups: list[dict] = []
    for group in state.get("scene_groups", []):
        group_id = str(group.get("group_id", ""))
        issues = [
            copy.deepcopy(issue) for issue in blocking
            if str(issue.get("group_id", "")) == group_id
        ]
        shot_ids = sorted({
            str(issue.get("shot_id", "")) for issue in issues
            if str(issue.get("shot_id", ""))
        })
        if shot_ids:
            groups.append({
                "group_id": group_id,
                "shot_ids": shot_ids,
                "maximum_paid_calls": len(shot_ids),
                "issues": issues,
            })
    return {
        "schema_version": "1.0",
        "status": "proposed" if groups else "none",
        "source_run_id": state.get("run_id", ""),
        "created_at": _now(),
        "requires_new_paid_grant": bool(groups),
        "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
        "maximum_paid_calls": sum(item["maximum_paid_calls"] for item in groups),
        "group_ids": [item["group_id"] for item in groups],
        "shot_ids": sorted({shot_id for item in groups for shot_id in item["shot_ids"]}),
        "groups": groups,
    }


def _apply_repair_convergence_gate(state: VisualAgentState, repair_plan: dict) -> dict:
    """Stop repeated paid repairs when the same reference contract is not converging."""
    if not state.get("vision_repair_mode") or repair_plan.get("status") != "proposed":
        return repair_plan
    reviewed = [
        item for item in state.get("repair_history", [])
        if isinstance(item, dict) and item.get("status") == "reviewed"
    ]
    if len(reviewed) < 2:
        return repair_plan

    def issue_count(plan: dict) -> int:
        return len({
            str(issue.get("issue_id", ""))
            for group in plan.get("groups", [])
            for issue in group.get("issues", [])
            if isinstance(issue, dict) and str(issue.get("issue_id", ""))
        })

    previous_counts = [issue_count(item) for item in reviewed[-2:]]
    current_count = issue_count(repair_plan)
    if not previous_counts or current_count < min(previous_counts):
        return repair_plan
    estimated_calls = int(repair_plan.get("maximum_paid_calls", 0))
    repair_plan.update({
        "status": "reference_reset_required",
        "requires_new_paid_grant": False,
        "maximum_paid_calls": 0,
        "estimated_shot_repair_calls_after_reference_reset": estimated_calls,
        "convergence": {
            "status": "not_converging",
            "previous_blocking_issue_counts": previous_counts,
            "current_blocking_issue_count": current_count,
            "reason": (
                "blocking issues did not decrease across the recent repair history; replace or relock "
                "the authoritative foundation reference before any further paid shot regeneration"
            ),
        },
    })
    return repair_plan


def _tool_finalize_vision_recheck(state: VisualAgentState) -> dict:
    state["issues"] = [
        issue for value in state.get("group_states", {}).values()
        for issue in value.get("issues", []) if isinstance(issue, dict)
    ]
    blocking = [issue for issue in state["issues"] if issue.get("blocking") is True]
    unavailable_group_ids = sorted(
        group_id for group_id, value in state.get("group_states", {}).items()
        if value.get("vision_available") is not True
    )
    blocking_ids = sorted(
        str(issue.get("issue_id", "")) for issue in blocking
        if str(issue.get("issue_id", ""))
    )
    previous_plan = state.get("repair_plan")
    if state.get("vision_repair_mode") and isinstance(previous_plan, dict) and previous_plan:
        archived = copy.deepcopy(previous_plan)
        archived.update({
            "status": "reviewed",
            "reviewed_at": _now(),
            "image_calls_after": int(state.get("counters", {}).get("image_calls", 0)),
        })
        state.setdefault("repair_history", []).append(archived)
    if unavailable_group_ids:
        repair_plan = {
            "schema_version": "2.0",
            "status": "verification_incomplete",
            "source_run_id": state.get("run_id", ""),
            "created_at": _now(),
            "requires_new_paid_grant": False,
            "maximum_paid_calls": 0,
            "group_ids": [],
            "shot_ids": [],
            "groups": [],
            "unavailable_group_ids": unavailable_group_ids,
            "observed_blocking_issue_ids": blocking_ids,
            "incomplete_reason": "vision_provider_unavailable",
            "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
        }
    elif state.get("provider_escalation_mode") and blocking:
        previous_strategy = (
            previous_plan.get("strategy", {}) if isinstance(previous_plan, dict) else {}
        )
        repair_plan = {
            "schema_version": "2.0",
            "status": "provider_escalation_blocked",
            "source_run_id": state.get("run_id", ""),
            "created_at": _now(),
            "requires_new_paid_grant": False,
            "maximum_paid_calls": 0,
            "group_ids": sorted({str(item.get("group_id", "")) for item in blocking}),
            "shot_ids": sorted({str(item.get("shot_id", "")) for item in blocking}),
            "blocking_issue_ids": blocking_ids,
            "strategy": copy.deepcopy(previous_strategy),
            "manual_action": (
                "use a provider with the listed capabilities or define a distinct strategy version; "
                "the same provider/model strategy cannot be repeated"
            ),
            "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
        }
    else:
        repair_plan = _build_vision_repair_plan(state, blocking)
        repair_plan = _apply_repair_convergence_gate(state, repair_plan)
    state["repair_plan"] = repair_plan
    if not blocking and not unavailable_group_ids:
        return _tool_finalize(state)

    state["quality_gate"] = {
        "mode": "vision_repair" if state.get("vision_repair_mode") else "vision_recheck",
        "quality_outcome": "blocked" if blocking else "unavailable",
        "passed_without_override": False,
        "accepted": False,
        "automated_review_status": (
            "completed" if not unavailable_group_ids else "partially_unavailable"
        ),
        "automated_review_completed": not bool(unavailable_group_ids),
        "verification_mode": (
            "automated_vision" if not unavailable_group_ids else "partial_vision"
        ),
        "vision_calls": int(state.get("counters", {}).get("vision_calls", 0)),
        "vision_attempts": int(state.get("counters", {}).get("vision_attempts", 0)),
        "vision_failures": int(state.get("counters", {}).get("vision_failures", 0)),
        "vision_failure_history": state.get("vision_failure_history", []),
        "blocking_issue_ids": blocking_ids,
        "blocking_status": "blocked" if blocking else "unknown",
        "observed_blocking_issue_count": len(blocking),
        "blocking_issue_count": len(blocking) if blocking else None,
        "overridden_blocking_issue_count": 0,
        "unavailable_group_ids": unavailable_group_ids,
        "unverified_checks": ([
            "automated_identity_match", "automated_prop_match", "automated_spatial_continuity",
            "automated_text_artifact_detection", "automated_storyboard_image_alignment",
        ] if unavailable_group_ids else []),
    }
    state["status"] = "needs_review"
    if state.get("vision_repair_mode"):
        state["stop_reason"] = (
            "vision_repair_unavailable" if unavailable_group_ids
            else "provider_escalation_blocked"
            if state.get("provider_escalation_mode") and blocking
            else "vision_repair_blocked"
        )
    else:
        state["stop_reason"] = (
            "vision_recheck_unavailable" if unavailable_group_ids
            else "vision_recheck_blocked"
        )
    state["stage"] = state["stop_reason"]
    state["pending_approval"] = {}
    return {
        "blocking_issue_count": len(blocking_ids),
        "unavailable_group_ids": unavailable_group_ids,
        "repair_plan_status": repair_plan["status"],
    }


def _tool_finalize(state: VisualAgentState) -> dict:
    ledger = _load_paid_ledger(state)
    unsettled = [
        str(item.get("logical_job_id", ""))
        for item in ledger.get("jobs", {}).values()
        if isinstance(item, dict) and _is_actionable_paid_entry(item)
        and item.get("status") in {"started", "uncertain", "produced", "publishing"}
    ]
    if unsettled:
        raise ValueError("paid artifact ledger is not closed: " + ", ".join(sorted(set(unsettled))[:20]))
    incomplete = [group_id for group_id, value in state.get("group_states", {}).items()
                  if value.get("status") != "accepted"]
    if incomplete:
        raise ValueError("scene groups are not accepted: " + ", ".join(incomplete))
    errors = validate_storyboard(state["storyboard"])
    if errors:
        raise ValueError("final storyboard is invalid: " + "; ".join(errors[:8]))
    for scene in state["storyboard"].get("scenes", []):
        for shot in scene.get("shots", []):
            relative = shot.get("assets", {}).get("image", "")
            absolute = os.path.join(os.path.dirname(state["storyboard_path"]), relative)
            if not relative or not os.path.isfile(absolute) or os.path.getsize(absolute) < 1:
                raise ValueError(f"final image missing for shot {shot.get('shot_id')}")
    atomic_write_json(state["storyboard_path"], state["storyboard"])
    overrides = {
        group_id: value.get("human_override")
        for group_id, value in state.get("group_states", {}).items()
        if value.get("human_override")
    }
    vision_override_groups = {
        group_id for group_id in overrides
        if state.get("group_states", {}).get(group_id, {}).get("vision_available") is True
    }
    human_only_groups = set(overrides) - vision_override_groups
    overridden_issue_ids = sorted({
        str(issue.get("issue_id"))
        for group_id in vision_override_groups
        for issue in state.get("group_states", {}).get(group_id, {}).get("issues", [])
        if isinstance(issue, dict) and str(issue.get("issue_id", ""))
    })
    issue_override_reasons = {
        str(issue_id): str(reason)
        for override in overrides.values() if isinstance(override, dict)
        for issue_id, reason in override.get("issue_override_reasons", {}).items()
    }
    all_blocking_issue_ids = sorted({
        str(issue.get("issue_id"))
        for value in state.get("group_states", {}).values()
        for issue in value.get("issues", []) if isinstance(issue, dict)
        and issue.get("blocking") is True and str(issue.get("issue_id", ""))
    })
    automated_review_status = (
        "unavailable" if human_only_groups
        else "completed_with_overrides" if vision_override_groups
        else "completed"
    )
    if vision_override_groups and human_only_groups:
        verification_mode = "mixed_vision_and_human_override"
    elif vision_override_groups:
        verification_mode = "vision_with_human_override"
    elif overrides:
        verification_mode = "human_only"
    else:
        verification_mode = "automated_vision"
    state["quality_gate"] = {
        "mode": "human_override" if overrides else "vision_verified",
        "quality_outcome": "overridden" if overrides else "passed",
        "passed_without_override": not bool(overrides),
        "accepted": True,
        "automated_review_status": automated_review_status,
        "automated_review_completed": not bool(human_only_groups),
        "verification_mode": verification_mode,
        "vision_calls": int(state.get("counters", {}).get("vision_calls", 0)),
        "vision_attempts": int(state.get("counters", {}).get("vision_attempts", 0)),
        "vision_failures": int(state.get("counters", {}).get("vision_failures", 0)),
        "vision_failure_history": state.get("vision_failure_history", []),
        "human_overrides": overrides,
        "vision_override_group_ids": sorted(vision_override_groups),
        "human_only_group_ids": sorted(human_only_groups),
        "overridden_issue_ids": overridden_issue_ids,
        "blocking_issue_ids": all_blocking_issue_ids,
        "blocking_status": "unknown" if human_only_groups else (
            "overridden" if all_blocking_issue_ids else "clear"
        ),
        "observed_blocking_issue_count": len(all_blocking_issue_ids),
        "blocking_issue_count": None if human_only_groups else len(all_blocking_issue_ids),
        "overridden_blocking_issue_count": len(set(all_blocking_issue_ids).intersection(overridden_issue_ids)),
        "issue_override_reasons": issue_override_reasons,
        "unverified_checks": ([
            "automated_identity_match", "automated_prop_match", "automated_spatial_continuity",
            "automated_text_artifact_detection", "automated_storyboard_image_alignment",
        ] if human_only_groups else []),
    }
    state["status"] = "completed"
    state["stop_reason"] = "completed_with_manual_override" if overrides else "completed"
    state["stage"] = "completed"
    if state.get("vision_repair_mode") and isinstance(state.get("repair_plan"), dict):
        state["repair_plan"] = {
            **state["repair_plan"],
            "status": "completed",
            "repair_run_id": state.get("run_id", ""),
            "completed_at": _now(),
            "image_calls_after": int(state.get("counters", {}).get("image_calls", 0)),
        }
    return {"shot_count": sum(len(group["shot_ids"]) for group in state["scene_groups"])}


def _recommended_action(state: VisualAgentState) -> str:
    return recommended_visual_command(state)


def _code_validated_stage_action(state: VisualAgentState) -> str:
    """Return the sole production action only when code owns the choice."""
    stage_actions = set(STAGE_ACTIONS.get(str(state.get("stage", "")), set()))
    recommended = recommended_visual_command(state)
    return recommended if len(stage_actions) == 1 and recommended in stage_actions else ""


def _recent_protocol_failures_are_unusable_responses(
    state: VisualAgentState,
    required: int = 3,
) -> bool:
    """Recognize the legacy technical stop without overriding semantic stops."""
    path = _private_trace_path(state)
    if not os.path.isfile(path):
        return False
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("event") == "protocol_error":
                    events.append(event)
    except OSError:
        return False
    recent = events[-required:]
    if len(recent) != required:
        return False
    return all(
        str(event.get("payload", {}).get("action", "")) == ""
        and event.get("payload", {}).get("errors") == ["response must be a JSON object"]
        for event in recent
    )


def _authorization_snapshot(state: VisualAgentState) -> dict:
    """Expose code-owned paid authorization using the durable ledger."""
    ledger = _load_paid_ledger(state)
    group = _current_group(state)
    stage = str(state.get("stage", ""))
    if stage.startswith("foundation_"):
        requested_grant_id = str(state.get("foundation_grant_id", ""))
        primary_grant_id = str(
            state.get("foundation_primary_grant_id", "") or requested_grant_id
        )
        retry_grant_id = str(state.get("foundation_retry_grant_id", ""))
        grant_id = requested_grant_id
        grants = ledger.get("grants", {})
        active = grants.get(grant_id, {}) if grant_id else {}
        active_remaining = int(active.get("maximum_paid_calls", 0) or 0) - int(
            active.get("used_calls", 0) or 0
        )
        # A retry grant authorizes only the failed logical attempt. Once it is
        # exhausted, expose the still-valid primary grant for later phases.
        primary = grants.get(primary_grant_id, {}) if primary_grant_id else {}
        primary_remaining = int(primary.get("maximum_paid_calls", 0) or 0) - int(
            primary.get("used_calls", 0) or 0
        )
        if active_remaining <= 0 and primary_remaining > 0 and stage != "foundation_retry_approval":
            grant_id = primary_grant_id
        scope = "foundation"
    elif group:
        grant_id = str(state.get("group_states", {}).get(group["group_id"], {}).get("grant_id", ""))
        scope = group["group_id"]
    else:
        grant_id = ""
        scope = ""
    grant = ledger.get("grants", {}).get(grant_id, {}) if grant_id else {}
    maximum = int(grant.get("maximum_paid_calls", 0) or 0)
    used = int(grant.get("used_calls", 0) or 0)
    pending = state.get("pending_approval", {})
    pending = pending if isinstance(pending, dict) else {}
    if grant_id and grant:
        approval_state = "active" if used < maximum else "exhausted"
    elif str(pending.get("stage", "")).strip() and str(pending.get("stage", "")) != "none":
        approval_state = "pending"
    else:
        approval_state = "not_requested"
    paid_stage = stage in {"foundation_generate", "group_generate", "group_retry"}
    ledger_grant_valid = bool(grant_id and grant)
    return {
        "paid_cli_authorized": bool(state.get("paid_authorized")),
        "foundation_budget_approved": bool(
            state.get("foundation_budget_approved")
            and grant_id
            and ledger.get("grants", {}).get(grant_id)
        ),
        "active_grant_id": grant_id,
        "approval_scope": scope,
        "grant_stage": grant.get("stage", ""),
        "maximum_paid_calls": maximum,
        "used_calls": used,
        "remaining_calls": max(0, maximum - used),
        "remaining_calls_scope": "active_approval_grant",
        "active_approval_remaining_calls": max(0, maximum - used),
        "approval_state": approval_state,
        "provider_quota_state": "unknown_not_queried",
        "covers_current_foundation_phase": bool(scope == "foundation" and ledger_grant_valid),
        "foundation_primary_grant_id": (
            str(state.get("foundation_primary_grant_id", "")) if scope == "foundation" else ""
        ),
        "foundation_retry_grant_id": (
            str(state.get("foundation_retry_grant_id", "")) if scope == "foundation" else ""
        ),
        "current_paid_action_authorized": bool(
            paid_stage and state.get("paid_authorized") and ledger_grant_valid and used < maximum
        ),
        "ledger_is_source_of_truth": True,
    }


def _paid_tool_trace_accounting(
    state: VisualAgentState, paid_before: dict, paid_after: dict,
) -> dict:
    """Measure one tool against the same durable grant across grant handoffs."""
    charged_grant_id = str(
        paid_before.get("active_grant_id", "") or paid_after.get("active_grant_id", "")
    )
    charged_grant = (
        _load_paid_ledger(state).get("grants", {}).get(charged_grant_id, {})
        if charged_grant_id else {}
    )
    charged_before = int(paid_before.get("used_calls", 0) or 0)
    charged_after = int(charged_grant.get("used_calls", charged_before) or 0)
    charged_maximum = int(charged_grant.get("maximum_paid_calls", 0) or 0)
    return {
        "paid_action": charged_after > charged_before,
        "paid_grant_id": charged_grant_id,
        "paid_calls_before": charged_before,
        "paid_calls_after": charged_after,
        "paid_calls_remaining": max(0, charged_maximum - charged_after),
        "paid_calls_remaining_in_active_grant": max(0, charged_maximum - charged_after),
        "active_grant_after": paid_after.get("active_grant_id", ""),
        "active_grant_remaining_after": paid_after.get("remaining_calls", 0),
        "approval_state": paid_after.get("approval_state", "not_requested"),
    }


def _normalize_recoverable_paid_state(state: VisualAgentState) -> dict | None:
    """Move incomplete work off an exhausted grant without consulting the LLM."""
    stage = str(state.get("stage", ""))
    if stage not in {"foundation_generate", "group_generate", "group_retry"}:
        return None
    authorization = _authorization_snapshot(state)
    if authorization.get("approval_state") != "exhausted":
        return None
    previous_status = str(state.get("status", ""))
    previous_reason = str(state.get("stop_reason", ""))
    transition: dict[str, Any]
    if stage == "foundation_generate":
        phase, assets = _current_phase_assets(state)
        target = int(state.get("budgets", {}).get("foundation_candidates", 0))
        missing = sum(max(0, target - sum(
            1 for candidate in state.get("candidates", {}).get(asset["asset_id"], [])
            if _candidate_is_published(state, candidate)
        )) for asset in assets)
        if missing <= 0:
            return None
        state["foundation_budget_approved"] = False
        state["foundation_retry_grant_id"] = ""
        state["stage"] = "foundation_retry_approval"
        transition = {"scope": "foundation", "phase": phase, "missing_paid_calls": missing}
    else:
        group = _current_group(state)
        if not group:
            return None
        group_state = state.setdefault("group_states", {}).setdefault(group["group_id"], {})
        if stage == "group_generate":
            missing = sum(
                1 for shot_id in group.get("shot_ids", [])
                if not os.path.isfile(os.path.join(
                    state["output_dir"], str(group_state.get("generated", {}).get(shot_id, ""))
                ))
            )
            group_state["pending_paid_operation"] = "initial"
            group_state["status"] = "planned"
        else:
            if group_state.get("pending_paid_operation") == "technical_retry":
                technical_retry = group_state.get("technical_retry", {})
                missing = len(technical_retry.get("shot_ids", []))
            else:
                missing = max(1, len(group_state.get("affected_shot_ids", [])))
                group_state["pending_paid_operation"] = "retry"
        if missing <= 0:
            return None
        group_state["approved"] = False
        state["stage"] = "group_approval"
        transition = {
            "scope": group["group_id"], "operation": stage,
            "missing_paid_calls": missing,
        }
    state["status"] = "running"
    state["stop_reason"] = ""
    state["pending_approval"] = {}
    state["action"] = ""
    state["action_args"] = {}
    state["no_progress"] = 0
    transition.update({
        "previous_status": previous_status,
        "previous_stop_reason": previous_reason,
        "previous_grant_id": authorization.get("active_grant_id", ""),
    })
    _trace(state, "exhausted_grant_transition", transition)
    return transition


def _supervisor_snapshot(state: VisualAgentState) -> dict:
    group = _current_group(state)
    stage_actions = set(STAGE_ACTIONS.get(state.get("stage", ""), set()))
    stage_actions.update({"retrieve_playbook", "stop_needs_review"})
    phase = (
        FOUNDATION_PHASES[int(state.get("foundation_phase_index", 0))]
        if int(state.get("foundation_phase_index", 0)) < len(FOUNDATION_PHASES) else "complete"
    )
    return {
        "stage": state.get("stage"), "status": state.get("status"),
        "recommended_action": _recommended_action(state),
        "allowed_actions": sorted(stage_actions),
        "foundation_phase": phase,
        "current_phase_item_count": len([
            item for item in state.get("foundation_assets", []) if item.get("phase") == phase
        ]),
        "foundation_asset_count": len(state.get("foundation_assets", [])),
        "locked_asset_ids": sorted(state.get("locked_assets", {})),
        "current_scene_group": group,
        "current_issues": state.get("group_states", {}).get(group["group_id"], {}).get("issues", []) if group else [],
        "counters": state.get("counters", {}), "budgets": state.get("budgets", {}),
        "authorization": _authorization_snapshot(state),
        "pending_approval": state.get("pending_approval", {}),
        "hard_rules": get_playbook_sections(["foundation", "shot_generation", "approval"], ["hard_gate"]),
    }


def _call_supervisor(state: VisualAgentState,
                     supervisor_provider: SupervisorProvider | None) -> dict | None:
    snapshot = _supervisor_snapshot(state)
    if supervisor_provider is not None:
        raw = supervisor_provider(snapshot)
    else:
        raw = call_llm(
            """You are the supervisor of a visual production Agent. Choose exactly one useful tool.
Return JSON only: {"action":"tool_name","params":{},"summary":"brief decision"}.
Never claim human approval. Never call paid generation before approval. Do not reveal chain of thought.
Unknown fields and unknown tool names are protocol errors. Choose only from allowed_actions for the
current stage. The recommended action is code-validated. When authorization.current_paid_action_authorized
is true, do not stop because approval evidence is missing; the durable ledger is authoritative. Use
stop_needs_review only with a specific reason, reason_code and current evidence. Do not infer paid-call
            facts from your summary. authorization.remaining_calls is scoped only to the active local approval
grant. Zero means not yet approved or that grant is exhausted; it never proves provider account quota is
exhausted. Follow recommended_action when a new approval request is the useful next step.""",
            json.dumps(snapshot, ensure_ascii=False), max_tokens=800, temperature=0,
        )
    state["counters"]["model_calls"] += 1
    usage = state.setdefault("run_budget_usage", {})
    usage["model_calls"] = int(usage.get("model_calls", 0)) + 1
    if isinstance(raw, str):
        return parse_json_response(raw)
    return raw if isinstance(raw, dict) else None


def _normalize_supervisor_action(response: Any) -> tuple[str, dict, str, list[str], list[str], list[str]]:
    """Normalize common provider-neutral JSON aliases and report exact errors."""
    if not isinstance(response, dict):
        return "", {}, "", [], [], ["response must be a JSON object"]
    raw = dict(response)
    action = str(
        raw.get("action") or raw.get("tool") or raw.get("tool_name") or ""
    ).strip()
    summary = str(
        raw.get("summary") or raw.get("decision_summary") or raw.get("message") or ""
    ).strip()[:500]
    params_value: Any = {}
    for key in ("params", "args", "arguments", "parameters"):
        if key in raw:
            params_value = raw[key]
            break
    parameter_keys = sorted(str(key) for key in params_value) if isinstance(params_value, dict) else []
    params = dict(params_value) if isinstance(params_value, dict) else {}
    errors: list[str] = []
    if not isinstance(params_value, dict):
        errors.append("params/args must be a JSON object")

    if action == "stop_needs_review":
        # Providers frequently emit stop evidence at the top level or use
        # function-calling-style field names despite the JSON-only contract.
        for key in ("reason", "reason_code", "evidence"):
            if key in raw and key not in params:
                params[key] = raw[key]
        aliases = {
            "stop_reason": "reason", "message": "reason", "details": "reason",
            "reasonCode": "reason_code", "code": "reason_code",
            "current_evidence": "evidence", "evidences": "evidence", "context": "evidence",
        }
        for alias, canonical in aliases.items():
            if alias in params and canonical not in params:
                params[canonical] = params[alias]
            params.pop(alias, None)
        if isinstance(params.get("reason"), (dict, list)):
            params["reason"] = json.dumps(params["reason"], ensure_ascii=False)
        if isinstance(params.get("evidence"), str):
            params["evidence"] = [params["evidence"]]
        params.setdefault("reason_code", "model_requested_review")
        params.setdefault("evidence", [])

    action_alias_keys = {"action", "tool", "tool_name"}
    params_alias_keys = {"params", "args", "arguments", "parameters"}
    summary_alias_keys = {"summary", "decision_summary", "message"}
    direct_stop_keys = {"reason", "reason_code", "evidence"} if action == "stop_needs_review" else set()
    unknown = sorted(
        set(raw) - action_alias_keys - params_alias_keys - summary_alias_keys - direct_stop_keys
    )
    if unknown:
        errors.append("unknown top-level fields: " + ", ".join(unknown))
    if action not in ALLOWED_ACTIONS:
        errors.append("action is not in the tool whitelist")
    if action == "stop_needs_review":
        unknown_params = sorted(set(params) - {"reason", "reason_code", "evidence"})
        if unknown_params:
            errors.append("unknown stop parameters: " + ", ".join(unknown_params))
        if len(str(params.get("reason", "")).strip()) < 4:
            errors.append("stop reason must contain at least 4 characters")
    elif params:
        errors.append("this action does not accept parameters")
    return action, params, summary, unknown, parameter_keys, errors


def _supervisor_node(state: VisualAgentState,
                     supervisor_provider: SupervisorProvider | None) -> dict:
    if state.get("status") != "running":
        return state
    fallback_action = _code_validated_stage_action(state)
    # Production routing is a deterministic state-machine concern.  An
    # injected supervisor remains available as a legacy protocol test seam,
    # but the default path spends no model call on choosing the next tool.
    if supervisor_provider is None and fallback_action:
        state["action"] = fallback_action
        state["action_args"] = {}
        state["decision_summary"] = "Deterministic code-owned workflow routing."
        _trace(state, "deterministic_route", {
            "action": fallback_action,
            "stage": state.get("stage", ""),
            "model_calls_made": 0,
        })
        return state
    if state.get("supervisor_unavailable_this_invocation") and fallback_action:
        state["action"] = fallback_action
        state["action_args"] = {}
        state["decision_summary"] = "Code-validated fallback after supervisor provider became unavailable."
        _trace(state, "supervisor_fallback", {
            "action": fallback_action,
            "reason": "provider_unavailable_this_invocation",
            "allowed_actions": sorted(STAGE_ACTIONS.get(state.get("stage", ""), set())),
        })
        return state
    usage = state.setdefault("run_budget_usage", {"model_calls": 0, "tool_steps": 0})
    if int(usage.get("model_calls", 0)) >= int(state["budgets"]["effective_max_calls"]):
        state["status"] = "needs_review"
        state["stop_reason"] = "model_budget_exhausted"
        _trace(state, "budget_exhausted", {
            "kind": "model_calls",
            "run_used": int(usage.get("model_calls", 0)),
            "usage_scope": "current_invocation",
            "cumulative_used": int(state.get("counters", {}).get("model_calls", 0)),
        })
        return state
    response = _call_supervisor(state, supervisor_provider)
    action, params, summary, unknown, parameter_keys, protocol_errors = (
        _normalize_supervisor_action(response)
    )
    if response is None and fallback_action:
        state["supervisor_unavailable_this_invocation"] = True
        state["invalid_actions"] = 0
        state["action"] = fallback_action
        state["action_args"] = {}
        state["decision_summary"] = "Code-validated fallback after an unusable supervisor response."
        _trace(state, "supervisor_fallback", {
            "action": fallback_action,
            "reason": "response_not_json_object",
            "protocol_errors": protocol_errors,
            "allowed_actions": sorted(STAGE_ACTIONS.get(state.get("stage", ""), set())),
        })
        return state
    if protocol_errors:
        state["invalid_actions"] = int(state.get("invalid_actions", 0)) + 1
        _trace(state, "protocol_error", {
            "action": action,
            "parameter_keys": parameter_keys,
            "normalized_params": params,
            "unknown_fields": unknown,
            "errors": protocol_errors,
        })
        if state["invalid_actions"] >= 3:
            state["status"] = "needs_review"
            state["stop_reason"] = "three_invalid_actions"
        return state
    state["action"] = action
    state["action_args"] = params
    state["decision_summary"] = summary
    _trace(state, "supervisor_action", {
        "action": action, "params": params, "summary": state["decision_summary"],
        "authorization": _authorization_snapshot(state),
    })
    return state


def _execute_tool_node(state: VisualAgentState, image_provider: ImageProvider,
                       vision_provider: VisionProvider) -> dict:
    if state.get("status") != "running":
        return state
    usage = state.setdefault("run_budget_usage", {"model_calls": 0, "tool_steps": 0})
    if int(usage.get("tool_steps", 0)) >= int(state["budgets"]["effective_max_steps"]):
        state["status"] = "needs_review"
        state["stop_reason"] = "tool_budget_exhausted"
        _trace(state, "budget_exhausted", {
            "kind": "tool_steps",
            "run_used": int(usage.get("tool_steps", 0)),
            "usage_scope": "current_invocation",
            "cumulative_used": int(state.get("counters", {}).get("tool_steps", 0)),
        })
        return state
    action = state.get("action", "")
    stage_allowed = STAGE_ACTIONS.get(state.get("stage", ""), set())
    if action not in stage_allowed and action not in {"retrieve_playbook", "stop_needs_review"}:
        state["invalid_actions"] = int(state.get("invalid_actions", 0)) + 1
        _trace(state, "recoverable_tool_error", {
            "action": action, "stage": state.get("stage"),
            "allowed": sorted(stage_allowed),
        })
        if state["invalid_actions"] >= 3:
            state["status"] = "needs_review"
            state["stop_reason"] = "three_invalid_tool_actions"
        state["action"] = ""
        state["action_args"] = {}
        return state
    before = _state_progress(state)
    paid_before = _authorization_snapshot(state)
    tools: dict[str, Callable[[], dict]] = {
        "inspect_storyboard": lambda: _tool_inspect_storyboard(state),
        "retrieve_playbook": lambda: _tool_retrieve_playbook(state),
        "build_visual_bible": lambda: _tool_build_visual_bible(state),
        "request_foundation_approval": lambda: _tool_request_foundation_approval(state),
        "generate_foundation_candidates": lambda: _tool_generate_foundation_candidates(state, image_provider),
        "rank_foundation_candidates": lambda: _tool_rank_foundation_candidates(state, vision_provider),
        "request_foundation_lock": lambda: _tool_request_foundation_lock(state),
        "build_scene_groups": lambda: _tool_build_scene_groups(state),
        "request_scene_group_approval": lambda: _tool_request_scene_group_approval(state),
        "generate_scene_group": lambda: _tool_generate_scene_group(state, image_provider),
        "inspect_scene_group": lambda: _tool_inspect_scene_group(state, vision_provider),
        "revise_scene_group": lambda: _tool_revise_scene_group(state, image_provider),
        "finalize_scene_group": lambda: _tool_finalize_scene_group(state),
        "request_manual_review": lambda: _tool_request_manual_review(state),
        "finalize_vision_recheck": lambda: _tool_finalize_vision_recheck(state),
        "finalize": lambda: _tool_finalize(state),
    }
    try:
        if action == "stop_needs_review":
            transition = _normalize_recoverable_paid_state(state)
            if transition:
                result = {"stopped": False, "code_owned_transition": transition}
            else:
                state["status"] = "needs_review"
                action_args = state.get("action_args", {})
                reason_code = re.sub(
                    r"[^a-z0-9_]+", "_", str(action_args.get("reason_code", "model_requested_review")).lower()
                ).strip("_")[:64] or "model_requested_review"
                state["stop_reason"] = f"supervisor_stopped:{reason_code}"
                result = {
                    "stopped": True,
                    "reason_code": reason_code,
                    "reason": str(action_args.get("reason", ""))[:500],
                    "evidence": action_args.get("evidence", []),
                }
        elif action not in tools:
            raise ValueError(f"tool is not implemented: {action}")
        else:
            result = tools[action]()
        state["invalid_actions"] = 0
        state["counters"]["tool_steps"] += 1
        usage["tool_steps"] = int(usage.get("tool_steps", 0)) + 1
        paid_after = _authorization_snapshot(state)
        _trace(state, "tool_result", {
            "action": action,
            "result": result,
            **_paid_tool_trace_accounting(state, paid_before, paid_after),
            "current_phase_item_count": _supervisor_snapshot(state).get("current_phase_item_count", 0),
        })
    except Exception as exc:
        state["status"] = "needs_review"
        state["stop_reason"] = f"tool_error:{action}:{type(exc).__name__}"
        _trace(state, "tool_error", {"action": action, "error": str(exc)[:500]})
    after = _state_progress(state)
    if before == after and state.get("status") == "running":
        state["no_progress"] = int(state.get("no_progress", 0)) + 1
        if state["no_progress"] >= 3:
            state["status"] = "needs_review"
            state["stop_reason"] = "three_no_progress_actions"
    else:
        state["no_progress"] = 0
    state["action"] = ""
    state["action_args"] = {}
    return state


def _route_after_supervisor(state: VisualAgentState) -> Literal["tool", "supervisor", "end"]:
    if state.get("status") != "running":
        return "end"
    return "tool" if state.get("action") else "supervisor"


def _route_after_tool(state: VisualAgentState) -> Literal["supervisor", "end"]:
    return "supervisor" if state.get("status") == "running" else "end"


def _persist_artifacts(state: VisualAgentState) -> dict:
    output_dir = state["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    if state.get("stop_reason") in {
        "scene_group_non_converging", "clean_regeneration_technical_failure",
        "foundation_reference_reset_required",
    }:
        _sync_blocked_quality_gate(state)
    quality_gate = dict(state.get("quality_gate", {}))
    if not quality_gate.get("quality_outcome"):
        status = str(state.get("status", ""))
        quality_gate["quality_outcome"] = (
            "blocked" if status in {"needs_review", "failed"} else "pending"
        )
        quality_gate.setdefault("passed_without_override", False)
        quality_gate.setdefault("accepted", False)
        quality_gate.setdefault("automated_review_status", "not_completed")
        quality_gate.setdefault("automated_review_completed", False)
        quality_gate.setdefault("blocking_status", "unknown")
    quality_gate["confidence_calibration"] = calibration_summary(
        state.get("vision_confidence_calibration")
    )
    state["quality_gate"] = quality_gate
    ledger = _load_paid_ledger(state)
    _reconcile_revision_attempt_history(state, ledger)
    _save_paid_ledger(state, ledger)
    grants = ledger.get("grants", {})
    historical_grant_ids = set(map(str, ledger.get("historical_grant_ids", [])))
    current_grants = {
        grant_id: item for grant_id, item in grants.items()
        if str(grant_id) not in historical_grant_ids
    }
    approved_paid_calls = sum(
        int(item.get("maximum_paid_calls", 0)) for item in current_grants.values()
    )
    used_paid_calls = sum(int(item.get("used_calls", 0)) for item in current_grants.values())
    historical_paid_calls = sum(
        int(item.get("used_calls", 0)) for grant_id, item in grants.items()
        if str(grant_id) in historical_grant_ids
    )
    actionable_uncertain_paid_jobs = sorted(
        str(item.get("logical_job_id", ""))
        for item in ledger.get("jobs", {}).values()
        if item.get("status") in {"started", "uncertain"}
        and _is_actionable_paid_entry(item)
    )
    superseded_uncertain_paid_jobs = sorted(
        str(item.get("logical_job_id", ""))
        for item in ledger.get("jobs", {}).values()
        if item.get("status") in {"started", "uncertain"}
        and not _is_actionable_paid_entry(item)
    )
    revision_attempt_summary = _revision_attempt_summary(ledger)
    pending_approval = (
        state.get("pending_approval", {})
        if isinstance(state.get("pending_approval"), dict) else {}
    )
    pending_stage = str(pending_approval.get("stage", ""))
    pending_paid_cost_gate = (
        pending_stage in {"foundation_cost", "foundation_retry_cost"}
        or pending_stage.startswith("scene_group_cost_")
    )
    pending_paid_limit = (
        int(pending_approval.get("maximum_paid_calls", 0))
        if pending_paid_cost_gate else 0
    )
    visual_bible = dict(state.get("visual_bible", {}))
    visual_bible["locked_assets"] = state.get("locked_assets", {})
    repair_plan = state.get("repair_plan")
    if not isinstance(repair_plan, dict) or not repair_plan:
        review_incomplete = bool(
            state.get("vision_recheck_only") or state.get("vision_repair_mode")
        ) and state.get("quality_gate", {}).get("automated_review_completed") is not True
        repair_plan = {
            "schema_version": "1.0",
            "status": "verification_incomplete" if review_incomplete else "none",
            "source_run_id": state.get("run_id", ""),
            "created_at": _now(),
            "groups": [], "group_ids": [], "shot_ids": [],
            "maximum_paid_calls": 0,
            "requires_new_paid_grant": False,
            "incomplete_reason": state.get("stop_reason", "") if review_incomplete else "",
            "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
        }
        state["repair_plan"] = repair_plan
    repair_history = [
        item for item in state.get("repair_history", []) if isinstance(item, dict)
    ]
    identity = identity_from_dict(state.get("run_identity"))
    if identity is None:
        bootstrap_contract = {
            "legacy_bootstrap": True,
            "run_id": state.get("run_id", ""),
            "storyboard_fingerprint": content_fingerprint(
                _storyboard_run_payload(state.get("storyboard", {})), length=32
            ),
        }
        _attach_v4_run_identity(state, bootstrap_contract, run_kind="legacy_migration")
        identity = identity_from_dict(state.get("run_identity"))
    event_store = VisualEventStore(output_dir, str(state["run_id"]))
    committed_event = event_store.commit_state(state, reason="persist_artifacts")
    if identity is None:
        raise ValueError("visual run identity could not be created")
    event_store.write_identity(identity)
    recovered = event_store.recover_state() or {}
    state["event_sequence"] = int(recovered.get("event_sequence", 0) or 0)
    state["event_checksum"] = str(recovered.get("event_checksum", ""))
    projection = projection_metadata(state)
    visual_bible["_projection"] = projection
    atomic_write_json(os.path.join(output_dir, "visual_bible.json"), visual_bible)
    atomic_write_json(os.path.join(output_dir, "visual_plan.json"), {
        "_projection": projection,
        "run_id": state["run_id"], "agent_version": VISUAL_AGENT_VERSION,
        "playbook_version": PLAYBOOK_VERSION, "inventory": state.get("inventory", {}),
        "foundation_assets": state.get("foundation_assets", []),
        "scene_groups": state.get("scene_groups", []),
        "compiled_constraints_by_shot": state.get("compiled_constraints_by_shot", {}),
        "fallback_constraints_by_shot": state.get("fallback_constraints_by_shot", {}),
    })
    public_repair_plan = {
        "_projection": projection, **repair_plan, "repair_history": repair_history
    }
    atomic_write_json(os.path.join(output_dir, "visual_repair_plan.json"), public_repair_plan)
    atomic_write_json(os.path.join(output_dir, "cost_plan.json"), {
        "_projection": projection,
        "run_id": state["run_id"], "budgets": state.get("budgets", {}),
        "actual": state.get("counters", {}),
        "paid_approval_grants": grants,
        "historical_grant_ids": sorted(historical_grant_ids),
        "historical_paid_calls": historical_paid_calls,
        "approved_paid_calls": approved_paid_calls,
        "used_paid_calls": used_paid_calls,
        "remaining_approved_paid_calls": max(0, approved_paid_calls - used_paid_calls),
        "unused_calls_across_current_run_grants": max(
            0, approved_paid_calls - used_paid_calls
        ),
        "current_pending_approval_stage": pending_stage,
        "current_pending_approval_is_paid_cost_gate": pending_paid_cost_gate,
        "current_pending_approval_maximum_paid_calls": pending_paid_limit,
        "current_stage_paid_calls_actionable_now": 0,
        "unused_grant_calls_are_current_stage_authorization": False,
        "unused_grant_calls_are_provider_quota": False,
        "provider_quota_state": "unknown_not_queried",
        "uncertain_paid_jobs": actionable_uncertain_paid_jobs,
        "actionable_uncertain_paid_jobs": actionable_uncertain_paid_jobs,
        "superseded_uncertain_paid_jobs": superseded_uncertain_paid_jobs,
        "revision_attempt_summary": revision_attempt_summary,
        "foundation_reset": state.get("foundation_reset", {}),
        "paid_ledger": os.path.relpath(_paid_ledger_path(state), output_dir),
        "remaining_model_calls": max(
            0, int(state["budgets"]["effective_max_calls"])
            - int(state.get("run_budget_usage", {}).get("model_calls", 0))
        ),
        "remaining_tool_steps": max(
            0, int(state["budgets"]["effective_max_steps"])
            - int(state.get("run_budget_usage", {}).get("tool_steps", 0))
        ),
        "budget_usage_scope": "current_invocation",
        "run_budget_usage": state.get("run_budget_usage", {}),
        "invocation_budget_history": state.get("invocation_budget_history", []),
        "paid_calls_require_cli_authorization_and_current_approval": True,
    })
    atomic_write_json(os.path.join(output_dir, "visual_review.json"), {
        "_projection": projection,
        "run_id": state["run_id"], "status": state.get("status"),
        "quality_outcome": state.get("quality_gate", {}).get("quality_outcome", "pending"),
        "passed_without_override": state.get("quality_gate", {}).get("passed_without_override", False),
        "blocking_issue_count": state.get("quality_gate", {}).get("blocking_issue_count"),
        "observed_blocking_issue_count": state.get("quality_gate", {}).get(
            "observed_blocking_issue_count", 0
        ),
        "last_observed_blocking_issue_count": state.get("quality_gate", {}).get(
            "last_observed_blocking_issue_count"
        ),
        "overridden_blocking_issue_count": state.get("quality_gate", {}).get(
            "overridden_blocking_issue_count", 0
        ),
        "automated_review_status": state.get("quality_gate", {}).get(
            "automated_review_status", "not_completed"
        ),
        "automated_review_completed": state.get("quality_gate", {}).get(
            "automated_review_completed", False
        ),
        "blocking_status": state.get("quality_gate", {}).get("blocking_status", "unknown"),
        "verification_mode": state.get("quality_gate", {}).get("verification_mode", "not_completed"),
        "unverified_checks": state.get("quality_gate", {}).get("unverified_checks", []),
        "stale_after_revision_shot_ids": state.get("quality_gate", {}).get(
            "stale_after_revision_shot_ids", []
        ),
        "overridden_issue_ids": state.get("quality_gate", {}).get("overridden_issue_ids", []),
        "confidence_calibration": calibration_summary(
            state.get("vision_confidence_calibration")
        ),
        "preflight_issues": state.get("preflight_issues", []),
        "issues": state.get("issues", []), "scene_groups": state.get("group_states", {}),
        "repair_plan": repair_plan,
        "repair_history": repair_history,
        "revision_attempt_summary": revision_attempt_summary,
        "foundation_reset": state.get("foundation_reset", {}),
    })
    run_dir = os.path.join(output_dir, "stages", "visual_agent", "runs", state["run_id"])
    os.makedirs(run_dir, exist_ok=True)
    atomic_write_json(os.path.join(run_dir, "state.json"), {
        **state, "_projection": projection,
    })
    manifest = {
        "_projection": projection,
        "run_id": state["run_id"], "status": state.get("status"),
        "stop_reason": state.get("stop_reason", ""), "stage": state.get("stage"),
        "visual_agent_version": VISUAL_AGENT_VERSION,
        "toolset_version": VISUAL_TOOLSET_VERSION, "playbook_version": PLAYBOOK_VERSION,
        "recovery_patch_version": VISUAL_RECOVERY_PATCH_VERSION,
        "architecture": architecture_manifest(),
        "run_identity": state.get("run_identity", {}),
        "invocation_compatibility": state.get("invocation_compatibility", {}),
        "event_store": {
            "event_sequence": state.get("event_sequence", 0),
            "event_checksum": state.get("event_checksum", ""),
            "event_appended": committed_event is not None,
            "recovery_authority": "events.jsonl",
        },
        "provider_capabilities": state.get("provider_capabilities", {}),
        "vision_confidence_calibration": calibration_summary(
            state.get("vision_confidence_calibration")
        ),
        "models": copy.deepcopy(
            state.get("run_invocation_contract", {}).get("models")
            or _model_names()
        ),
        "budgets": state.get("budgets", {}),
        "counters": state.get("counters", {}),
        "budget_usage_scope": "current_invocation",
        "run_budget_usage": state.get("run_budget_usage", {}),
        "invocation_budget_history": state.get("invocation_budget_history", []),
        "quality_gate": state.get("quality_gate", {}),
        "verification_history": state.get("verification_history", []),
        "vision_recheck_only": bool(state.get("vision_recheck_only")),
        "vision_repair_mode": bool(state.get("vision_repair_mode")),
        "provider_escalation_mode": bool(state.get("provider_escalation_mode")),
        "repair_source_run_id": state.get("repair_source_run_id", ""),
        "repair_group_ids": state.get("repair_group_ids", []),
        "repair_plan": repair_plan,
        "repair_history": repair_history,
        "revision_attempt_summary": revision_attempt_summary,
        "pending_approval": state.get("pending_approval", {}),
        "reconciliation": state.get("reconciliation", {}),
        "foundation_reset": state.get("foundation_reset", {}),
        "foundation_phase_index": state.get("foundation_phase_index", 0),
        "current_group_index": state.get("current_group_index", 0),
        "created_or_updated_at": _now(),
    }
    atomic_write_json(os.path.join(output_dir, "visual_agent_run.json"), manifest)
    return manifest


def _initial_state(storyboard_path: str, output_dir: str, run_id: str,
                   storyboard: dict, execute_paid_calls: bool,
                   foundation_candidates: int, max_auto_retries: int,
                   max_steps: int | None, max_calls: int | None, size: str,
                    capabilities: dict, image_parallelism: int,
                    aspect_mode: str,
                    confidence_calibration: dict | None = None) -> VisualAgentState:
    scene_count = len(storyboard.get("scenes", []))
    character_count = len(get_characters(storyboard))
    effective_calls = max_calls or min(120, max(40, 20 + 3 * character_count + 6 * scene_count))
    effective_steps = max_steps or min(180, max(60, 32 + 6 * character_count + 10 * scene_count))
    return {
        "run_id": run_id, "status": "running", "stop_reason": "", "stage": "new",
        "storyboard_path": storyboard_path, "output_dir": output_dir, "storyboard": storyboard,
        "inventory": {}, "visual_bible": {}, "foundation_assets": [],
        "foundation_phase_index": 0, "candidates": {}, "rankings": {},
        "locked_assets": {}, "scene_groups": [], "current_group_index": 0,
        "group_states": {}, "issues": [], "preflight_issues": [], "pending_approval": {},
        "foundation_budget_approved": False, "paid_authorized": execute_paid_calls,
        "approval_grants": {}, "foundation_grant_id": "",
        "foundation_primary_grant_id": "", "foundation_retry_grant_id": "",
        "paid_ledger": {},
        "counters": {
            "tool_steps": 0, "model_calls": 0, "image_calls": 0,
            "vision_calls": 0, "vision_attempts": 0, "vision_failures": 0,
        },
        "run_budget_usage": {"tool_steps": 0, "model_calls": 0},
        "invocation_budget_history": [],
        "invocation_budget_started_at": _now(),
        "budgets": {
            "requested_max_steps": max_steps if max_steps is not None else "auto",
            "effective_max_steps": effective_steps,
            "requested_max_calls": max_calls if max_calls is not None else "auto",
            "effective_max_calls": effective_calls,
            "foundation_candidates": foundation_candidates,
            "max_auto_retries": max_auto_retries,
            "image_parallelism": image_parallelism,
            "effective_request_size": size,
            "target_aspect_ratio": _storyboard_aspect(storyboard),
            "aspect_mode": aspect_mode,
        },
        "action": "", "action_args": {}, "decision_summary": "",
        "invalid_actions": 0, "no_progress": 0, "last_progress_fingerprint": "",
        "supervisor_unavailable_this_invocation": False,
        "trace_seq": 0, "invocation_count": 0,
        "vision_failure_history": [], "verification_history": [],
        "vision_recheck_only": False, "vision_recheck_group_ids": [],
        "vision_repair_mode": False,
        "vision_confidence_calibration": copy.deepcopy(confidence_calibration or {}),
        "provider_escalation_mode": False,
        "repair_source_run_id": "", "repair_group_ids": [], "repair_plan": {},
        "repair_history": [],
        "foundation_reset": {}, "foundation_candidate_history": {},
        "provider_capabilities": capabilities, "size": size,
        "fallback_constraints_by_shot": {},
        "target_aspect_ratio": _storyboard_aspect(storyboard),
        "aspect_mode": aspect_mode,
    }


def _begin_invocation_budget(
    state: VisualAgentState,
    *,
    archive_previous: bool,
) -> dict:
    """Start a fresh supervisor/tool budget while preserving cumulative audit facts."""
    previous_invocation = int(state.get("invocation_count", 0) or 0)
    previous_usage = {
        "model_calls": int(state.get("run_budget_usage", {}).get("model_calls", 0) or 0),
        "tool_steps": int(state.get("run_budget_usage", {}).get("tool_steps", 0) or 0),
    }
    archived: dict = {}
    if archive_previous:
        archived = {
            "schema_version": 1,
            "run_id": str(state.get("run_id", "")),
            "invocation_count": previous_invocation,
            "started_at": str(state.get("invocation_budget_started_at", "")),
            "archived_at": _now(),
            "status_at_end": str(state.get("status", "")),
            "stop_reason_at_end": str(state.get("stop_reason", "")),
            "stage_at_end": str(state.get("stage", "")),
            "usage": previous_usage,
            "limits": {
                "model_calls": int(state.get("budgets", {}).get("effective_max_calls", 0) or 0),
                "tool_steps": int(state.get("budgets", {}).get("effective_max_steps", 0) or 0),
            },
            "cumulative_counters_at_end": {
                "model_calls": int(state.get("counters", {}).get("model_calls", 0) or 0),
                "tool_steps": int(state.get("counters", {}).get("tool_steps", 0) or 0),
                "vision_calls": int(state.get("counters", {}).get("vision_calls", 0) or 0),
                "image_calls": int(state.get("counters", {}).get("image_calls", 0) or 0),
            },
        }
        history = state.setdefault("invocation_budget_history", [])
        if not isinstance(history, list):
            history = []
            state["invocation_budget_history"] = history
        history.append(archived)
    state["invocation_count"] = previous_invocation + 1
    state["run_budget_usage"] = {"tool_steps": 0, "model_calls": 0}
    state["invocation_budget_started_at"] = _now()
    return archived


def _configure_verification_run_budget(
    state: VisualAgentState,
    max_steps: int | None,
    max_calls: int | None,
) -> None:
    group_count = max(1, len(state.get("scene_groups", [])))
    effective_calls = max_calls if max_calls is not None else min(
        120, max(12, 4 + 4 * group_count)
    )
    effective_steps = max_steps if max_steps is not None else min(
        180, max(16, 6 + 6 * group_count)
    )
    state.setdefault("budgets", {}).update({
        "requested_max_calls": max_calls if max_calls is not None else "auto",
        "effective_max_calls": effective_calls,
        "requested_max_steps": max_steps if max_steps is not None else "auto",
        "effective_max_steps": effective_steps,
    })
    state["run_budget_usage"] = {"tool_steps": 0, "model_calls": 0}
    state["invocation_budget_started_at"] = _now()


def _prepare_vision_recheck_state(
    output_dir: str,
    run_id: str,
    storyboard_path: str,
    storyboard: dict,
    max_steps: int | None,
    max_calls: int | None,
    confidence_calibration: dict | None = None,
) -> VisualAgentState:
    manifest = read_json(os.path.join(output_dir, "visual_agent_run.json"))
    prior_run_id = str(manifest.get("run_id", "")) if isinstance(manifest, dict) else ""
    if not prior_run_id:
        raise ValueError("vision recheck requires an existing visual_agent_run.json")
    prior_path = os.path.join(
        output_dir, "stages", "visual_agent", "runs", prior_run_id, "state.json"
    )
    prior = read_json(prior_path)
    if not isinstance(prior, dict) or not (
        prior.get("status") == "completed"
        or prior.get("vision_recheck_only") is True
        or prior.get("vision_repair_mode") is True
    ):
        raise ValueError(
            "vision recheck requires a completed visual Agent, prior vision-only state, "
            "or prior vision-repair state"
        )
    if not prior.get("scene_groups") or not prior.get("group_states"):
        raise ValueError("vision recheck requires generated scene groups")
    state: VisualAgentState = copy.deepcopy(prior)
    all_group_ids = [
        str(group.get("group_id", "")) for group in prior.get("scene_groups", [])
        if str(group.get("group_id", ""))
    ]
    prior_unavailable = (
        list(map(str, prior.get("quality_gate", {}).get("unavailable_group_ids", [])))
        if prior.get("vision_recheck_only") is True
        and prior.get("repair_plan", {}).get("status") == "verification_incomplete"
        else []
    )
    review_group_ids = [
        group_id for group_id in all_group_ids if group_id in set(prior_unavailable)
    ] or all_group_ids
    scene_index = {
        str(group.get("group_id", "")): index
        for index, group in enumerate(prior.get("scene_groups", []))
    }
    prior_repair_plan = prior.get("repair_plan")
    if (
        isinstance(prior_repair_plan, dict)
        and prior_repair_plan
        and prior_repair_plan.get("status") not in {"none", "verification_incomplete"}
    ):
        archived = copy.deepcopy(prior_repair_plan)
        archived.update({"status": "superseded_by_recheck", "superseded_at": _now()})
        state.setdefault("repair_history", []).append(archived)
    state.setdefault("verification_history", []).append({
        "run_id": prior_run_id,
        "quality_gate": copy.deepcopy(prior.get("quality_gate", {})),
        "recorded_at": _now(),
    })
    state.update({
        "run_id": run_id,
        "storyboard_path": storyboard_path,
        "output_dir": output_dir,
        "storyboard": storyboard,
        "status": "running",
        "stop_reason": "",
        "stage": "group_review",
        "current_group_index": min(scene_index[group_id] for group_id in review_group_ids),
        "pending_approval": {},
        "paid_authorized": False,
        "vision_recheck_only": True,
        "vision_recheck_group_ids": review_group_ids,
        "vision_repair_mode": False,
        "vision_confidence_calibration": copy.deepcopy(confidence_calibration or {}),
        "provider_escalation_mode": False,
        "repair_source_run_id": "",
        "repair_group_ids": [],
        "repair_plan": {},
        "quality_gate": {
            "quality_outcome": "pending",
            "passed_without_override": False,
            "accepted": False,
            "automated_review_status": "pending",
            "automated_review_completed": False,
            "blocking_status": "unknown",
        },
        "issues": [],
        "action": "",
        "action_args": {},
        "invalid_actions": 0,
        "no_progress": 0,
        "trace_seq": 0,
        "invocation_count": 0,
    })
    review_id_set = set(review_group_ids)
    for group_id, group_state in state.get("group_states", {}).items():
        if not isinstance(group_state, dict) or not group_state.get("generated"):
            raise ValueError("vision recheck requires every scene group to retain generated images")
        group_state.pop("human_override", None)
        if str(group_id) in review_id_set:
            group_state["status"] = "generated"
            group_state["issues"] = []
            group_state["vision_available"] = None
        group_state["approved"] = False
        group_state["grant_id"] = ""
        group_state["pending_paid_operation"] = ""
    _configure_verification_run_budget(state, max_steps, max_calls)
    prior_ledger = read_json(os.path.join(
        output_dir, "stages", "visual_agent", "runs", prior_run_id, "paid_ledger.json"
    ))
    if isinstance(prior_ledger, dict):
        copied_ledger = copy.deepcopy(prior_ledger)
        copied_ledger["run_id"] = run_id
        copied_ledger["source_run_id"] = prior_run_id
        copied_ledger["historical_grant_ids"] = sorted(set(map(str, (
            list(prior_ledger.get("historical_grant_ids", []))
            + list(prior_ledger.get("grants", {}).keys())
        ))))
        _save_paid_ledger(state, copied_ledger)
    return state


def _prepare_or_resume_vision_repair_state(
    output_dir: str,
    base_run_id: str,
    storyboard_path: str,
    storyboard: dict,
    max_steps: int | None,
    max_calls: int | None,
    capabilities: dict,
    confidence_calibration: dict | None = None,
) -> tuple[VisualAgentState, bool]:
    manifest = read_json(os.path.join(output_dir, "visual_agent_run.json"))
    source_run_id = str(manifest.get("run_id", "")) if isinstance(manifest, dict) else ""
    if not source_run_id:
        raise ValueError("vision repair requires an existing visual_agent_run.json")
    source_path = os.path.join(
        output_dir, "stages", "visual_agent", "runs", source_run_id, "state.json"
    )
    source = recover_current_state(output_dir)
    if not isinstance(source, dict) or str(source.get("run_id", "")) != source_run_id:
        source = read_json(source_path)
    if not isinstance(source, dict):
        raise ValueError("vision repair source state is missing")
    if source.get("vision_repair_mode") and source.get("status") in {"running", "awaiting_approval"}:
        source["storyboard_path"] = storyboard_path
        source["output_dir"] = output_dir
        source["storyboard"] = storyboard
        return source, False

    repair_plan = source.get("repair_plan")
    provider_escalation_source = bool(
        source.get("status") == "needs_review"
        and source.get("stop_reason") == "scene_group_non_converging"
        and isinstance(repair_plan, dict)
        and repair_plan.get("status") == "provider_escalation_proposed"
    )
    if (
        isinstance(repair_plan, dict)
        and repair_plan.get("status") == "reference_reset_required"
    ):
        raise ValueError(
            "vision repair is not converging with the current locked references; start a fresh visual run "
            "and relock canonical foundation anchors before further paid shot regeneration"
        )
    if (
        not provider_escalation_source
        and (
        source.get("status") != "needs_review"
        or source.get("stop_reason") not in {"vision_recheck_blocked", "vision_repair_blocked"}
        or not (source.get("vision_recheck_only") or source.get("vision_repair_mode"))
        or not isinstance(repair_plan, dict)
        or repair_plan.get("status") != "proposed"
        )
    ):
        raise ValueError(
            "vision repair requires a blocked vision-only result or a prepared provider escalation"
        )
    if (
        not provider_escalation_source
        and not source.get("quality_gate", {}).get("automated_review_completed")
    ):
        raise ValueError("vision repair requires a complete automated review of every scene group")
    repair_group_ids = [
        str(value) for value in repair_plan.get("group_ids", []) if str(value)
    ]
    if not repair_group_ids or int(repair_plan.get("maximum_paid_calls", 0)) < 1:
        raise ValueError("vision repair plan contains no payable blocking shots")
    scene_index = {
        str(group.get("group_id", "")): index
        for index, group in enumerate(source.get("scene_groups", []))
    }
    missing_groups = [group_id for group_id in repair_group_ids if group_id not in scene_index]
    if missing_groups:
        raise ValueError("vision repair plan references unknown scene groups: " + ", ".join(missing_groups))

    run_id = create_run_identity({
        "compatibility_key": base_run_id,
        "source_run_id": source_run_id,
        "mode": "provider_escalation" if provider_escalation_source else "vision_repair",
        "escalation_strategy_version": (
            ESCALATION_STRATEGY_VERSION if provider_escalation_source else 0
        ),
    }, run_kind=(
        "provider_escalation" if provider_escalation_source else "vision_repair"
    ), parent_run_id=source_run_id).run_id
    state: VisualAgentState = copy.deepcopy(source)
    state.setdefault("verification_history", []).append({
        "run_id": source_run_id,
        "quality_gate": copy.deepcopy(source.get("quality_gate", {})),
        "recorded_at": _now(),
    })
    prepared_plan = copy.deepcopy(repair_plan)
    prepared_plan.update({
        "status": (
            "provider_escalation_pending_approval"
            if provider_escalation_source else "pending_approval"
        ),
        "repair_run_id": run_id,
        "repair_started_at": _now(),
    })
    state.update({
        "run_id": run_id,
        "storyboard_path": storyboard_path,
        "output_dir": output_dir,
        "storyboard": storyboard,
        "status": "running",
        "stop_reason": "",
        "stage": "group_approval",
        "current_group_index": min(scene_index[group_id] for group_id in repair_group_ids),
        "pending_approval": {},
        "paid_authorized": False,
        "vision_recheck_only": False,
        "vision_repair_mode": True,
        "provider_escalation_mode": provider_escalation_source,
        "repair_source_run_id": source_run_id,
        "repair_group_ids": repair_group_ids,
        "repair_plan": prepared_plan,
        "provider_capabilities": copy.deepcopy(capabilities),
        "vision_confidence_calibration": copy.deepcopy(confidence_calibration or {}),
        "quality_gate": {
            "mode": "vision_repair",
            "quality_outcome": "pending",
            "passed_without_override": False,
            "accepted": False,
            "automated_review_status": "pending",
            "automated_review_completed": False,
            "blocking_status": "blocked",
            "blocking_issue_count": len({
                str(issue.get("issue_id", ""))
                for group in repair_plan.get("groups", [])
                for issue in group.get("issues", [])
                if str(issue.get("issue_id", ""))
            }),
        },
        "action": "",
        "action_args": {},
        "invalid_actions": 0,
        "no_progress": 0,
        "trace_seq": 0,
        "invocation_count": 0,
    })
    repair_ids = set(repair_group_ids)
    escalation_groups = {
        str(item.get("group_id", "")): item
        for item in repair_plan.get("groups", [])
        if isinstance(item, dict) and str(item.get("group_id", ""))
    }
    for group_id, group_state in state.get("group_states", {}).items():
        if group_id in repair_ids:
            group_state.pop("human_override", None)
            group_state["status"] = "planned"
            group_state["approved"] = False
            group_state["grant_id"] = ""
            if provider_escalation_source:
                group_plan = escalation_groups.get(str(group_id), {})
                group_state["provider_escalation"] = {
                    "schema_version": ESCALATION_SCHEMA_VERSION,
                    "strategy_version": ESCALATION_STRATEGY_VERSION,
                    "status": "pending_approval",
                    "group_id": str(group_id),
                    "tasks": copy.deepcopy(group_plan.get("tasks", [])),
                    "shot_ids": list(map(str, group_plan.get("shot_ids", []))),
                    "maximum_paid_calls": int(
                        group_plan.get("maximum_paid_calls", 0) or 0
                    ),
                }
                group_state["pending_paid_operation"] = "provider_escalation"
            else:
                group_state["pending_paid_operation"] = "retry"
        else:
            group_state["status"] = "accepted"
    _configure_verification_run_budget(state, max_steps, max_calls)
    prior_ledger = read_json(os.path.join(
        output_dir, "stages", "visual_agent", "runs", source_run_id, "paid_ledger.json"
    ))
    if isinstance(prior_ledger, dict):
        copied_ledger = copy.deepcopy(prior_ledger)
        copied_ledger["run_id"] = run_id
        copied_ledger["source_run_id"] = source_run_id
        copied_ledger["historical_grant_ids"] = sorted(set(map(str, (
            list(prior_ledger.get("historical_grant_ids", []))
            + list(prior_ledger.get("grants", {}).keys())
        ))))
        _save_paid_ledger(state, copied_ledger)
    return state, True


@_exclusive_visual_invocation
def run_image_agent(
    storyboard_json: str,
    output_dir: str | None = None,
    *,
    execute_paid_calls: bool = False,
    resume: bool = True,
    resume_needs_review: bool = False,
    resume_reviewer: str = "",
    resume_note: str = "",
    recheck_vision: bool = False,
    repair_vision_blockers: bool = False,
    reset_foundation_references: bool = False,
    foundation_candidates: int = 3,
    max_auto_retries: int = 1,
    max_steps: int | None = None,
    max_calls: int | None = None,
    size: str | None = None,
    image_parallelism: int | None = None,
    provider_capabilities: dict | None = None,
    vision_calibration_file: str | None = None,
    image_provider: ImageProvider | None = None,
    vision_provider: VisionProvider | None = None,
    supervisor_provider: SupervisorProvider | None = None,
) -> dict:
    """Run or resume the visual supervisor and return its public manifest.

    Injected providers are intended for tests and offline Mock demonstrations;
    they are never serialized into LangGraph state.
    """
    if foundation_candidates < 1:
        raise ValueError("foundation_candidates must be positive")
    if max_auto_retries < 0:
        raise ValueError("max_auto_retries cannot be negative")
    if max_calls is not None and max_calls < 1:
        raise ValueError("max_calls must be positive")
    if max_steps is not None and max_steps < 1:
        raise ValueError("max_steps must be positive")
    if recheck_vision and repair_vision_blockers:
        raise ValueError("vision recheck and vision repair are mutually exclusive")
    if recheck_vision and execute_paid_calls:
        raise ValueError("vision recheck must run with --no-image-api")
    if reset_foundation_references and (
        not resume or execute_paid_calls or recheck_vision or repair_vision_blockers
    ):
        raise ValueError(
            "foundation reference reset preparation requires --resume --no-image-api "
            "and cannot be combined with vision modes"
        )
    if image_parallelism is None:
        image_parallelism = _configured_image_parallelism()
    if image_parallelism < 1 or image_parallelism > 16:
        raise ValueError("image_parallelism must be between 1 and 16")
    storyboard_path = os.path.abspath(storyboard_json)
    output_dir = os.path.abspath(output_dir or os.path.dirname(storyboard_path))
    storyboard = _read_storyboard(storyboard_path)
    size = _request_size_for_aspect(storyboard, size)
    target_aspect_ratio = _storyboard_aspect(storyboard)
    aspect_mode = _configured_aspect_mode()
    capabilities = dict(provider_capabilities or get_image_provider_capabilities())
    models = _model_names()
    confidence_calibration = (
        load_calibration_profile(os.path.abspath(vision_calibration_file))
        if vision_calibration_file else {}
    )
    calibrated_model = str(confidence_calibration.get("model", ""))
    if calibrated_model and models.get("vision_model") and calibrated_model != models["vision_model"]:
        raise ValueError(
            "vision calibration model does not match the configured vision model"
        )
    current_resume_contract = _resume_invocation_contract(
        storyboard, models, capabilities, foundation_candidates, max_auto_retries,
        max_steps, max_calls, size, target_aspect_ratio, aspect_mode, image_parallelism,
        confidence_calibration,
    )
    base_run_id = content_fingerprint(
        _storyboard_run_payload(storyboard), models, capabilities, foundation_candidates, max_auto_retries,
        max_steps, max_calls, size, target_aspect_ratio, aspect_mode, image_parallelism,
        PLAYBOOK_VERSION, VISUAL_AGENT_VERSION,
        VISUAL_TOOLSET_VERSION, calibration_summary(confidence_calibration), length=32,
    )
    # ``base_run_id`` is retained only as a legacy compatibility lookup key.
    # New v4 runs always receive an opaque UUID identity independent of the
    # invocation contract hash.
    proposed_identity = create_run_identity(
        current_resume_contract,
        run_kind="vision_recheck" if recheck_vision else "production",
    )
    run_id = proposed_identity.run_id
    upstream_gate = _upstream_storyboard_gate(storyboard)
    if not upstream_gate["passed"]:
        blocked_state = _initial_state(
            storyboard_path, output_dir, run_id, storyboard, False,
            foundation_candidates, max_auto_retries, max_steps, max_calls, size,
            capabilities, image_parallelism, aspect_mode,
            confidence_calibration,
        )
        _attach_v4_run_identity(
            blocked_state, current_resume_contract, run_kind="upstream_blocked"
        )
        blocked_state["status"] = "needs_review"
        blocked_state["stop_reason"] = upstream_gate["reason"]
        blocked_state["stage"] = "blocked_upstream"
        blocked_state["quality_gate"] = {"upstream_storyboard": upstream_gate}
        _activate_trace(blocked_state, reset_private=not resume)
        _trace(blocked_state, "upstream_gate_blocked", upstream_gate)
        return _persist_artifacts(blocked_state)
    repair_state_created = False
    migration_resume_selected: dict = {}
    if repair_vision_blockers:
        if not resume:
            raise ValueError("vision repair requires --resume")
        state, repair_state_created = _prepare_or_resume_vision_repair_state(
            output_dir, base_run_id, storyboard_path, storyboard, max_steps, max_calls,
            capabilities,
            confidence_calibration,
        )
        run_id = state["run_id"]
    elif recheck_vision:
        state = _prepare_vision_recheck_state(
            output_dir, run_id, storyboard_path, storyboard, max_steps, max_calls,
            confidence_calibration,
        )
    else:
        state = recover_current_state(output_dir) if resume else None
        recovered_identity = identity_from_dict(
            state.get("run_identity") if isinstance(state, dict) else None
        )
        if isinstance(state, dict) and recovered_identity:
            report = compatibility_report(recovered_identity, current_resume_contract)
            if (
                not report["compatible"]
                or recovered_identity.run_kind not in {"production", "legacy_migration"}
            ):
                state = None
            else:
                run_id = recovered_identity.run_id
        legacy_lookup_run_id = run_id if state is not None else base_run_id
        state_path = os.path.join(
            output_dir, "stages", "visual_agent", "runs", legacy_lookup_run_id, "state.json"
        )
        if state is None:
            state = read_json(state_path) if resume else None
            if isinstance(state, dict) and state.get("run_id"):
                run_id = str(state["run_id"])
        if state is None and resume:
            manifest = read_json(os.path.join(output_dir, "visual_agent_run.json"))
            prior_run_id = (
                str(manifest.get("run_id", "")) if isinstance(manifest, dict) else ""
            )
            if prior_run_id and prior_run_id != run_id:
                prior_state_path = os.path.join(
                    output_dir, "stages", "visual_agent", "runs", prior_run_id, "state.json"
                )
                prior_state = read_json(prior_state_path)
                migration = (
                    prior_state.get("resume_migration_contract", {})
                    if isinstance(prior_state, dict) else {}
                )
                stored_contract = (
                    migration.get("invocation_contract", {})
                    if isinstance(migration, dict) else {}
                )
                compatible = bool(
                    isinstance(prior_state, dict)
                    and isinstance(migration, dict)
                    and int(migration.get("schema_version", 0) or 0) == 1
                    and str(migration.get("migrated_run_id", "")) == prior_run_id
                    and str(migration.get("target_toolset_version", ""))
                    == VISUAL_TOOLSET_VERSION
                    and isinstance(stored_contract, dict)
                    and str(stored_contract.get("fingerprint", ""))
                    == str(current_resume_contract.get("fingerprint", ""))
                )
                if compatible:
                    state = prior_state
                    run_id = prior_run_id
                    migration_resume_selected = {
                        "run_id": prior_run_id,
                        "source_toolset_version": migration.get("source_toolset_version", ""),
                        "target_toolset_version": migration.get("target_toolset_version", ""),
                        "invocation_contract_fingerprint": stored_contract.get("fingerprint", ""),
                    }
    if state is None:
        if reset_foundation_references:
            raise ValueError(
                "foundation reference reset requires a compatible existing run; "
                "run zero-API paid-artifact reconcile first after a toolset upgrade"
            )
        state = _initial_state(
            storyboard_path, output_dir, run_id, storyboard, execute_paid_calls,
            foundation_candidates, max_auto_retries, max_steps, max_calls, size, capabilities,
            image_parallelism, aspect_mode,
            confidence_calibration,
        )
        _attach_v4_run_identity(state, current_resume_contract)
        _activate_trace(state, reset_private=not resume)
        _trace(state, "run_started", {"resume": False, "models": models})
    else:
        run_kind = (
            "vision_recheck" if recheck_vision else
            "provider_escalation" if state.get("provider_escalation_mode") else
            "vision_repair" if repair_vision_blockers else
            "production"
        )
        parent_run_id = str(state.get("run_identity", {}).get("run_id", ""))
        _attach_v4_run_identity(
            state,
            current_resume_contract,
            run_kind=run_kind,
            parent_run_id=parent_run_id if parent_run_id != str(state.get("run_id", "")) else "",
        )
        state["supervisor_unavailable_this_invocation"] = False
        state["output_dir"] = output_dir
        state["storyboard_path"] = storyboard_path
        state["vision_confidence_calibration"] = copy.deepcopy(confidence_calibration)
        _activate_trace(state, reset_private=recheck_vision or repair_state_created)
        archived_budget = _begin_invocation_budget(
            state,
            archive_previous=not recheck_vision and not repair_state_created,
        )
        _trace(state, "invocation_budget_started", {
            "invocation_count": state["invocation_count"],
            "usage_scope": "current_invocation",
            "archived_previous_invocation": bool(archived_budget),
            "archived_previous_usage": archived_budget.get("usage", {}),
            "effective_max_calls": int(state.get("budgets", {}).get("effective_max_calls", 0) or 0),
            "effective_max_steps": int(state.get("budgets", {}).get("effective_max_steps", 0) or 0),
        })
        if migration_resume_selected:
            _trace(state, "migration_resume_selected", migration_resume_selected)
        if recheck_vision:
            _trace(state, "vision_recheck_started", {
                "models": models,
                "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
                "verification_history_count": len(state.get("verification_history", [])),
            })
        if repair_state_created:
            _trace(state, "vision_repair_started", {
                "models": models,
                "repair_source_run_id": state.get("repair_source_run_id", ""),
                "repair_group_ids": state.get("repair_group_ids", []),
                "maximum_paid_calls": state.get("repair_plan", {}).get("maximum_paid_calls", 0),
                "image_calls_before": int(state.get("counters", {}).get("image_calls", 0)),
            })
        state["paid_authorized"] = execute_paid_calls
        _trace(state, "run_resumed", {
            "invocation_count": state["invocation_count"],
            "vision_recheck_only": bool(state.get("vision_recheck_only")),
            "vision_repair_mode": bool(state.get("vision_repair_mode")),
        })
        if (
            state.get("status") == "needs_review"
            and state.get("stop_reason") in {"model_budget_exhausted", "tool_budget_exhausted"}
        ):
            previous_reason = str(state.get("stop_reason", ""))
            state["status"] = "running"
            state["stop_reason"] = ""
            state["action"] = ""
            state["action_args"] = {}
            state["invalid_actions"] = 0
            state["no_progress"] = 0
            _trace(state, "invocation_budget_resume", {
                "previous_stop_reason": previous_reason,
                "invocation_count": state["invocation_count"],
                "stage": state.get("stage", ""),
            })
        backfilled_contracts = _backfill_foundation_reference_contracts(state)
        reconciliation = _reconcile_durable_progress(state)
        if backfilled_contracts:
            reconciliation["foundation_reference_contracts_backfilled"] = backfilled_contracts
        if (
            reconciliation.get("recovered_jobs")
            or reconciliation.get("recovered_candidates")
            or reconciliation.get("uncertain_paid_jobs")
            or reconciliation.get("post_reset_revision_collision_repair")
        ):
            _trace(state, "durable_progress_reconciled", reconciliation)
        current_group = _current_group(state)
        migration_transition = None
        if current_group:
            current_group_state = state.get("group_states", {}).get(
                str(current_group.get("group_id", "")), {}
            )
            current_blocking = [
                issue for issue in current_group_state.get("issues", [])
                if isinstance(issue, dict) and issue.get("blocking") is True
            ] if isinstance(current_group_state, dict) else []
            migration_transition = (
                _close_blocked_post_transfer_scale_evidence_reconstruction(
                    state, current_group, current_group_state, current_blocking,
                    "resume_migration",
                )
                or _prepare_post_transfer_scale_evidence_reconstruction(
                    state, current_group, current_group_state, current_blocking,
                    "resume_migration",
                )
                or _close_blocked_post_foundation_reset_transfer(
                    state, current_group, current_group_state, current_blocking,
                    "resume_migration",
                )
                or _prepare_post_foundation_reset_transfer(
                    state, current_group, current_group_state, current_blocking,
                    "resume_migration",
                )
            )
            if (
                migration_transition
                and migration_transition.get("operation") == "scale_evidence_reconstruction"
                and not state.get("pending_approval")
            ):
                state["stage"] = "manual_review"
                _tool_request_manual_review(state)
        try:
            applied = False if recheck_vision else _apply_pending_decision(state)
        except ValueError as exc:
            state["status"] = "failed"
            state["stop_reason"] = f"invalid_approval:{exc}"
            _trace(state, "approval_error", {"error": str(exc)})
            return _persist_artifacts(state)
        transition = _normalize_recoverable_paid_state(state)
        if (
            transition is None
            and state.get("status") == "needs_review"
            and state.get("stop_reason") == "clean_regeneration_technical_failure"
        ):
            _prepare_technical_retry_transition(state, "legacy_resume")
        if transition is None:
            transition = _reclassify_scale_reference_nonconvergence(
                state, "legacy_resume"
            )
        if reset_foundation_references:
            reset = _prepare_foundation_reference_reset(state, "explicit_cli_request")
            if reset.get("status") != "candidate_approval":
                raise ValueError(
                    "foundation reference reset is already prepared; resume without "
                    "--reset-foundation-references"
                )
            approval = _tool_request_foundation_approval(state)
            _trace(state, "foundation_reference_reset_approval_prepared", {
                "approval": approval,
                "model_calls_made": 0,
                "vision_calls_made": 0,
                "image_calls_made": 0,
            })
            return _persist_artifacts(state)
        if not applied and state.get("status") == "awaiting_approval":
            if state.get("stop_reason") == "paid_calls_not_authorized" and execute_paid_calls:
                state["status"] = "running"
                state["stop_reason"] = ""
            else:
                return _persist_artifacts(state)
        if (
            state.get("status") == "needs_review"
            and state.get("stop_reason") == "three_invalid_actions"
            and _code_validated_stage_action(state)
            and _recent_protocol_failures_are_unusable_responses(state)
        ):
            previous_reason = str(state.get("stop_reason", ""))
            state["status"] = "running"
            state["stop_reason"] = ""
            state["action"] = ""
            state["action_args"] = {}
            state["invalid_actions"] = 0
            state["no_progress"] = 0
            _trace(state, "technical_supervisor_recovery", {
                "previous_stop_reason": previous_reason,
                "stage": state.get("stage", ""),
                "fallback_action": _code_validated_stage_action(state),
                "evidence": "three consecutive unusable supervisor responses",
            })
        if state.get("status") == "needs_review" and str(state.get("stop_reason", "")).startswith("tool_error:"):
            state["status"] = "running"
            state["stop_reason"] = ""
            state["action"] = ""
        if state.get("status") == "needs_review" and resume_needs_review:
            stop_reason = str(state.get("stop_reason", ""))
            if not stop_reason.startswith("supervisor_stopped"):
                raise ValueError(
                    "--resume-needs-review only resumes an explicit supervisor stop; "
                    "technical, approval and budget stops require their owning remedy"
                )
            if _is_placeholder_review_text(resume_reviewer) or len(resume_reviewer.strip()) < 2:
                raise ValueError("resuming needs_review requires a specific human reviewer")
            if _is_placeholder_review_text(resume_note) or len(resume_note.strip()) < 12:
                raise ValueError("resuming needs_review requires a specific human review note")
            previous_reason = stop_reason
            state["status"] = "running"
            state["stop_reason"] = ""
            state["action"] = ""
            state["action_args"] = {}
            state["invalid_actions"] = 0
            state["no_progress"] = 0
            _trace(state, "manual_resume", {
                "reviewer": resume_reviewer.strip()[:200],
                "note": resume_note.strip()[:1000],
                "previous_stop_reason": previous_reason,
                "state_fingerprint": _state_progress(state),
            })
        if state.get("status") == "needs_review":
            return _persist_artifacts(state)
    if state.get("status") in {"completed", "failed"}:
        return _persist_artifacts(state)

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        state["status"] = "failed"
        state["stop_reason"] = "langgraph_dependencies_missing"
        _trace(state, "dependency_error", {"error": str(exc)})
        return _persist_artifacts(state)

    image_provider = image_provider or _default_image_provider
    vision_provider = vision_provider or _default_vision_provider
    checkpoint_path = os.path.join(output_dir, "stages", "visual_agent", "checkpoints.sqlite")
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    try:
        saver = SqliteSaver(connection)
        saver.setup()
        graph_builder = StateGraph(VisualAgentState)
        graph_builder.add_node("supervisor", lambda value: _supervisor_node(value, supervisor_provider))
        graph_builder.add_node(
            "execute_tool", lambda value: _execute_tool_node(value, image_provider, vision_provider)
        )
        graph_builder.add_edge(START, "supervisor")
        graph_builder.add_conditional_edges(
            "supervisor", _route_after_supervisor,
            {"tool": "execute_tool", "supervisor": "supervisor", "end": END},
        )
        graph_builder.add_conditional_edges(
            "execute_tool", _route_after_tool, {"supervisor": "supervisor", "end": END},
        )
        graph = graph_builder.compile(checkpointer=saver)
        invocation = int(state.get("invocation_count", 0))
        config = {
            "configurable": {"thread_id": f"{run_id}:{invocation}"},
            "recursion_limit": max(200, int(state["budgets"]["effective_max_steps"]) * 3),
        }
        result = graph.invoke(state, config=config)
        if isinstance(result, dict):
            state = result
        else:
            state["status"] = "failed"
            state["stop_reason"] = "langgraph_returned_no_state"
    except KeyboardInterrupt:
        reconciliation = _reconcile_durable_progress(state)
        state["status"] = "needs_review"
        state["stop_reason"] = "execution_interrupted"
        _trace(state, "execution_interrupted", reconciliation)
    except Exception as exc:
        state["status"] = "needs_review"
        state["stop_reason"] = f"graph_error:{type(exc).__name__}"
        _trace(state, "graph_error", {"error": str(exc)[:500]})
    finally:
        connection.close()
    return _persist_artifacts(state)
