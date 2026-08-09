"""Provider-neutral LangGraph supervisor Agent for storyboard generation.

The model selects JSON actions; Python owns the tool whitelist, hard quality
gates, budgets, checkpoints, and completion decision. Media APIs are
deliberately absent from the tool registry.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from manju.pipeline.storyboard_agent import (
    BLOCKING_REVIEW_CATEGORIES,
    _canonical_text,
    _clean_plan_anchors,
    _deterministic_quality_issues,
    _ending_fidelity_issues,
    _plan_quality_errors,
    _prepare_generated_scene,
    _replace_named_styles,
)
from manju.pipeline.storyboard_schema import normalize_storyboard, validate_storyboard
from manju.pipeline.storyboard_stages import (
    STAGE_VERSION,
    _chunk_text,
    _plan_prompts,
    _scene_prompts,
    _scene_source,
)
from manju.utils.ai import call_llm, get_ai_config, parse_json_response
from manju.utils.runtime import atomic_write_json, content_fingerprint, read_json


SUPERVISOR_AGENT_VERSION = "3.5"
SUPERVISOR_TOOLSET_VERSION = "3.5"

REVIEW_CATEGORY_ALIASES = {
    "source_alignment": "source_fidelity",
    "beat_alignment": "source_fidelity",
    "source_coverage": "source_fidelity",
    "required_fact_omission": "source_fidelity",
    "source_fact_omission": "source_fidelity",
    "fact_omission": "source_fidelity",
    "source_traceability": "source_fidelity",
    "beat_traceability": "source_fidelity",
    "source_mapping": "source_fidelity",
    "beat_mapping": "source_fidelity",
    "source_beat_alignment": "source_fidelity",
    "beat_source_alignment": "source_fidelity",
    "metadata_consistency": "continuity",
    "character_visibility": "continuity",
    "visible_character_metadata": "visible_entity_consistency",
    "visible_prop_metadata": "visible_entity_consistency",
    "prop_continuity_metadata": "asset_binding",
    "prop_visibility": "asset_binding",
    "asset_reference": "asset_binding",
}


def _canonical_review_category(value: Any) -> str:
    """Normalize provider category drift without accepting unrelated checks."""
    original = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if original in REVIEW_CATEGORY_ALIASES:
        return REVIEW_CATEGORY_ALIASES[original]
    tokens = {token for token in original.split("_") if token}
    if tokens.intersection({"source", "beat"}) and tokens.intersection({
        "alignment", "mapping", "coverage", "fidelity", "trace", "traceability",
        "omission",
    }):
        return "source_fidelity"
    return original
SUBJECTIVE_REVIEW_CATEGORIES = {
    "aesthetics", "art_direction", "art_style", "camera", "cinematography",
    "composition", "framing", "pacing", "rhythm", "shot_variety", "style",
}
PRODUCTION_METADATA_CATEGORIES = {
    "asset_binding", "visible_entity_consistency",
}
DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_CALLS = None
AUTO_MAX_CALLS_CAP = 36
DEFAULT_MAX_REVISIONS = 2

ALLOWED_ACTIONS = (
    "analyze_source",
    "create_plan",
    "revise_plan",
    "generate_scenes",
    "assemble_storyboard",
    "validate_schema",
    "compare_source",
    "inspect_shootability",
    "review_storyboard",
    "revise_scenes",
    "finalize",
    "stop_needs_review",
)

# Provider-independent action contract. Unknown fields are protocol errors;
# they are never silently discarded or written to checkpoints.
ACTION_ARG_KEYS = {
    "analyze_source": set(),
    "create_plan": set(),
    "revise_plan": {"issue_ids"},
    "generate_scenes": {"scene_ids"},
    "assemble_storyboard": set(),
    "validate_schema": set(),
    "compare_source": {"scene_ids"},
    "inspect_shootability": {"scene_ids"},
    "review_storyboard": set(),
    "revise_scenes": {"scene_ids", "issue_ids"},
    "finalize": set(),
    "stop_needs_review": {"reason"},
}

ACTION_ARG_ALIASES = {
    "revise_scenes": {
        "blocking_issue_ids": "issue_ids",
        "current_blocking_issue_ids": "issue_ids",
        "target_scene_ids": "scene_ids",
    },
    "revise_plan": {"blocking_issue_ids": "issue_ids"},
}


class SupervisorState(TypedDict, total=False):
    input_text: str
    title: str
    word_count: int
    scene_count: int
    stage_dir: str
    run_dir: str
    run_id: str
    trace_path: str
    model_name: str
    chunks: list[str]
    chunk_count: int
    summaries: list[str]
    source_model: dict
    plan: dict
    plan_errors: list[str]
    completed_scenes: list[dict]
    storyboard: dict
    validation_errors: list[str] | None
    source_issues: list[dict] | None
    shootability_issues: list[dict] | None
    review: dict | None
    review_history: list[dict]
    last_verified_storyboard: dict
    last_verified_fingerprint: str
    candidate_fingerprint: str
    audited_fingerprint: str
    verification_state: str
    pending_revision: dict
    pending_action: dict
    last_result: dict
    action_history: list[dict]
    tool_counts: dict[str, int]
    model_calls: int
    tool_steps: int
    revision_counts: dict[str, int]
    revision_attempt_counts: dict[str, int]
    revision_extension_counts: dict[str, int]
    failed_revision_contracts: list[dict]
    invalid_action_count: int
    no_progress_count: int
    last_no_progress_signature: str
    max_steps: int
    max_calls: int
    requested_max_calls: int | None
    budget_factors: dict
    max_revisions: int
    status: str
    stop_reason: str


class ModelBudgetExhausted(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _redact_configured_secret(text: str) -> str:
    value = str(text)
    try:
        _, _, api_key = get_ai_config()
    except Exception:
        api_key = None
    if api_key:
        value = value.replace(str(api_key), "[REDACTED]")
    return value


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in (
                "key", "token", "secret", "password", "auth", "reasoning",
                "thought", "chain_of_thought", "analysis", "explanation",
            )):
                continue
            clean[str(key)] = _safe_value(item)
        return clean
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:100]]
    if isinstance(value, str):
        return _redact_configured_secret(value)[:12000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _validate_action_args(action: str, args: Any) -> tuple[dict, dict | None]:
    if not isinstance(args, dict):
        return {}, {"error": "invalid_action_args", "message": "args must be a JSON object"}
    aliases = ACTION_ARG_ALIASES.get(action, {})
    normalized = dict(args)
    for alias, canonical in aliases.items():
        if alias not in normalized:
            continue
        if canonical in normalized and normalized[canonical] != normalized[alias]:
            return {}, {
                "error": "invalid_action_args",
                "message": f"conflicting values for {canonical} and alias {alias}",
            }
        normalized[canonical] = normalized.pop(alias)
    args = normalized
    allowed = ACTION_ARG_KEYS.get(action)
    if allowed is None:
        return {}, None
    unknown = sorted(str(key) for key in args if key not in allowed)
    if unknown:
        return {}, {
            "error": "invalid_action_args",
            "message": "unknown action arguments",
            "unknown_args": unknown,
            "allowed_args": sorted(allowed),
        }
    clean = {key: _safe_value(args[key]) for key in allowed if key in args}
    for key in ("scene_ids", "issue_ids"):
        if key in clean and not isinstance(clean[key], list):
            return {}, {
                "error": "invalid_action_args",
                "message": f"{key} must be an array",
                "allowed_args": sorted(allowed),
            }
    if action == "stop_needs_review" and len(str(clean.get("reason", "")).strip()) < 4:
        return {}, {
            "error": "invalid_action_args",
            "message": "stop_needs_review requires a specific non-empty reason",
            "allowed_args": sorted(allowed),
        }
    return clean, None


def calculate_agent_call_budget(
    scene_count: int,
    chunk_count: int,
    max_revisions: int,
    requested: int | None = None,
) -> tuple[int, dict]:
    """Return the explicit budget or the bounded complexity-based default."""
    scene_count = max(1, int(scene_count))
    chunk_count = max(1, int(chunk_count))
    max_revisions = max(0, int(max_revisions))
    convergence_extension_calls = 3 * min(scene_count, 3) if max_revisions else 0
    calculated = max(
        20,
        scene_count + chunk_count + 9
        + max_revisions * (min(scene_count, 3) + 3)
        + convergence_extension_calls,
    )
    effective = int(requested) if requested is not None else min(AUTO_MAX_CALLS_CAP, calculated)
    factors = {
        "mode": "explicit" if requested is not None else "auto",
        "scene_count": scene_count,
        "chunk_count": chunk_count,
        "max_revisions_per_scene": max_revisions,
        "convergence_extension_per_improving_scene": 1 if max_revisions else 0,
        "convergence_extension_calls": convergence_extension_calls,
        "uncapped_calls": calculated,
        "auto_cap": AUTO_MAX_CALLS_CAP,
    }
    if requested is not None and int(requested) < min(AUTO_MAX_CALLS_CAP, calculated):
        factors["warning"] = "explicit_budget_below_automatic_safe_default"
        factors["recommended_minimum_calls"] = min(AUTO_MAX_CALLS_CAP, calculated)
        factors["may_stop_before_revision_closure"] = True
    return max(1, effective), factors


def _call_dir(state: SupervisorState) -> str:
    path = os.path.join(state["run_dir"], "model_calls")
    os.makedirs(path, exist_ok=True)
    return path


def _call_record_path(state: SupervisorState, call_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", call_id)[:140]
    return os.path.join(_call_dir(state), safe_id + ".json")


def _count_model_calls(state: SupervisorState) -> int:
    path = os.path.join(state["run_dir"], "model_calls")
    if not os.path.isdir(path):
        return 0
    return len([name for name in os.listdir(path) if name.endswith(".json")])


def _call_text_cached(
    state: SupervisorState,
    call_id: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
) -> str | None:
    """Cache each actual model call before downstream processing.

    If a graph node fails after the provider returned, resume reuses this file
    instead of charging for or repeating the same call.
    """
    path = _call_record_path(state, call_id)
    cached = read_json(path)
    if isinstance(cached, dict) and cached.get("status") == "complete":
        response = cached.get("response")
        return response if isinstance(response, str) and response.strip() else None
    if isinstance(cached, dict) and cached.get("status") in {"invalid_response", "invalid_contract"}:
        return None
    if _count_model_calls(state) >= state["max_calls"]:
        raise ModelBudgetExhausted("model call budget exhausted")
    response = call_llm(system, user, max_tokens=max_tokens, temperature=temperature)
    safe_response = _redact_configured_secret(response) if isinstance(response, str) else None
    if not isinstance(safe_response, str) or not safe_response.strip():
        atomic_write_json(path, {
            "call_id": call_id,
            "status": "invalid_response",
            "error": "provider_returned_empty_response",
            "completed_at": _now(),
        })
        return None
    # Persist only the structured JSON envelope. Explanatory prose outside the
    # object could contain hidden reasoning and is unnecessary for resume.
    cached_response = safe_response
    if isinstance(safe_response, str):
        parsed = parse_json_response(safe_response)
        if isinstance(parsed, dict):
            cached_response = json.dumps(_safe_value(parsed), ensure_ascii=False)
        else:
            envelope = re.search(r"\{.*\}", safe_response, flags=re.DOTALL)
            cached_response = envelope.group(0) if envelope else ""
    atomic_write_json(path, {
        "call_id": call_id,
        "status": "complete",
        "response": cached_response,
        "completed_at": _now(),
    })
    return safe_response


def _call_json_cached(
    state: SupervisorState,
    call_id: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
) -> dict | None:
    response = _call_text_cached(
        state, call_id, system, user,
        max_tokens=max_tokens, temperature=temperature,
    )
    parsed = parse_json_response(response) if response else None
    if isinstance(parsed, dict):
        return parsed
    if not response:
        retried = _call_text_cached(
            state,
            call_id + "_empty_retry",
            system + " The previous provider response was empty. Return the required JSON object now.",
            user,
            max_tokens=max_tokens,
            temperature=0,
        )
        parsed = parse_json_response(retried) if retried else None
        return parsed if isinstance(parsed, dict) else None
    repaired = _call_text_cached(
        state,
        call_id + "_repair",
        "Repair malformed JSON. Return one legal JSON object only; preserve meaning.",
        "Repair this response:\n" + response[:30000],
        max_tokens=max_tokens,
        temperature=0,
    )
    parsed = parse_json_response(repaired) if repaired else None
    return parsed if isinstance(parsed, dict) else None


def _mark_call_invalid_contract(state: SupervisorState, call_id: str, errors: list[str]) -> None:
    """Keep a paid response for audit/resume, but never replay it as usable output."""
    path = _call_record_path(state, call_id)
    record = read_json(path)
    payload = dict(record) if isinstance(record, dict) else {"call_id": call_id}
    payload.update({
        "status": "invalid_contract",
        "contract_errors": [str(error) for error in errors if str(error)],
        "validated_at": _now(),
    })
    atomic_write_json(path, payload)


def _scene_shots_contract_errors(
    value: Any,
    *,
    scene_id: str,
    allowed_beat_ids: set[str],
    require_source_beat_ids: bool = True,
    required_beat_ids: set[str] | None = None,
    required_shot_ids: set[str] | None = None,
) -> list[str]:
    shots = value.get("shots") if isinstance(value, dict) else None
    if not isinstance(shots, list) or not shots:
        return ["shots_must_be_a_non_empty_array"]
    errors: list[str] = []
    seen_ids: set[str] = set()
    covered_beat_ids: set[str] = set()
    for index, shot in enumerate(shots):
        prefix = f"shots[{index}]"
        if not isinstance(shot, dict):
            errors.append(f"{prefix}_must_be_an_object")
            continue
        shot_id = str(shot.get("shot_id", "")).strip()
        if not shot_id or not shot_id.startswith(scene_id + "."):
            errors.append(f"{prefix}.shot_id_must_belong_to_scene_{scene_id}")
        elif shot_id in seen_ids:
            errors.append(f"{prefix}.shot_id_must_be_unique")
        else:
            seen_ids.add(shot_id)
        beat_ids = shot.get("source_beat_ids")
        if require_source_beat_ids and (not isinstance(beat_ids, list) or not beat_ids):
            errors.append(f"{prefix}.source_beat_ids_must_be_non_empty")
        elif isinstance(beat_ids, list):
            normalized_beat_ids = {str(beat_id) for beat_id in beat_ids if str(beat_id)}
            covered_beat_ids.update(normalized_beat_ids)
            unknown = sorted(normalized_beat_ids - allowed_beat_ids)
            if unknown:
                errors.append(f"{prefix}.source_beat_ids_are_not_allowed:{','.join(unknown)}")
        if not isinstance(shot.get("visual"), dict):
            errors.append(f"{prefix}.visual_must_be_an_object")
    missing_beats = sorted((required_beat_ids or set()) - covered_beat_ids)
    if missing_beats:
        errors.append("scene.source_beat_ids_missing_required:" + ",".join(missing_beats))
    missing_shots = sorted((required_shot_ids or set()) - seen_ids)
    if missing_shots:
        errors.append("scene.untargeted_shot_ids_missing:" + ",".join(missing_shots))
    return errors


def _call_scene_shots_with_contract(
    state: SupervisorState,
    call_id: str,
    system: str,
    user: str,
    *,
    scene_id: str,
    allowed_beat_ids: set[str],
    require_source_beat_ids: bool = True,
    required_beat_ids: set[str] | None = None,
    required_shot_ids: set[str] | None = None,
    max_tokens: int,
    temperature: float,
) -> tuple[dict | None, list[str]]:
    result = _call_json_cached(
        state, call_id, system, user,
        max_tokens=max_tokens, temperature=temperature,
    )
    errors = _scene_shots_contract_errors(
        result, scene_id=scene_id, allowed_beat_ids=allowed_beat_ids,
        require_source_beat_ids=require_source_beat_ids,
        required_beat_ids=required_beat_ids,
        required_shot_ids=required_shot_ids,
    )
    if not errors:
        return result, []
    _mark_call_invalid_contract(state, call_id, errors)
    retry_id = call_id + "_contract_retry"
    retry = _call_json_cached(
        state,
        retry_id,
        system + " The prior object violated the scene contract. Correct every listed error.",
        user + "\nInvalid object:\n" + json.dumps(_safe_value(result), ensure_ascii=False)
        + "\nContract errors:\n" + json.dumps(errors, ensure_ascii=False),
        max_tokens=max_tokens,
        temperature=0,
    )
    retry_errors = _scene_shots_contract_errors(
        retry, scene_id=scene_id, allowed_beat_ids=allowed_beat_ids,
        require_source_beat_ids=require_source_beat_ids,
        required_beat_ids=required_beat_ids,
        required_shot_ids=required_shot_ids,
    )
    if retry_errors:
        _mark_call_invalid_contract(state, retry_id, retry_errors)
        return None, retry_errors
    return retry, []


def _valid_source_analysis(value: Any, chunk: str) -> bool:
    """Require a minimally useful, source-grounded semantic extraction."""
    if not isinstance(value, dict) or not str(value.get("summary", "")).strip():
        return False
    beats = value.get("beats")
    if not isinstance(beats, list) or not beats:
        return False
    for beat in beats:
        if not isinstance(beat, dict):
            return False
        quote = str(beat.get("source_quote", "")).strip()
        if not quote or quote not in chunk:
            return False
    entities = value.get("entities", [])
    return isinstance(entities, list)


def _initialize_node(state: SupervisorState) -> dict:
    return {
        "chunks": [],
        "summaries": [],
        "source_model": {},
        "plan_errors": [],
        "completed_scenes": [],
        "validation_errors": None,
        "source_issues": None,
        "shootability_issues": None,
        "review": None,
        "review_history": [],
        "last_verified_storyboard": {},
        "last_verified_fingerprint": "",
        "candidate_fingerprint": "",
        "audited_fingerprint": "",
        "verification_state": "not_audited",
        "pending_revision": {},
        "pending_action": {},
        "last_result": {},
        "action_history": [],
        "tool_counts": {},
        "model_calls": _count_model_calls(state),
        "tool_steps": 0,
        "revision_counts": {},
        "revision_attempt_counts": {},
        "revision_extension_counts": {},
        "failed_revision_contracts": [],
        "invalid_action_count": 0,
        "no_progress_count": 0,
        "last_no_progress_signature": "",
        "status": "running",
        "stop_reason": "",
    }


def _scene_ids_from_plan(state: SupervisorState) -> list[str]:
    plan = state.get("plan", {})
    return [
        str(scene.get("scene_id") or index + 1)
        for index, scene in enumerate(plan.get("scenes", []))
        if isinstance(scene, dict)
    ] if isinstance(plan, dict) else []


def _completed_scene_ids(state: SupervisorState) -> list[str]:
    return [
        str(scene.get("scene_id", ""))
        for scene in state.get("completed_scenes", [])
        if isinstance(scene, dict)
    ]


def _plan_issue_records(errors: Any) -> list[dict]:
    return [{
        "issue_id": content_fingerprint(str(problem), length=16),
        "scene_id": "",
        "shot_ids": [],
        "category": "planning",
        "severity": "high",
        "blocking": True,
        "problem": str(problem),
        "instruction": "Repair the plan before generating dependent scenes.",
        "source_evidence": [],
        "storyboard_evidence": [],
        "origin": "deterministic",
    } for problem in errors if str(problem).strip()] if isinstance(errors, list) else []


def _blocking_review_issues(review: Any) -> list[dict] | None:
    if not isinstance(review, dict):
        return None
    value = review.get("blocking_issues")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _completion_blockers(state: SupervisorState) -> list[str]:
    blockers: list[str] = []
    source_model = state.get("source_model")
    if not isinstance(source_model, dict):
        blockers.append("a valid source model is required")
    elif source_model.get("integrity_errors"):
        integrity_errors = [
            str(value) for value in source_model.get("integrity_errors", []) if str(value)
        ]
        blockers.append(
            "source model integrity checks have blocking issues: "
            + ", ".join(integrity_errors)
        )
    if not isinstance(state.get("plan"), dict) or state.get("plan_errors"):
        blockers.append("a valid scene plan is required")
    planned = _scene_ids_from_plan(state)
    completed = _completed_scene_ids(state)
    missing = [scene_id for scene_id in planned if scene_id not in completed]
    if not planned or missing or len(completed) != len(planned):
        blockers.append("all planned scenes must be generated")
    storyboard = state.get("storyboard")
    if not isinstance(storyboard, dict):
        blockers.append("storyboard must be assembled")
    else:
        direct_schema = validate_storyboard(storyboard)
        if direct_schema:
            blockers.append("storyboard v2 schema is invalid")
        direct_quality = _deterministic_quality_issues(storyboard, state.get("input_text", ""))
        if direct_quality:
            blockers.append("deterministic storyboard checks have blocking issues")
    candidate_fingerprint = content_fingerprint(storyboard, length=24) if isinstance(storyboard, dict) else ""
    if not candidate_fingerprint or state.get("audited_fingerprint") != candidate_fingerprint:
        blockers.append("the current storyboard candidate requires a combined audit")
    if state.get("validation_errors") is None:
        blockers.append("schema validation tool has not run on the current storyboard")
    elif state.get("validation_errors"):
        blockers.append("schema validation has blocking issues")
    if state.get("source_issues") is None:
        blockers.append("source comparison has not run on the current storyboard")
    elif state.get("source_issues"):
        blockers.append("source comparison has blocking issues")
    if state.get("shootability_issues") is None:
        blockers.append("shootability inspection has not run on the current storyboard")
    elif state.get("shootability_issues"):
        blockers.append("shootability inspection has blocking issues")
    model_blockers = _blocking_review_issues(state.get("review"))
    if model_blockers is None:
        blockers.append("independent storyboard review has not run on the current storyboard")
    elif model_blockers:
        blockers.append("independent storyboard review has blocking issues")
    return blockers


def _supervisor_snapshot(state: SupervisorState) -> dict:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    source_context = (
        "\n\n".join(state.get("summaries", []))
        if state.get("summaries") else state.get("input_text", "")
    )
    allowed_actions = list(ALLOWED_ACTIONS)
    recommended_action = ""
    candidate = content_fingerprint(state.get("storyboard"), length=24) if state.get("storyboard") else ""
    if state.get("pending_revision") and state.get("audited_fingerprint") != candidate:
        allowed_actions = ["review_storyboard", "stop_needs_review"]
        recommended_action = "review_storyboard"
    return {
        "target_scene_count": state["scene_count"],
        "source_context": source_context[:3000],
        "source_analyzed": bool(state.get("chunks")),
        "source_model": {
            "beat_ids": [
                item.get("beat_id") for item in state.get("source_model", {}).get("beats", [])
                if isinstance(item, dict)
            ],
            "relations": state.get("source_model", {}).get("relations", []),
            "ending_constraint": state.get("source_model", {}).get("ending_constraint", {}),
            "integrity_errors": state.get("source_model", {}).get("integrity_errors", []),
        },
        "plan_exists": isinstance(state.get("plan"), dict),
        "plan_errors": _plan_issue_records(state.get("plan_errors", [])),
        "planned_scene_ids": _scene_ids_from_plan(state),
        "completed_scene_ids": _completed_scene_ids(state),
        "storyboard_assembled": isinstance(state.get("storyboard"), dict),
        "schema_issues": state.get("validation_errors"),
        "source_issues": state.get("source_issues"),
        "shootability_issues": state.get("shootability_issues"),
        "review_blocking_issues": review.get("blocking_issues") if review else None,
        "revision_counts": state.get("revision_counts", {}),
        "revision_attempt_counts": state.get("revision_attempt_counts", {}),
        "verification_state": state.get("verification_state", "not_audited"),
        "last_result": state.get("last_result", {}),
        "tool_steps": state.get("tool_steps", 0),
        "model_calls": _count_model_calls(state),
        "budgets": {
            "max_steps": state["max_steps"],
            "max_calls": state["max_calls"],
            "remaining_calls": max(0, state["max_calls"] - _count_model_calls(state)),
            "max_revisions_per_scene": state["max_revisions"],
        },
        "completion_blockers": _completion_blockers(state),
        "allowed_actions": allowed_actions,
        "recommended_action": recommended_action,
    }


def _supervisor_node(state: SupervisorState) -> dict:
    candidate = content_fingerprint(state.get("storyboard"), length=24) if state.get("storyboard") else ""
    if state.get("pending_revision") and state.get("audited_fingerprint") != candidate:
        return {
            "pending_action": {
                "action": "review_storyboard",
                "args": {},
                "decision_summary": "Code-required combined audit after candidate revision.",
                "protocol_error": None,
            },
            "model_calls": _count_model_calls(state),
        }
    model_calls = _count_model_calls(state)
    if state.get("tool_steps", 0) >= state["max_steps"]:
        return {
            "status": "needs_review",
            "stop_reason": "budget_exhausted",
            "model_calls": model_calls,
        }
    if model_calls >= state["max_calls"]:
        return {
            "status": "needs_review",
            "stop_reason": "budget_exhausted",
            "model_calls": model_calls,
        }
    system = (
        "You are a storyboard supervisor Agent. Select exactly one tool action based on current "
        "evidence; do not follow a fixed sequence. Return one JSON object only with action, args, "
        "and a short decision_summary. Never include hidden reasoning or credentials. Available "
        "actions: " + ", ".join(ALLOWED_ACTIONS) + ". Obey allowed_actions in the state snapshot. "
        "Arguments are strict: revise_plan accepts "
        "issue_ids; generate_scenes/compare_source/inspect_shootability accept scene_ids; "
        "revise_scenes accepts only scene_ids and current blocking issue_ids. Use revise_plan for "
        "planning defects and revise_scenes only for defects in generated shots. After any assembled "
        "or revised candidate, prefer review_storyboard because it runs the complete audit. "
        "Advisory issues must never be revised. Do not request media generation. "
        "finalize is only appropriate when every completion blocker is gone; Python will enforce it."
    )
    snapshot = _supervisor_snapshot(state)
    decision_id = content_fingerprint(snapshot, length=16)
    try:
        decision = _call_json_cached(
            state,
            f"supervisor_{state.get('tool_steps', 0):03d}_{decision_id}",
            system,
            "Current state:\n" + json.dumps(snapshot, ensure_ascii=False),
            max_tokens=900,
            temperature=0.1,
        )
    except ModelBudgetExhausted:
        return {
            "status": "needs_review",
            "stop_reason": "budget_exhausted",
            "model_calls": _count_model_calls(state),
        }
    if not isinstance(decision, dict):
        action = "__invalid_json__"
        args = {}
        protocol_error = None
        summary = "Supervisor did not return a valid JSON action."
    else:
        action = str(decision.get("action", "")).strip()
        args, protocol_error = _validate_action_args(action, decision.get("args"))
        summary = _redact_configured_secret(str(decision.get("decision_summary", "")))[:500]
    return {
        "pending_action": {
            "action": action,
            "args": args,
            "decision_summary": summary,
            "protocol_error": protocol_error,
        },
        "model_calls": _count_model_calls(state),
    }


def _plan_input(state: SupervisorState) -> str:
    chunks = state.get("chunks", [])
    summaries = state.get("summaries", [])
    if len(chunks) <= 1:
        return state["input_text"]
    return "\n\n".join(
        f"[Chunk {index}] {summary}" for index, summary in enumerate(summaries, 1)
    )


def _source_sentences(text: str) -> list[str]:
    # A regex-only split breaks Chinese dialogue because punctuation normally
    # appears before the closing quote. Keep quoted speech and its attribution
    # in the same evidence span so dialogue, speakers and ending constraints do
    # not degrade into standalone closing-quote beats.
    pieces: list[str] = []
    pending: list[str] = []
    quote_stack: list[str] = []
    quote_pairs = {"\u201c": "\u201d", "\u2018": "\u2019", "\u300c": "\u300d", "\u300e": "\u300f"}
    sentence_ends = set(".!?;\u3002\uff01\uff1f\uff1b")

    def flush() -> None:
        value = "".join(pending).strip()
        if value:
            pieces.append(value)
        pending.clear()

    for character in text:
        if character in "\r\n":
            if quote_stack:
                pending.append(" ")
            else:
                flush()
            continue
        pending.append(character)
        if character in quote_pairs:
            quote_stack.append(quote_pairs[character])
        elif quote_stack and character == quote_stack[-1]:
            quote_stack.pop()
            if not quote_stack and len(pending) >= 2 and pending[-2] in sentence_ends:
                flush()
        elif character == '"':
            if quote_stack and quote_stack[-1] == '"':
                quote_stack.pop()
                if not quote_stack and len(pending) >= 2 and pending[-2] in sentence_ends:
                    flush()
            else:
                quote_stack.append('"')
        if character in sentence_ends and not quote_stack:
            flush()
    flush()
    if len(pieces) <= 200:
        return pieces or ([text.strip()] if text.strip() else [])
    # Very short sentences are grouped to keep the semantic model bounded for
    # long prose while retaining exact contiguous source evidence.
    grouped: list[str] = []
    pending_text = ""
    for piece in pieces:
        pending_text = (pending_text + " " + piece).strip()
        if len(pending_text) >= 160:
            grouped.append(pending_text)
            pending_text = ""
    if pending_text:
        grouped.append(pending_text)
    return grouped or ([text.strip()] if text.strip() else [])


def _quoted_dialogue(text: str) -> list[str]:
    values: list[str] = []
    for pattern in (r"\u201c([^\u201d]{1,500})\u201d", r'"([^"\r\n]{1,500})"',
                    r"\u300c([^\u300d]{1,500})\u300d", r"\u300e([^\u300f]{1,500})\u300f"):
        values.extend(match.strip() for match in re.findall(pattern, text) if match.strip())
    return list(dict.fromkeys(values))


def _infer_dialogue_speaker(
    beat_text: str,
    line: str,
    candidate_names: list[str] | None = None,
) -> str:
    """Resolve explicit nearby attribution without inventing a character."""
    escaped = re.escape(line)
    chinese_name = r"([\u4e00-\u9fff]{1,8}?)"
    chinese_modifier = (
        r"(?:低声|轻声|高声|大声|小声|沉声|厉声|冷冷|平静|急切|喃喃)?(?:地)?"
    )
    chinese_verb = r"(?:说道|说|追问|反问|问道|问|回答|答道|喊道|喊|叫道|警告|补充|低语|开口道|道)"
    english_name = r"([A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*)?)"
    english_verb = r"(?:said|asked|replied|answered|warned|shouted|whispered)"
    patterns = (
        rf"{chinese_name}{chinese_modifier}{chinese_verb}\s*[:：,，]?\s*[“\"]{escaped}[”\"]",
        rf"[“\"]{escaped}[”\"]\s*[,，]?\s*{chinese_name}{chinese_modifier}{chinese_verb}",
        rf"{english_name}\s+{english_verb}\s*[:,]?\s*[“\"]{escaped}[”\"]",
        rf"[“\"]{escaped}[”\"]\s*[,]\s*{english_name}\s+{english_verb}",
    )
    for pattern in patterns:
        match = re.search(pattern, beat_text, re.I)
        if match:
            return str(match.group(1)).strip()
    # Providers sometimes classify reported speech as dialogue without
    # returning its explicit speaker. Resolve only a character name already
    # grounded by source extraction and only when it is the grammatical
    # subject of the attribution clause. Walking clauses backwards avoids
    # mistaking an object in constructs such as "A looks at B, then says..."
    # for the speaker.
    names = sorted(
        {
            str(value).strip() for value in (candidate_names or [])
            if str(value).strip() and str(value).strip() in beat_text
        },
        key=len,
        reverse=True,
    )
    if not names or line not in beat_text:
        return ""
    line_offset = beat_text.find(line)
    prefix = beat_text[:line_offset]
    report = re.search(
        r"(?:说道|说|表示|告诉|告知|称|提到|解释|回答|答道|追问|反问|问道|问|"
        r"询问|提醒|警告|补充|低语|开口道|道)\s*[:：,，]?\s*$",
        prefix,
    )
    if not report:
        return ""
    attribution_prefix = prefix[:report.start()]
    clauses = [value.strip() for value in re.split(r"[，,；;。！？!?]", attribution_prefix)]
    for clause in reversed(clauses):
        if not clause:
            continue
        for name in names:
            if clause.startswith(name):
                return name
    return ""


def _normalize_dialogue(
    value: Any,
    beat_text: str,
    candidate_names: list[str] | None = None,
) -> list[dict]:
    """Normalize provider dialogue variants and keep only beat-grounded lines."""
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, str):
            speaker, line = "", item.strip()
        elif isinstance(item, dict):
            speaker = str(
                item.get("speaker") or item.get("character") or item.get("name") or ""
            ).strip()
            line = str(
                item.get("line") or item.get("text") or item.get("dialogue")
                or item.get("content") or ""
            ).strip()
        else:
            continue
        # A model sometimes attaches the next beat's dialogue to a wider raw
        # extraction object. Exact source grounding prevents that field drift.
        if not line or line not in beat_text:
            continue
        if not speaker:
            speaker = _infer_dialogue_speaker(beat_text, line, candidate_names)
        key = (speaker, line)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"speaker": speaker, "line": line})
    return normalized


def _anchored_beat(chunk_beats: list[dict], source_quote: str) -> dict | None:
    """Resolve a model extraction object to exactly one deterministic beat.

    A quote spanning multiple deterministic beats is deliberately rejected:
    copying all of its facts onto the first match caused adjacent dialogue and
    ending constraints to leak into the wrong beat.
    """
    contained = [
        beat for beat in chunk_beats if source_quote in str(beat.get("text", ""))
    ]
    if len(contained) == 1:
        return contained[0]
    covering = [
        beat for beat in chunk_beats if str(beat.get("text", "")) in source_quote
    ]
    return covering[0] if len(covering) == 1 else None


def _validate_source_model_integrity(model: dict) -> list[str]:
    """Return code-owned consistency errors without relying on model prose."""
    errors: list[str] = []
    beats = [item for item in model.get("beats", []) if isinstance(item, dict)]
    beat_ids = [str(item.get("beat_id", "")) for item in beats]
    if len(beat_ids) != len(set(beat_ids)) or any(not value for value in beat_ids):
        errors.append("beat_ids_not_unique")
    valid_ids = set(beat_ids)
    for beat in beats:
        text = str(beat.get("text", ""))
        for dialogue in beat.get("dialogue", []):
            if not isinstance(dialogue, dict) or str(dialogue.get("line", "")) not in text:
                errors.append(f"ungrounded_dialogue:{beat.get('beat_id', '')}")
            elif not str(dialogue.get("speaker", "")).strip():
                errors.append(f"unresolved_dialogue_speaker:{beat.get('beat_id', '')}")
    for prop in model.get("props", []):
        if not isinstance(prop, dict):
            errors.append("invalid_prop")
            continue
        if any(str(value) not in valid_ids for value in prop.get("beat_ids", [])):
            errors.append(f"invalid_prop_beat:{prop.get('prop_id', '')}")
    ending = model.get("ending_constraint", {})
    if ending and str(ending.get("beat_id", "")) != (beat_ids[-1] if beat_ids else ""):
        errors.append("ending_not_last_beat")
    return sorted(set(errors))


def _entity_kind(value: Any) -> str:
    kind = str(value or "entity").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "person": "character", "people": "character", "人物": "character", "角色": "character",
        "object": "prop", "item": "prop", "key_prop": "prop", "key_object": "prop",
        "道具": "prop", "关键道具": "prop", "核心道具": "prop", "物品": "prop", "物件": "prop",
        "place": "location", "setting": "location", "scene": "location",
        "地点": "location", "场景": "location", "场所": "location", "环境": "location",
    }
    return aliases.get(kind, kind or "entity")


def _asset_kind(value: Any) -> str:
    """Normalize semantic production roles without guessing from story keywords."""
    kind = str(value or "story_key_prop").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "costume": "wardrobe", "clothing": "wardrobe", "wearable": "wardrobe",
        "服装": "wardrobe", "衣着": "wardrobe", "穿戴": "wardrobe",
        "portable": "portable_prop", "hand_prop": "portable_prop", "handheld": "portable_prop",
        "手持道具": "portable_prop", "随身道具": "portable_prop",
        "environment": "set_piece", "environmental": "set_piece", "structure": "set_piece",
        "布景": "set_piece", "场景构件": "set_piece", "固定装置": "set_piece",
        "key_prop": "story_key_prop", "plot_prop": "story_key_prop", "prop": "story_key_prop",
        "关键道具": "story_key_prop", "剧情道具": "story_key_prop",
    }
    normalized = aliases.get(kind, kind)
    return normalized if normalized in {
        "wardrobe", "portable_prop", "set_piece", "story_key_prop",
    } else "story_key_prop"


def _prop_payload(item: dict, index: int) -> dict:
    """Keep the source-grounded fields needed for downstream asset planning."""
    aliases = item.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    lifecycle = item.get("lifecycle", [])
    if not isinstance(lifecycle, list):
        lifecycle = []
    physical_spec = item.get("physical_spec", {})
    if not isinstance(physical_spec, dict):
        physical_spec = {}
    return {
        "prop_id": str(item.get("prop_id") or f"prop_{index:03d}"),
        "name": str(item.get("name", "")),
        "source_quote": str(item.get("source_quote", "")),
        "description": str(item.get("description", "")),
        "aliases": list(dict.fromkeys(
            str(value).strip() for value in aliases if str(value).strip()
        )),
        "beat_ids": [str(value) for value in item.get("beat_ids", []) if str(value)],
        "continuity_required": bool(item.get("continuity_required", True)),
        "asset_kind": _asset_kind(item.get("asset_kind")),
        "physical_spec": _safe_value(physical_spec),
        "lifecycle": [_safe_value(value) for value in lifecycle if isinstance(value, dict)],
    }


def _build_source_model(chunks: list[str], analyses: list[dict] | None = None) -> dict:
    beats: list[dict] = []
    entities: list[dict] = []
    entity_names: set[str] = set()
    chunk_models: list[dict] = []
    for chunk_index, chunk in enumerate(chunks, 1):
        chunk_beat_ids: list[str] = []
        for sentence in _source_sentences(chunk):
            beat_id = f"beat_{len(beats) + 1:04d}"
            chunk_beat_ids.append(beat_id)
            dialogue = _normalize_dialogue(_quoted_dialogue(sentence), sentence)
            speakers = re.findall(
                r"(?:^|[\n.!?\u3002\uff01\uff1f])\s*([A-Z][A-Za-z0-9_-]{0,30}|[\u4e00-\u9fff]{1,8})\s*[:\uff1a]",
                sentence,
            )
            for name in speakers:
                clean = name.strip()
                if clean and clean.casefold() not in entity_names:
                    entity_names.add(clean.casefold())
                    entities.append({
                        "entity_id": f"entity_{len(entities) + 1:03d}",
                        "name": clean,
                        "kind": "character",
                    })
            beats.append({
                "beat_id": beat_id,
                "chunk_id": chunk_index,
                "order": len(beats) + 1,
                "text": sentence,
                "dialogue": dialogue,
                "required": True,
            })
        chunk_models.append({
            "chunk_id": chunk_index,
            "beat_ids": chunk_beat_ids,
            "source_sha": content_fingerprint(chunk, length=20),
        })
    relations: list[dict] = []
    simultaneous = re.compile(r"\b(?:simultaneously|meanwhile|at the same time)\b|\u540c\u65f6|\u4e0e\u6b64\u540c\u65f6", re.I)
    causal = re.compile(r"\b(?:because|therefore|so that|as a result)\b|\u56e0\u4e3a|\u56e0\u6b64|\u6240\u4ee5", re.I)
    for previous, current in zip(beats, beats[1:]):
        relation_type = "simultaneous" if simultaneous.search(current["text"]) else "before"
        relation = {
            "relation_id": f"relation_{len(relations) + 1:04d}",
            "type": relation_type,
            "from_beat_id": previous["beat_id"],
            "to_beat_id": current["beat_id"],
        }
        if causal.search(current["text"]):
            relation["causal"] = True
        relations.append(relation)
    ending = beats[-1] if beats else None
    model = {
        "version": "1",
        "chunks": chunk_models,
        "entities": entities,
        "beats": beats,
        "relations": relations,
        "required_fact_ids": [beat["beat_id"] for beat in beats if beat.get("required")],
        "ending_constraint": {
            "beat_id": ending["beat_id"],
            "text": ending["text"],
        } if ending else {},
    }
    analyses = analyses if isinstance(analyses, list) else []
    for chunk_index, analysis in enumerate(analyses, 1):
        if not isinstance(analysis, dict) or chunk_index > len(chunks):
            continue
        chunk = chunks[chunk_index - 1]
        raw_entities = analysis.get("entities", analysis.get("characters", []))
        if isinstance(raw_entities, list):
            for raw in raw_entities:
                if isinstance(raw, str):
                    raw_payload: dict = {"name": raw}
                    name, kind = raw.strip(), "entity"
                elif isinstance(raw, dict):
                    raw_payload = raw
                    name = str(raw.get("name") or raw.get("entity") or "").strip()
                    kind = _entity_kind(raw.get("kind") or raw.get("type") or "entity")
                else:
                    continue
                aliases = raw_payload.get("aliases", [])
                if isinstance(aliases, str):
                    aliases = [aliases]
                aliases = [str(value).strip() for value in aliases if str(value).strip()]
                requested_quote = str(
                    raw_payload.get("source_quote") or raw_payload.get("quote") or ""
                ).strip()
                source_quote = next((
                    candidate for candidate in [requested_quote, *aliases, name]
                    if candidate and candidate in chunk
                ), "")
                # A canonical entity name may normalize the source wording.
                # Exact source_quote/alias evidence is sufficient grounding.
                if not name or not source_quote:
                    continue
                identity_keys = {name.casefold(), source_quote.casefold()}
                identity_keys.update(alias.casefold() for alias in aliases)
                existing_keys = {
                    str(value).strip().casefold()
                    for entity in model["entities"]
                    if isinstance(entity, dict) and _entity_kind(entity.get("kind")) == kind
                    for value in [
                        entity.get("name", ""), entity.get("source_quote", ""),
                        *(entity.get("aliases", []) if isinstance(entity.get("aliases"), list) else []),
                    ]
                    if str(value).strip()
                }
                if identity_keys.intersection(existing_keys):
                    continue
                entity_names.add(name.casefold())
                beat_ids = [
                    str(beat.get("beat_id")) for beat in model["beats"]
                    if source_quote in str(beat.get("text", "")) or name in str(beat.get("text", ""))
                ]
                payload = {
                    "entity_id": f"entity_{len(model['entities']) + 1:03d}",
                    "name": name, "kind": kind, "source_quote": source_quote,
                    "beat_ids": beat_ids,
                }
                description = str(raw_payload.get("description") or raw_payload.get("visual_description") or "").strip()
                if description:
                    payload["description"] = description
                if kind == "prop":
                    payload["prop_id"] = f"prop_{len([item for item in model['entities'] if item.get('kind') == 'prop']) + 1:03d}"
                    payload["continuity_required"] = bool(raw_payload.get("required_visual_consistency", True))
                    payload["asset_kind"] = _asset_kind(raw_payload.get("asset_kind") or raw_payload.get("production_role"))
                    payload["aliases"] = [
                        str(value).strip() for value in aliases
                        if str(value).strip() and str(value).strip() in chunk
                    ]
                    physical_spec = raw_payload.get("physical_spec", {})
                    if isinstance(physical_spec, dict):
                        payload["physical_spec"] = _safe_value(physical_spec)
                    lifecycle = raw_payload.get("lifecycle", raw_payload.get("state_events", []))
                    if isinstance(lifecycle, list):
                        normalized_events: list[dict] = []
                        chunk_beats = [
                            beat for beat in model["beats"]
                            if beat.get("chunk_id") == chunk_index
                        ]
                        for event in lifecycle:
                            if not isinstance(event, dict):
                                continue
                            quote = str(event.get("source_quote") or event.get("quote") or "").strip()
                            target = _anchored_beat(chunk_beats, quote) if quote and quote in chunk else None
                            beat_id = str(event.get("beat_id", "")).strip()
                            if target is not None:
                                beat_id = str(target.get("beat_id", ""))
                            if beat_id not in {str(value.get("beat_id")) for value in chunk_beats}:
                                continue
                            normalized_events.append({
                                "beat_id": beat_id,
                                "source_quote": quote,
                                "state": str(event.get("state", "")).strip(),
                                "holder": str(event.get("holder", "")).strip(),
                                "visible": event.get("visible") if isinstance(event.get("visible"), bool) else None,
                                "persists": bool(event.get("persists", False)),
                                "change": str(event.get("change", "")).strip(),
                            })
                        payload["lifecycle"] = normalized_events
                model["entities"].append(payload)
        chunk_beats = [beat for beat in model["beats"] if beat.get("chunk_id") == chunk_index]
        raw_beats = analysis.get("beats", [])
        if not isinstance(raw_beats, list):
            continue
        for raw in raw_beats:
            if not isinstance(raw, dict):
                continue
            quote = str(
                raw.get("source_quote") or raw.get("quote") or raw.get("text") or ""
            ).strip()
            if not quote or quote not in chunk:
                continue
            target = _anchored_beat(chunk_beats, quote)
            if target is None:
                continue
            candidate_speakers = [
                str(entity.get("name", "")).strip()
                for entity in model.get("entities", [])
                if isinstance(entity, dict)
                and _entity_kind(entity.get("kind")) == "character"
                and str(entity.get("name", "")).strip()
            ]
            facts = raw.get("must_preserve_facts", raw.get("required_facts", raw.get("required_fact", [])))
            if isinstance(facts, str):
                facts = [facts]
            if isinstance(facts, list):
                target["must_preserve_facts"] = [
                    str(item).strip() for item in facts if str(item).strip()
                ]
            for field, aliases in {
                "dialogue": ("dialogue", "dialogues"),
                "sounds": ("sounds", "sound"),
                "spatial_state": ("spatial_state", "space"),
            }.items():
                value = next((raw.get(alias) for alias in aliases if raw.get(alias) is not None), None)
                if value not in (None, "", []):
                    if field == "dialogue":
                        grounded = _normalize_dialogue(
                            value,
                            str(target.get("text", "")),
                            candidate_speakers,
                        )
                        if grounded:
                            target[field] = grounded
                    else:
                        target[field] = _safe_value(value)
            relation = str(raw.get("relation_to_previous", "")).strip().lower()
            if relation:
                for item in model["relations"]:
                    if item.get("to_beat_id") == target.get("beat_id"):
                        relation_evidence = str(
                            raw.get("relation_evidence")
                            or raw.get("relation_source_quote")
                            or ""
                        ).strip()
                        evidence_valid = bool(
                            relation_evidence and relation_evidence in chunk
                        )
                        # Sequential source order is the safe default. A model
                        # may upgrade it to simultaneous/causal only with an
                        # exact source span; unsupported semantic guesses must
                        # not become hard production metadata.
                        if (
                            relation in {"simultaneous", "parallel", "at_the_same_time"}
                            and evidence_valid
                            and simultaneous.search(relation_evidence)
                        ):
                            item["type"] = "simultaneous"
                            item["evidence_quote"] = relation_evidence
                        if (
                            relation in {"causal", "caused_by", "result"}
                            and evidence_valid
                            and causal.search(relation_evidence)
                        ):
                            item["causal"] = True
                            item["evidence_quote"] = relation_evidence
                        break
    model["props"] = [
        _prop_payload(item, index)
        for index, item in enumerate(
            [value for value in model["entities"] if value.get("kind") == "prop"], 1
        )
    ]
    model["required_facts"] = [
        {
            "beat_id": beat["beat_id"],
            "facts": beat.get("must_preserve_facts", [beat["text"]]),
        }
        for beat in model["beats"] if beat.get("required")
    ]
    model["integrity_errors"] = _validate_source_model_integrity(model)
    return model


def _tool_analyze_source(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    chunks = _chunk_text(state["input_text"])
    analyses: list[dict] = []
    summaries: list[str] = []
    extraction_system = (
        "SOURCE_MODEL_EXTRACTION_V2. Extract only facts supported by the supplied source chunk. "
        "Return JSON {summary, entities:[{name,kind,source_quote,description,aliases,"
        "required_visual_consistency,asset_kind,physical_spec,lifecycle:[{source_quote,state,holder,"
        "visible,persists,change}]}], beats:[{source_quote, "
        "must_preserve_facts, dialogue, sounds, spatial_state, relation_to_previous,relation_evidence}]}. "
        "source_quote must be an exact substring. relation_to_previous is before, simultaneous, "
        "or causal. relation_evidence must be an exact source substring explicitly supporting simultaneous "
        "or causal; omit it for ordinary sequence. Entity kind must distinguish character, prop and location. "
        "A prop asset_kind must be "
        "wardrobe, portable_prop, set_piece, or story_key_prop. physical_spec records only supported object "
        "class, construction/topology, material, opacity, mounting, motion and state facts. Each lifecycle event "
        "uses an exact source_quote; visible is boolean and persists is true only when the same object remains "
        "available/held until a later change. Include recurring or plot-critical "
        "visible objects as prop entities. Do not infer unseen facts and do not include reasoning."
    )
    for index, chunk in enumerate(chunks, 1):
        call_id = f"analyze_source_chunk_{index:04d}_" + content_fingerprint(chunk, length=16)
        result = _call_json_cached(
            state,
            call_id,
            extraction_system,
            f"Chunk {index}/{len(chunks)}:\n{chunk}",
            max_tokens=3500,
            temperature=0.1,
        )
        if isinstance(result, dict) and not _valid_source_analysis(result, chunk):
            repaired = _call_text_cached(
                state, call_id + "_contract_retry",
                extraction_system + " The prior object violated this contract; correct it without inventing facts.",
                "Invalid object:\n" + json.dumps(_safe_value(result), ensure_ascii=False)
                + f"\nChunk {index}/{len(chunks)}:\n{chunk}",
                max_tokens=3500, temperature=0,
            )
            repaired_json = parse_json_response(repaired) if repaired else None
            result = repaired_json if isinstance(repaired_json, dict) else None
        if not _valid_source_analysis(result, chunk):
            return {}, {
                "ok": False,
                "error": "invalid_source_analysis",
                "chunk_index": index,
                "message": "Provider did not return a source-grounded summary and beat list after retry.",
            }
        analysis = result
        analyses.append(analysis)
        summary = str(analysis.get("summary", "")).strip()
        summaries.append(summary or chunk[:2000])
    source_model = _build_source_model(chunks, analyses)
    atomic_write_json(os.path.join(state["run_dir"], "source_model.json"), source_model)
    updates = {"chunks": chunks, "summaries": summaries, "source_model": source_model}
    integrity_errors = [
        str(value) for value in source_model.get("integrity_errors", []) if str(value)
    ]
    if integrity_errors:
        updates.update({
            "status": "needs_review",
            "stop_reason": "source_model_integrity_failed",
        })
        return updates, {
            "ok": False,
            "error": "source_model_integrity_failed",
            "summary": "Source analysis contains unresolved code-owned integrity errors.",
            "beat_count": len(source_model["beats"]),
            "integrity_errors": integrity_errors,
        }
    return updates, {
        "ok": True,
        "summary": f"Analyzed {len(chunks)} source chunk(s) into {len(source_model['beats'])} semantic beats.",
        "beat_count": len(source_model["beats"]),
    }


def _normalize_plan(state: SupervisorState, plan: Any) -> tuple[dict | None, list[str]]:
    cleaned = _clean_plan_anchors(_replace_named_styles(plan))
    errors = _plan_quality_errors(cleaned, state["scene_count"])
    if isinstance(cleaned, dict):
        cleaned.setdefault("title", state["title"])
        cleaned.setdefault("creative_bible", {})
        characters = cleaned["creative_bible"].get("characters", [])
        if isinstance(characters, list):
            for index, character in enumerate(characters, 1):
                if isinstance(character, dict) and not str(character.get("character_id", "")).strip():
                    character["character_id"] = f"character_{index:03d}"
        beats = [
            str(item.get("beat_id")) for item in state.get("source_model", {}).get("beats", [])
            if isinstance(item, dict) and item.get("beat_id")
        ]
        scenes = [item for item in cleaned.get("scenes", []) if isinstance(item, dict)]
        for index, scene in enumerate(scenes):
            existing = scene.get("source_beat_ids")
            if not isinstance(existing, list) or not existing:
                start = (len(beats) * index) // max(1, len(scenes))
                end = (len(beats) * (index + 1)) // max(1, len(scenes))
                scene["source_beat_ids"] = beats[start:end]
        return cleaned, errors
    return None, errors


def _enrich_source_model_from_plan(source_model: Any, plan: dict) -> dict:
    model = json.loads(json.dumps(source_model)) if isinstance(source_model, dict) else {}
    entities = [item for item in model.get("entities", []) if isinstance(item, dict)]
    beats = [item for item in model.get("beats", []) if isinstance(item, dict)]
    by_name = {
        str(item.get("name", "")).strip().casefold(): item
        for item in entities if str(item.get("name", "")).strip()
    }
    bible = plan.get("creative_bible", {}) if isinstance(plan, dict) else {}
    characters = bible.get("characters", []) if isinstance(bible, dict) else []
    canonical_character_names: set[str] = set()
    for character in characters if isinstance(characters, list) else []:
        if not isinstance(character, dict):
            continue
        names = [
            str(character.get(key, "")).strip() for key in ("name", "name_en")
            if str(character.get(key, "")).strip()
        ]
        if not names:
            continue
        canonical_character_names.update(name.casefold() for name in names)
        beat_ids = [
            str(beat.get("beat_id")) for beat in beats
            if any(name.casefold() in str(beat.get("text", "")).casefold() for name in names)
        ]
        existing = next((by_name.get(name.casefold()) for name in names if name.casefold() in by_name), None)
        payload = existing if isinstance(existing, dict) else {
            "entity_id": f"entity_{len(entities) + 1:03d}",
            "name": names[0],
            "kind": "character",
        }
        payload["character_id"] = str(character.get("character_id", ""))
        payload["aliases"] = names
        payload["beat_ids"] = beat_ids
        if existing is None:
            entities.append(payload)
        for name in names:
            by_name[name.casefold()] = payload
    # The extraction model may return attribution phrases such as
    # "Alice whispered" as a character. Once the plan has established the
    # canonical cast, only exact cast names/aliases may remain character
    # entities. Non-character entities (props, places, sounds) are preserved.
    if canonical_character_names:
        entities = [
            item for item in entities
            if str(item.get("kind", "")).lower() != "character"
            or bool(item.get("character_id"))
            or str(item.get("name", "")).strip().casefold() in canonical_character_names
        ]
    model["entities"] = entities
    model["props"] = [
        _prop_payload(item, index)
        for index, item in enumerate(
            [value for value in entities if str(value.get("kind", "")).lower() == "prop"], 1
        )
    ]
    return model


def _tool_create_plan(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    if not state.get("chunks"):
        return {}, {"ok": False, "error": "source_not_analyzed"}
    system, user = _plan_prompts(
        _plan_input(state), state["title"], state["word_count"],
        state["scene_count"], len(state["chunks"]),
    )
    user += (
        "\nSemantic contract: every scene must include source_beat_ids selected from this Source Model. "
        "Do not invent IDs. Preserve ordered, causal, and simultaneous relationships.\nSource Model:\n"
        + json.dumps(state.get("source_model", {}), ensure_ascii=False)[:30000]
    )
    result = _call_json_cached(
        state,
        "create_plan_" + content_fingerprint(_plan_input(state), state["scene_count"], length=16),
        system, user, max_tokens=5000, temperature=0.3,
    )
    plan, errors = _normalize_plan(state, result)
    updates: dict = {"plan_errors": errors}
    if plan is not None:
        updates["plan"] = plan
        source_model = _enrich_source_model_from_plan(state.get("source_model", {}), plan)
        updates["source_model"] = source_model
        atomic_write_json(os.path.join(state["run_dir"], "source_model.json"), source_model)
        atomic_write_json(os.path.join(state["run_dir"], "plan.json"), plan)
    return updates, {
        "ok": plan is not None and not errors,
        "summary": "Plan created." if not errors else "Plan has deterministic defects.",
        "issues": _plan_issue_records(errors),
    }


def _tool_revise_plan(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    if not isinstance(state.get("plan"), dict):
        return {}, {"ok": False, "error": "plan_missing"}
    system, base_user = _plan_prompts(
        _plan_input(state), state["title"], state["word_count"],
        state["scene_count"], len(state.get("chunks", [])),
    )
    requested_ids = args.get("issue_ids") if isinstance(args.get("issue_ids"), list) else []
    issues = state.get("plan_errors", [])
    if requested_ids:
        issues = [item for item in issues if content_fingerprint(str(item), length=16) in requested_ids]
    user = (
        base_user
        + "\nCurrent plan:\n" + json.dumps(state["plan"], ensure_ascii=False)
        + "\nRepair these planning defects and return the complete plan, not a patch:\n"
        + json.dumps(issues, ensure_ascii=False)
        + "\nPreserve valid source_beat_ids and Source Model relations.\nSource Model:\n"
        + json.dumps(state.get("source_model", {}), ensure_ascii=False)[:30000]
    )
    plan_hash = content_fingerprint(state["plan"], issues, length=16)
    result = _call_json_cached(
        state, "revise_plan_" + plan_hash, system, user,
        max_tokens=5000, temperature=0.2,
    )
    plan, errors = _normalize_plan(state, result)
    if plan is None:
        return {"plan_errors": errors}, {"ok": False, "issues": errors}
    atomic_write_json(os.path.join(state["run_dir"], "plan.json"), plan)
    source_model = _enrich_source_model_from_plan(state.get("source_model", {}), plan)
    atomic_write_json(os.path.join(state["run_dir"], "source_model.json"), source_model)
    return {
        "plan": plan,
        "source_model": source_model,
        "plan_errors": errors,
        "completed_scenes": [],
        "storyboard": {},
        "validation_errors": None,
        "source_issues": None,
        "shootability_issues": None,
        "review": None,
        "revision_counts": {},
        "revision_attempt_counts": {},
        "revision_extension_counts": {},
        "candidate_fingerprint": "",
        "audited_fingerprint": "",
        "verification_state": "not_audited",
        "pending_revision": {},
    }, {
        "ok": not errors,
        "summary": "Plan revised; dependent scenes were invalidated.",
        "issues": _plan_issue_records(errors),
    }


def _requested_scene_ids(state: SupervisorState, args: dict, *, missing_only: bool) -> list[str]:
    planned = _scene_ids_from_plan(state)
    requested = args.get("scene_ids")
    if not isinstance(requested, list) or not requested:
        requested = planned
    requested = [str(item) for item in requested if str(item) in planned]
    if missing_only:
        completed = set(_completed_scene_ids(state))
        requested = [item for item in requested if item not in completed]
    return requested


def _storyboard_candidate(state: SupervisorState, scenes: list[dict]) -> dict:
    """Build a schema-normalized candidate without marking quality gates complete."""
    plan = state.get("plan", {})
    return normalize_storyboard({
        "title": plan.get("title", state["title"]),
        "creative_bible": plan.get("creative_bible", {}),
        "scenes": scenes,
    }, title=state["title"])


def _prop_active_beat_ids(prop: dict, source_model: dict) -> set[str]:
    """Resolve explicit lifecycle persistence against stable Source Model order."""
    ordered = [
        str(item.get("beat_id")) for item in source_model.get("beats", [])
        if isinstance(item, dict) and item.get("beat_id")
    ]
    positions = {beat_id: index for index, beat_id in enumerate(ordered)}
    active = {str(value) for value in prop.get("beat_ids", []) if str(value) in positions}
    events = [
        item for item in prop.get("lifecycle", [])
        if isinstance(item, dict) and str(item.get("beat_id", "")) in positions
    ]
    events.sort(key=lambda item: positions[str(item.get("beat_id"))])
    for index, event in enumerate(events):
        beat_id = str(event.get("beat_id"))
        if event.get("visible") is True:
            active.add(beat_id)
        if event.get("persists") and event.get("visible") is not False:
            start = positions[beat_id]
            end = len(ordered)
            for later in events[index + 1:]:
                if later.get("visible") is False:
                    end = positions[str(later.get("beat_id"))]
                    break
                if later.get("persists") is False:
                    # A later visible event with no persistence is evidence for
                    # that beat only. It terminates the earlier open-ended span
                    # without asserting that the asset became nonexistent.
                    end = positions[str(later.get("beat_id"))] + (
                        1 if later.get("visible") is True else 0
                    )
                    break
            active.update(ordered[start:end])
        if event.get("visible") is False:
            active.discard(beat_id)
    return active


def _lifecycle_requires_prop_in_shot(
    prop: dict, beat_ids: list[str], shot: dict, source_model: dict,
) -> bool:
    """Treat persistence as availability, constrained by a visible holder."""
    active_beats = _prop_active_beat_ids(prop, source_model)
    target_beats = [str(value) for value in beat_ids if str(value) in active_beats]
    if not target_beats:
        return False
    ordered = [
        str(item.get("beat_id")) for item in source_model.get("beats", [])
        if isinstance(item, dict) and item.get("beat_id")
    ]
    positions = {beat_id: index for index, beat_id in enumerate(ordered)}
    events = sorted(
        [
            item for item in prop.get("lifecycle", [])
            if isinstance(item, dict) and str(item.get("beat_id", "")) in positions
        ],
        key=lambda item: positions[str(item.get("beat_id"))],
    )
    holders: set[str] = set()
    for target in target_beats:
        prior = [event for event in events if positions[str(event.get("beat_id"))] <= positions[target]]
        if prior:
            holder = str(prior[-1].get("holder", "")).strip()
            if holder.casefold() not in {"", "none", "null", "unknown"}:
                holders.add(holder.casefold())
    if not holders:
        return True
    holder_character_ids: set[str] = set()
    for entity in source_model.get("entities", []):
        if not isinstance(entity, dict) or _entity_kind(entity.get("kind")) != "character":
            continue
        names = {str(entity.get("name", "")).strip().casefold()}
        aliases = entity.get("aliases", [])
        if isinstance(aliases, list):
            names.update(str(value).strip().casefold() for value in aliases if str(value).strip())
        if holders.intersection(names) and str(entity.get("character_id", "")).strip():
            holder_character_ids.add(str(entity["character_id"]))
    if not holder_character_ids:
        return True
    visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
    visible_ids = {
        str(value) for value in visual.get("visible_character_ids", []) if str(value)
    } if isinstance(visual.get("visible_character_ids"), list) else set()
    return bool(holder_character_ids.intersection(visible_ids))


def _prop_relevant_to_shot(
    prop: dict, beat_ids: list[str], shot: dict, source_model: dict | None = None,
) -> bool:
    """Return whether a source prop is required inside this particular frame.

    Scene continuity makes an asset available to the scene, not visible in
    every shot. Beat links are authoritative; descriptive text is a fallback
    for intentionally recurring props and older storyboards.
    """
    # Wardrobe is part of character identity/outfit continuity. Treating it
    # as a handheld/set prop duplicates assets and creates false per-shot
    # visible_prop_ids blockers.
    if _asset_kind(prop.get("asset_kind")) == "wardrobe":
        return False
    prop_beats = (
        _prop_active_beat_ids(prop, source_model)
        if isinstance(source_model, dict)
        else {str(value) for value in prop.get("beat_ids", []) if str(value)}
    )
    if prop_beats.intersection(beat_ids) and (
        not isinstance(source_model, dict)
        or _lifecycle_requires_prop_in_shot(prop, beat_ids, shot, source_model)
    ):
        return True
    visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
    prompts = shot.get("prompts", {}) if isinstance(shot.get("prompts"), dict) else {}
    haystack = _canonical_text(" ".join((
        str(visual.get("description", "")),
        str(visual.get("composition", "")),
        str(prompts.get("image_cn", "")),
        str(prompts.get("image_en", "")),
    )))
    names = {
        str(prop.get(key, "")).strip()
        for key in ("name", "source_quote")
        if str(prop.get(key, "")).strip()
    }
    aliases = prop.get("aliases", [])
    if isinstance(aliases, list):
        names.update(str(value).strip() for value in aliases if str(value).strip())
    return any(_canonical_text(name) in haystack for name in names if _canonical_text(name))


def _prepare_scene_semantics(state: SupervisorState, scene: dict) -> dict:
    scene = dict(scene)
    source_model = state.get("source_model", {})
    valid_beats = {
        str(item.get("beat_id")) for item in source_model.get("beats", [])
        if isinstance(item, dict) and item.get("beat_id")
    }
    scene_beats = [
        str(item) for item in scene.get("source_beat_ids", []) if str(item) in valid_beats
    ] if isinstance(scene.get("source_beat_ids"), list) else []
    scene["source_beat_ids"] = list(dict.fromkeys(scene_beats))
    source_props = [
        item for item in source_model.get("props", [])
        if isinstance(item, dict) and str(item.get("prop_id", "")).strip()
    ]
    source_props_by_id = {str(item["prop_id"]): item for item in source_props}
    scene_props = [
        dict(prop) for prop in source_props
        if not prop.get("beat_ids")
        or _prop_active_beat_ids(prop, source_model).intersection(scene_beats)
    ]
    existing_scene_props = scene.get("key_props", [])
    merged_scene_props = {
        str(item.get("prop_id")): dict(item)
        for item in existing_scene_props if isinstance(item, dict) and item.get("prop_id")
    } if isinstance(existing_scene_props, list) else {}
    for prop in scene_props:
        merged_scene_props.setdefault(str(prop["prop_id"]), prop)
    if merged_scene_props:
        scene["key_props"] = list(merged_scene_props.values())
    shots = [item for item in scene.get("shots", []) if isinstance(item, dict)]
    for index, shot in enumerate(shots):
        existing = shot.get("source_beat_ids")
        beat_ids = [str(item) for item in existing if str(item) in valid_beats] if isinstance(existing, list) else []
        if not beat_ids and scene_beats:
            start = (len(scene_beats) * index) // max(1, len(shots))
            end = (len(scene_beats) * (index + 1)) // max(1, len(shots))
            beat_ids = scene_beats[start:end] or scene_beats[-1:]
        shot["source_beat_ids"] = list(dict.fromkeys(beat_ids))
        visual = dict(shot.get("visual")) if isinstance(shot.get("visual"), dict) else {}
        declared_prop_ids = {
            str(value) for value in shot.get("visible_prop_ids", []) if str(value)
        } if isinstance(shot.get("visible_prop_ids"), list) else set()
        has_explicit_props = "key_props" in visual and isinstance(visual.get("key_props"), list)
        existing_props = visual.get("key_props", [])
        merged_props: dict[str, dict] = {}
        if isinstance(existing_props, list):
            for item in existing_props:
                if not isinstance(item, dict) or not item.get("prop_id"):
                    continue
                prop_id = str(item.get("prop_id"))
                canonical = source_props_by_id.get(prop_id)
                if canonical and _prop_relevant_to_shot(canonical, beat_ids, shot, source_model):
                    merged_props[prop_id] = {**dict(canonical), **dict(item), "prop_id": prop_id}
        if declared_prop_ids:
            for prop in source_props:
                if (
                    str(prop.get("prop_id")) in declared_prop_ids
                    and _asset_kind(prop.get("asset_kind")) != "wardrobe"
                ):
                    merged_props.setdefault(str(prop["prop_id"]), dict(prop))
            unknown_declared = declared_prop_ids - set(source_props_by_id)
            if unknown_declared:
                # Providers often invent readable IDs even when the prompt
                # supplies a canonical registry. Never persist those IDs.
                # Recover only source-owned, non-wardrobe assets whose beat
                # lifecycle or current frame text makes them relevant.
                for prop in source_props:
                    if _prop_relevant_to_shot(prop, beat_ids, shot, source_model):
                        merged_props.setdefault(str(prop["prop_id"]), dict(prop))
        elif has_explicit_props:
            # A model may keep one explicitly listed set piece while dropping
            # a persistent handheld object from later beats. Only an explicit
            # Source Model lifecycle may repair that omission automatically;
            # an authored empty list remains empty when no such evidence exists.
            for prop in source_props:
                if (
                    _asset_kind(prop.get("asset_kind")) != "wardrobe"
                    and prop.get("lifecycle")
                    and _lifecycle_requires_prop_in_shot(prop, beat_ids, shot, source_model)
                ):
                    merged_props.setdefault(str(prop["prop_id"]), dict(prop))
        elif not has_explicit_props:
            for prop in scene_props:
                if _prop_relevant_to_shot(prop, beat_ids, shot, source_model):
                    merged_props.setdefault(str(prop["prop_id"]), dict(prop))
        shot["visible_prop_ids"] = sorted(merged_props)
        if has_explicit_props or merged_props:
            # Preserve an explicit empty list. It means no key prop is visible
            # and must not be expanded back to every scene-level asset.
            visual["key_props"] = list(merged_props.values())
        shot["visual"] = visual
        if not isinstance(shot.get("temporal_relations"), list):
            shot["temporal_relations"] = [
                dict(relation) for relation in source_model.get("relations", [])
                if isinstance(relation, dict)
                and relation.get("from_beat_id") in beat_ids
                and relation.get("to_beat_id") in beat_ids
            ]
    scene["shots"] = shots
    if not isinstance(scene.get("temporal_relations"), list):
        scene["temporal_relations"] = [
            dict(relation) for relation in source_model.get("relations", [])
            if isinstance(relation, dict)
            and relation.get("from_beat_id") in scene_beats
            and relation.get("to_beat_id") in scene_beats
        ]
    return scene


def _tool_generate_scenes(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    if not isinstance(state.get("plan"), dict) or state.get("plan_errors"):
        return {}, {"ok": False, "error": "valid_plan_required"}
    targets = _requested_scene_ids(state, args, missing_only=True)
    if not targets:
        return {}, {"ok": False, "error": "no_missing_target_scenes"}
    completed = {
        str(scene.get("scene_id")): dict(scene)
        for scene in state.get("completed_scenes", []) if isinstance(scene, dict)
    }
    plan_scenes = [scene for scene in state["plan"].get("scenes", []) if isinstance(scene, dict)]
    generated_ids: list[str] = []
    plan_hash = content_fingerprint(state["plan"], length=12)
    for index, planned_scene in enumerate(plan_scenes):
        scene_id = str(planned_scene.get("scene_id") or index + 1)
        if scene_id not in targets:
            continue
        scene = {**planned_scene, "scene_id": scene_id}
        source = _scene_source(scene, state["chunks"])
        system, user = _scene_prompts(
            source, state["title"], state["plan"].get("creative_bible", {}), scene,
        )
        user += (
            "\nSemantic contract: each shot must include source_beat_ids, visual.visible_character_ids "
            "for characters actually inside the frame (not merely mentioned or off-camera), shot-level "
            "visible_prop_ids and visual.key_props for the same actually visible production assets, and "
            "temporal_relations for multiple events in one shot. Use only canonical IDs below; never invent "
            "an ID. Wardrobe is carried by the character outfit anchor and must not be repeated in "
            "visible_prop_ids or visual.key_props.\n"
            + json.dumps({
                "source_beat_ids": scene.get("source_beat_ids", []),
                "source_relations": state.get("source_model", {}).get("relations", []),
                "canonical_props": [
                    prop for prop in state.get("source_model", {}).get("props", [])
                    if isinstance(prop, dict) and (
                        not prop.get("beat_ids")
                        or set(str(value) for value in prop.get("beat_ids", [])).intersection(
                            str(value) for value in scene.get("source_beat_ids", [])
                        )
                    )
                ],
            }, ensure_ascii=False)
        )
        allowed_beat_ids = {
            str(beat.get("beat_id"))
            for beat in state.get("source_model", {}).get("beats", [])
            if isinstance(beat, dict) and str(beat.get("beat_id", ""))
        }
        result, contract_errors = _call_scene_shots_with_contract(
            state, f"generate_scene_{scene_id}_{plan_hash}", system, user,
            scene_id=scene_id,
            allowed_beat_ids=allowed_beat_ids,
            require_source_beat_ids=False,
            max_tokens=7000, temperature=0.35,
        )
        if contract_errors or not isinstance(result, dict):
            return {}, {
                "ok": False,
                "error": "scene_generation_contract_failed",
                "scene_id": scene_id,
                "contract_errors": contract_errors,
            }
        shots = result["shots"]
        completed[scene_id] = _prepare_scene_semantics(
            state,
            _prepare_generated_scene(
                {**scene, "shots": shots}, state["plan"].get("creative_bible", {}),
            ),
        )
        atomic_write_json(
            os.path.join(state["run_dir"], f"scene_{scene_id}.json"), completed[scene_id]
        )
        generated_ids.append(scene_id)
    ordered = [completed[item] for item in _scene_ids_from_plan(state) if item in completed]
    candidate = (
        _storyboard_candidate(state, ordered)
        if len(ordered) == len(_scene_ids_from_plan(state)) else {}
    )
    if candidate:
        atomic_write_json(os.path.join(state["run_dir"], "storyboard.json"), candidate)
    candidate_fingerprint = content_fingerprint(candidate, length=24) if candidate else ""
    return {
        "completed_scenes": ordered,
        "storyboard": candidate,
        "validation_errors": None,
        "source_issues": None,
        "shootability_issues": None,
        "review": None,
        "candidate_fingerprint": candidate_fingerprint,
        "audited_fingerprint": "",
        "verification_state": "unverified" if candidate else "not_assembled",
        "pending_revision": {},
    }, {"ok": True, "summary": f"Generated scenes: {', '.join(generated_ids)}"}


def _tool_assemble_storyboard(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    if not isinstance(state.get("plan"), dict):
        return {}, {"ok": False, "error": "plan_missing"}
    if set(_completed_scene_ids(state)) != set(_scene_ids_from_plan(state)):
        return {}, {"ok": False, "error": "scenes_incomplete"}
    storyboard = _storyboard_candidate(state, state["completed_scenes"])
    atomic_write_json(os.path.join(state["run_dir"], "storyboard.json"), storyboard)
    return {
        "storyboard": storyboard,
        "validation_errors": None,
        "source_issues": None,
        "shootability_issues": None,
        "review": None,
        "candidate_fingerprint": content_fingerprint(storyboard, length=24),
        "audited_fingerprint": "",
        "verification_state": "unverified",
        "pending_revision": {},
    }, {"ok": True, "summary": "Assembled storyboard v2 candidate."}


def _tool_validate_schema(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    if not isinstance(state.get("storyboard"), dict) or not state["storyboard"]:
        return {}, {"ok": False, "error": "storyboard_missing"}
    errors = validate_storyboard(state["storyboard"])
    return {"validation_errors": errors}, {
        "ok": not errors, "summary": "Schema valid." if not errors else "Schema invalid.",
        "issues": errors,
    }


def _json_path_value(payload: Any, path: str) -> tuple[bool, Any]:
    if not isinstance(path, str) or not path.startswith("$"):
        return False, None
    if not re.fullmatch(r"\$(?:(?:\.[A-Za-z_][A-Za-z0-9_-]*)|(?:\[\d+\]))*", path):
        return False, None
    current = payload
    for key, index in re.findall(r"(?:\.([A-Za-z_][A-Za-z0-9_-]*))|(?:\[(\d+)\])", path[1:]):
        if key:
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
        else:
            offset = int(index)
            if not isinstance(current, list) or offset >= len(current):
                return False, None
            current = current[offset]
    return True, current


def _scene_id_from_shots(shot_ids: list[str]) -> str:
    return shot_ids[0].split(".", 1)[0] if shot_ids and "." in shot_ids[0] else ""


def _evidence_items(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [{"reference": value.strip()}]
    if isinstance(value, list):
        result: list[dict] = []
        for item in value:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str) and item.strip():
                result.append({"reference": item.strip()})
        return result
    return []


def _normalize_model_issues(
    result: Any,
    *,
    default_category: str,
    source_model: dict | None = None,
    storyboard: dict | None = None,
) -> dict:
    raw = result.get("issues", []) if isinstance(result, dict) else []
    raw = raw if isinstance(raw, list) else []
    source_model = source_model if isinstance(source_model, dict) else {}
    storyboard = storyboard if isinstance(storyboard, dict) else {}
    beats = {
        str(item.get("beat_id")): item for item in source_model.get("beats", [])
        if isinstance(item, dict) and item.get("beat_id")
    }
    source_text = "\n".join(str(item.get("text", "")) for item in beats.values())
    issues: list[dict] = []
    blocking: list[dict] = []
    advisory: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "medium")).strip().lower()
        if severity not in {"low", "medium", "high", "critical"}:
            severity = "medium"
        original_category = str(item.get("category", default_category)).strip().lower() or default_category
        category = _canonical_review_category(original_category)
        problem = str(
            item.get("problem") or item.get("defect") or item.get("description") or ""
        ).strip()[:2000]
        instruction = str(
            item.get("instruction") or item.get("suggestion") or ""
        ).strip()[:2000]
        shot_value = item.get("shot_ids", item.get("shot_id", []))
        if isinstance(shot_value, str):
            shot_ids = [shot_value.strip()] if shot_value.strip() else []
        elif isinstance(shot_value, list):
            shot_ids = [str(value).strip() for value in shot_value if str(value).strip()]
        else:
            shot_ids = []
        scene_id = str(item.get("scene_id", "")).strip() or _scene_id_from_shots(shot_ids)
        source_evidence = _evidence_items(
            item.get("source_evidence")
            or item.get("source_beat_ids")
            or item.get("source_beat_id")
            or item.get("source_quote")
        )
        storyboard_evidence = _evidence_items(
            item.get("storyboard_evidence")
            or item.get("storyboard_paths")
            or item.get("storyboard_path")
            or item.get("json_path")
        )
        source_valid = False
        normalized_source: list[dict] = []
        for evidence in source_evidence:
            beat_id = str(evidence.get("beat_id") or evidence.get("source_beat_id") or "").strip()
            quote = str(evidence.get("quote") or evidence.get("source_quote") or "").strip()
            reference = str(evidence.get("reference", "")).strip()
            if reference in beats:
                beat_id = reference
            elif reference and reference in source_text:
                quote = reference
            valid = bool((beat_id and beat_id in beats) or (quote and quote in source_text))
            source_valid = source_valid or valid
            normalized_source.append({"beat_id": beat_id, "quote": quote, "valid": valid})
        storyboard_valid = False
        normalized_storyboard: list[dict] = []
        for evidence in storyboard_evidence:
            path = str(evidence.get("path") or evidence.get("json_path") or evidence.get("reference") or "").strip()
            found, actual = _json_path_value(storyboard, path)
            expected_present = "value" in evidence or "expected_value" in evidence
            expected = evidence.get("value", evidence.get("expected_value"))
            matches = found and expected_present and actual == expected
            storyboard_valid = storyboard_valid or matches
            normalized_storyboard.append({
                "path": path,
                "value": _safe_value(actual) if found else None,
                "valid": matches,
            })
        requested_blocking = item.get("blocking") is True or str(item.get("blocking", "")).lower() == "true"
        known_objective = category in BLOCKING_REVIEW_CATEGORIES
        subjective = original_category in SUBJECTIVE_REVIEW_CATEGORIES
        production_metadata = category in PRODUCTION_METADATA_CATEGORIES
        source_props = {
            str(prop.get("prop_id")): prop
            for prop in source_model.get("props", [])
            if isinstance(prop, dict) and str(prop.get("prop_id", ""))
        }
        referenced_prop_ids = {
            value for value in re.findall(
                r"\bprop_[A-Za-z0-9_.-]+\b",
                json.dumps(_safe_value(item), ensure_ascii=False),
            ) if value in source_props
        }
        wardrobe_only_binding = bool(
            production_metadata
            and referenced_prop_ids
            and all(
                _asset_kind(source_props[prop_id].get("asset_kind")) == "wardrobe"
                for prop_id in referenced_prop_ids
            )
        )
        # Entity/asset bindings are executable production data, not art taste.
        # Providers commonly label these medium/non-blocking even when they
        # supply exact source and current JSON evidence. Code promotes only the
        # known production categories and still requires both evidence sides.
        evidence_backed_production_blocker = bool(
            production_metadata
            and not wardrobe_only_binding
            and severity in {"medium", "high", "critical"}
            and source_valid
            and storyboard_valid
        )
        needs_classification = bool(
            requested_blocking
            and severity in {"high", "critical"}
            and not known_objective
            and not subjective
            and source_valid
            and storyboard_valid
        )
        # An unfamiliar but well-evidenced high-severity category must never be
        # silently accepted. It blocks completion for human classification,
        # while auto revision remains disabled because the tool cannot safely
        # decide whether the plan or an individual shot owns the defect.
        is_blocking = evidence_backed_production_blocker or (
            requested_blocking and severity in {"high", "critical"} and (
                known_objective or needs_classification
            )
        )
        downgrade_reason = "wardrobe_managed_by_character_identity" if wardrobe_only_binding else ""
        if wardrobe_only_binding:
            is_blocking = False
        if is_blocking and not (source_valid and storyboard_valid):
            is_blocking = False
            downgrade_reason = "evidence_invalid"
        provided_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item.get("issue_id", ""))).strip("_")[:80]
        issue_id = (
            provided_id if provided_id.startswith("model_") else "model_" + provided_id
        ) if provided_id else "model_" + content_fingerprint(
            category, scene_id, shot_ids, problem, normalized_source, normalized_storyboard,
            length=18,
        )
        normalized = {
            "issue_id": issue_id,
            "scene_id": scene_id,
            "shot_ids": shot_ids,
            "category": category,
            "original_category": original_category,
            "severity": severity,
            "blocking": is_blocking,
            "problem": problem,
            "instruction": instruction,
            "source_evidence": normalized_source,
            "storyboard_evidence": normalized_storyboard,
            "origin": "model",
        }
        canonical_prop_anchor = bool(referenced_prop_ids)
        if category == "asset_binding" and not canonical_prop_anchor:
            evidence_beat_ids = {
                str(evidence.get("beat_id", "")) for evidence in normalized_source
                if evidence.get("valid") and str(evidence.get("beat_id", ""))
            }
            evidence_quotes = {
                str(evidence.get("quote", "")) for evidence in normalized_source
                if evidence.get("valid") and str(evidence.get("quote", ""))
            }
            canonical_prop_anchor = any(
                evidence_beat_ids.intersection(str(value) for value in prop.get("beat_ids", []))
                or any(
                    anchor and any(anchor in quote or quote in anchor for quote in evidence_quotes)
                    for anchor in [
                        str(prop.get("name", "")), str(prop.get("source_quote", "")),
                        *(str(value) for value in prop.get("aliases", [])),
                    ]
                )
                for prop in source_props.values()
            )
        if evidence_backed_production_blocker:
            normalized["promotion_reason"] = "evidence_backed_production_metadata"
            normalized["auto_revisable"] = category != "asset_binding" or canonical_prop_anchor
            if category == "asset_binding" and not canonical_prop_anchor:
                normalized["registry_repair_required"] = True
        if needs_classification:
            normalized["needs_classification"] = True
            normalized["auto_revisable"] = False
        if downgrade_reason:
            normalized["downgrade_reason"] = downgrade_reason
        issues.append(normalized)
        (blocking if is_blocking else advisory).append(normalized)
    return {
        "decision": "revise" if blocking else "accept",
        "summary": str(result.get("summary", ""))[:1000] if isinstance(result, dict) else "",
        "issues": issues,
        "blocking_issues": blocking,
        "advisory_issues": advisory,
        "target_scene_ids": sorted({item["scene_id"] for item in blocking if item["scene_id"]}),
    }


def _dedupe_review_issues(items: list[dict]) -> list[dict]:
    """Collapse duplicate findings produced by overlapping audit layers."""
    result: list[dict] = []
    seen_ids: set[str] = set()
    seen_facts: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        issue_id = str(item.get("issue_id", "")).strip()
        fact = content_fingerprint(
            item.get("category"), item.get("scene_id"), item.get("shot_ids"),
            item.get("problem"), item.get("storyboard_evidence"), length=24,
        )
        if (issue_id and issue_id in seen_ids) or fact in seen_facts:
            continue
        if issue_id:
            seen_ids.add(issue_id)
        seen_facts.add(fact)
        result.append(item)
    return result


def _shot_json_path(storyboard: dict, scene_id: str, shot_id: str = "") -> str:
    for scene_index, scene in enumerate(storyboard.get("scenes", [])):
        if not isinstance(scene, dict) or str(scene.get("scene_id", "")) != str(scene_id):
            continue
        if not shot_id:
            return f"$.scenes[{scene_index}]"
        for shot_index, shot in enumerate(scene.get("shots", [])):
            if isinstance(shot, dict) and str(shot.get("shot_id", "")) == str(shot_id):
                return f"$.scenes[{scene_index}].shots[{shot_index}]"
    return "$.scenes"


def _canonical_code_issue(
    item: Any,
    *,
    category: str,
    storyboard: dict,
    source_model: dict,
) -> dict:
    raw = item if isinstance(item, dict) else {"problem": str(item)}
    scene_id = str(raw.get("scene_id", "")).strip()
    shot_id = str(raw.get("shot_id", "")).strip()
    problem = str(raw.get("problem") or raw.get("message") or raw.get("code") or item).strip()
    instruction = str(raw.get("instruction") or "Resolve the deterministic validation issue.").strip()
    path = _shot_json_path(storyboard, scene_id, shot_id)
    ending = source_model.get("ending_constraint", {}) if category == "source_fidelity" else {}
    code = str(raw.get("code") or category)
    return {
        "issue_id": "code_" + content_fingerprint(code, scene_id, shot_id, path, problem, length=18),
        "scene_id": scene_id,
        "shot_ids": [shot_id] if shot_id else [],
        "category": category,
        "severity": "high",
        "blocking": True,
        "problem": problem,
        "instruction": instruction,
        "source_evidence": ([{
            "beat_id": str(ending.get("beat_id", "")),
            "quote": str(ending.get("text", "")),
            "valid": True,
        }] if ending else []),
        "storyboard_evidence": [{"path": path, "valid": True}],
        "origin": "deterministic",
        "evidence_status": "code_owned",
    }


def _semantic_source_issues(state: SupervisorState, storyboard: dict) -> list[dict]:
    source_model = state.get("source_model", {})
    beats = [item for item in source_model.get("beats", []) if isinstance(item, dict)]
    beat_order = {str(item.get("beat_id")): index for index, item in enumerate(beats)}
    seen: list[tuple[str, str]] = []
    explicit_relations: list[tuple[dict, str]] = []
    shot_records: list[tuple[dict, str, str, list[str]]] = []
    for scene_index, scene in enumerate(storyboard.get("scenes", [])):
        if not isinstance(scene, dict):
            continue
        for shot_index, shot in enumerate(scene.get("shots", [])):
            if not isinstance(shot, dict):
                continue
            path = f"$.scenes[{scene_index}].shots[{shot_index}]"
            ids = shot.get("source_beat_ids", [])
            if isinstance(ids, list):
                seen.extend((str(beat_id), path + ".source_beat_ids") for beat_id in ids)
            shot_records.append((
                shot, path, str(scene.get("scene_id", "")),
                [str(beat_id) for beat_id in ids] if isinstance(ids, list) else [],
            ))
            relations = shot.get("temporal_relations", [])
            if isinstance(relations, list):
                explicit_relations.extend((item, path + ".temporal_relations") for item in relations if isinstance(item, dict))
    issues: list[dict] = []

    def add(code: str, problem: str, instruction: str, beat_id: str, path: str, scene_id: str = "") -> None:
        issues.append({
            "issue_id": "code_" + content_fingerprint(code, beat_id, path, length=18),
            "scene_id": scene_id,
            "shot_ids": [],
            "category": "source_fidelity",
            "severity": "high",
            "blocking": True,
            "problem": problem,
            "instruction": instruction,
            "source_evidence": [{"beat_id": beat_id, "valid": beat_id in beat_order}],
            "storyboard_evidence": [{"path": path, "valid": True}],
            "origin": "deterministic",
            "evidence_status": "code_owned",
        })

    unknown = [(beat_id, path) for beat_id, path in seen if beat_id not in beat_order]
    for beat_id, path in unknown:
        add("unknown_beat", f"Storyboard references unknown source beat {beat_id}.", "Use only Source Model beat IDs.", beat_id, path)
    positions: dict[str, int] = {}
    for position, (beat_id, _) in enumerate(seen):
        positions.setdefault(beat_id, position)
    for beat in beats:
        beat_id = str(beat.get("beat_id", ""))
        if beat.get("required") and beat_id not in positions:
            add("missing_beat", f"Required source beat {beat_id} is not covered.", "Add the missing fact to the appropriate target scene.", beat_id, "$.scenes")
        # Exact authored dialogue is strong, provider-independent evidence for
        # beat ownership. If it appears in a shot but that shot omits the beat
        # ID, downstream coverage and revision targeting become unreliable.
        dialogue_values = beat.get("dialogue", [])
        if isinstance(dialogue_values, str):
            dialogue_values = [dialogue_values]
        for dialogue in dialogue_values if isinstance(dialogue_values, list) else []:
            dialogue_text = (
                str(dialogue.get("line") or dialogue.get("text") or dialogue.get("dialogue") or "")
                if isinstance(dialogue, dict) else str(dialogue)
            )
            canonical_dialogue = _canonical_text(dialogue_text)
            if len(canonical_dialogue) < 3:
                continue
            for shot, path, scene_id, shot_beat_ids in shot_records:
                audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
                visual = shot.get("visual", {}) if isinstance(shot.get("visual"), dict) else {}
                prompt = shot.get("prompts", {}) if isinstance(shot.get("prompts"), dict) else {}
                delivery = _canonical_text(" ".join((
                    str(audio.get("dialogue", "")), str(audio.get("narration", "")),
                    str(visual.get("description", "")), str(prompt.get("image_cn", "")),
                    str(prompt.get("image_en", "")),
                )))
                if canonical_dialogue in delivery and beat_id not in shot_beat_ids:
                    shot_id = str(shot.get("shot_id", ""))
                    add(
                        "dialogue_beat_alignment",
                        f"Shot {shot_id or path} contains dialogue from {beat_id} but does not cite that beat.",
                        "Add the matching Source Model beat ID to this shot without removing other valid beat IDs.",
                        beat_id, path + ".source_beat_ids", scene_id,
                    )
                    issues[-1]["shot_ids"] = [shot_id] if shot_id else []
        # Source-owned sound cues provide another strong alignment signal for
        # silent beats such as bells, alarms, impacts, or off-screen events.
        sound_values = beat.get("sounds", [])
        if isinstance(sound_values, str):
            sound_values = [sound_values]
        for sound in sound_values if isinstance(sound_values, list) else []:
            canonical_sound = _canonical_text(str(sound))
            if len(canonical_sound) < 3:
                continue
            for shot, path, scene_id, shot_beat_ids in shot_records:
                audio = shot.get("audio", {}) if isinstance(shot.get("audio"), dict) else {}
                delivery = _canonical_text(" ".join((
                    str(audio.get("sound_music", "")),
                    str(audio.get("dialogue", "")),
                    str(audio.get("narration", "")),
                )))
                if canonical_sound in delivery and beat_id not in shot_beat_ids:
                    shot_id = str(shot.get("shot_id", ""))
                    add(
                        "sound_beat_alignment",
                        f"Shot {shot_id or path} contains source sound from {beat_id} but does not cite that beat.",
                        "Assign the matching Source Model beat to this shot and remove it from shots that do not contain the cited event.",
                        beat_id, path + ".source_beat_ids", scene_id,
                    )
                    issues[-1]["shot_ids"] = [shot_id] if shot_id else []
    for relation in source_model.get("relations", []):
        if not isinstance(relation, dict):
            continue
        left = str(relation.get("from_beat_id", ""))
        right = str(relation.get("to_beat_id", ""))
        relation_type = str(relation.get("type", "before"))
        if relation_type == "before" and left in positions and right in positions and positions[left] > positions[right]:
            add("beat_order", f"Source order {left} before {right} was reversed.", "Restore the declared Source Model order.", left, "$.scenes")
        if relation_type == "simultaneous":
            for actual, path in explicit_relations:
                pair = {str(actual.get("from_beat_id", "")), str(actual.get("to_beat_id", ""))}
                if pair == {left, right} and str(actual.get("type", "")) not in {"simultaneous", "parallel"}:
                    add("temporal_relation", f"Simultaneous beats {left} and {right} were marked sequential.", "Preserve the simultaneous relation.", left, path)
    return issues


def _tool_compare_source(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    storyboard = state.get("storyboard")
    if not isinstance(storyboard, dict) or not storyboard:
        return {}, {"ok": False, "error": "storyboard_missing"}
    issues = _semantic_source_issues(state, storyboard)
    issues.extend(
        _canonical_code_issue(
            item, category="source_fidelity", storyboard=storyboard,
            source_model=state.get("source_model", {}),
        )
        for item in _ending_fidelity_issues(storyboard, state["input_text"])
    )
    return {"source_issues": issues}, {
        "ok": not issues,
        "summary": "Source comparison passed." if not issues else "Source comparison found blockers.",
        "issues": issues,
        "advisory_issues": [],
    }


def _tool_inspect_shootability(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    storyboard = state.get("storyboard")
    if not isinstance(storyboard, dict) or not storyboard:
        return {}, {"ok": False, "error": "storyboard_missing"}
    issues = [
        _canonical_code_issue(
            issue, category="shootability", storyboard=storyboard,
            source_model=state.get("source_model", {}),
        )
        for issue in _deterministic_quality_issues(storyboard, state["input_text"])
        if issue.get("code") != "ending_fidelity"
    ]
    return {"shootability_issues": issues}, {
        "ok": not issues,
        "summary": "Deterministic shootability checks passed." if not issues else "Shootability blockers found.",
        "issues": issues,
    }


def _audit_projection_value(value: Any, *, string_limit: int = 12000) -> Any:
    """Redact audit data and label field excerpts instead of silently slicing."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in (
                "key", "token", "secret", "password", "auth", "reasoning",
                "thought", "chain_of_thought", "analysis", "explanation",
            )):
                continue
            clean[str(key)] = _audit_projection_value(item, string_limit=string_limit)
        return clean
    if isinstance(value, list):
        return [_audit_projection_value(item, string_limit=string_limit) for item in value]
    if isinstance(value, str):
        clean = _redact_configured_secret(value)
        if len(clean) > string_limit:
            return clean[:string_limit] + f"\n[FIELD_EXCERPT_CLIPPED original_chars={len(clean)}]"
        return clean
    if value is None or isinstance(value, (bool, int, float)):
        return value
    clean = str(value)
    if len(clean) > string_limit:
        return clean[:string_limit] + f"\n[FIELD_EXCERPT_CLIPPED original_chars={len(clean)}]"
    return clean


def _review_audit_payload(source_model: Any, storyboard: Any) -> dict:
    """Build a complete, valid JSON audit projection without byte slicing.

    The full storyboard repeats long prompt variants and production placeholders.
    Raw character slicing can terminate inside a JSON string and makes a reviewer
    falsely report a damaged artifact. This projection retains every scene, shot,
    source beat, executable binding and primary image prompt while omitting only
    declared duplicate/downstream fields.
    """
    source = source_model if isinstance(source_model, dict) else {}
    candidate = storyboard if isinstance(storyboard, dict) else {}
    projected_source = {
        key: _audit_projection_value(source.get(key))
        for key in (
            "version", "beats", "relations", "required_fact_ids", "required_facts",
            "ending_constraint", "entities", "props", "integrity_errors",
        )
        if key in source
    }
    projected_scenes: list[dict] = []
    for scene in candidate.get("scenes", []) if isinstance(candidate.get("scenes"), list) else []:
        if not isinstance(scene, dict):
            continue
        projected_scene = {
            key: _audit_projection_value(value)
            for key, value in scene.items()
            if key not in {"shots", "key_props"}
        }
        if isinstance(scene.get("key_props"), list):
            projected_scene["key_props"] = [
                {
                    key: _audit_projection_value(value)
                    for key, value in prop.items()
                    if key in {
                        "prop_id", "name", "aliases", "beat_ids", "asset_kind",
                        "continuity_required", "physical_spec", "lifecycle",
                    }
                }
                for prop in scene["key_props"] if isinstance(prop, dict)
            ]
        projected_shots: list[dict] = []
        for shot in scene.get("shots", []) if isinstance(scene.get("shots"), list) else []:
            if not isinstance(shot, dict):
                continue
            projected_shot = {
                key: _audit_projection_value(value)
                for key, value in shot.items()
                if key not in {"prompts", "assets", "status"}
            }
            prompts = shot.get("prompts")
            if isinstance(prompts, dict):
                projected_shot["prompts"] = {
                    key: _audit_projection_value(prompts.get(key))
                    for key in ("image_cn", "image_en") if key in prompts
                }
            projected_shots.append(projected_shot)
        projected_scene["shots"] = projected_shots
        projected_scenes.append(projected_scene)
    projected_storyboard = {
        key: _audit_projection_value(value)
        for key, value in candidate.items()
        if key not in {"scenes", "creative_bible"}
    }
    if isinstance(candidate.get("creative_bible"), dict):
        projected_storyboard["creative_bible"] = _audit_projection_value(candidate["creative_bible"])
    projected_storyboard["scenes"] = projected_scenes
    return {
        "payload_contract": {
            "kind": "valid_json_audit_projection",
            "complete_scene_and_shot_list": True,
            "omitted_storyboard_fields": [
                "shot.prompts.video", "shot.prompts.video_cn", "shot.prompts.video_en",
                "shot.assets", "shot.status",
            ],
            "schema_authority": "python_validate_storyboard",
            "long_string_policy": (
                "Fields longer than 12000 characters are valid JSON string excerpts ending in "
                "FIELD_EXCERPT_CLIPPED; this is not artifact truncation."
            ),
        },
        "source_model": projected_source,
        "storyboard": projected_storyboard,
    }


def _is_model_schema_claim(issue: Any) -> bool:
    """Keep full-candidate schema authority in deterministic validation."""
    if not isinstance(issue, dict):
        return False
    category = str(issue.get("original_category") or issue.get("category") or "")
    tokens = {
        token for token in category.strip().lower().replace("-", "_").replace(" ", "_").split("_")
        if token
    }
    return bool(
        tokens.intersection({"schema", "json", "serialization"})
        and tokens.intersection({"schema", "integrity", "validity", "serialization"})
    )


def _tool_review_storyboard(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    storyboard = state.get("storyboard")
    if not isinstance(storyboard, dict) or not storyboard:
        return {}, {"ok": False, "error": "storyboard_missing"}
    fingerprint = content_fingerprint(storyboard, length=24)
    validation_errors = validate_storyboard(storyboard)
    source_issues = _semantic_source_issues(state, storyboard)
    source_issues.extend(
        _canonical_code_issue(
            item, category="source_fidelity", storyboard=storyboard,
            source_model=state.get("source_model", {}),
        )
        for item in _ending_fidelity_issues(storyboard, state["input_text"])
    )
    shootability_issues = [
        _canonical_code_issue(
            item, category="shootability", storyboard=storyboard,
            source_model=state.get("source_model", {}),
        )
        for item in _deterministic_quality_issues(storyboard, state["input_text"])
        if item.get("code") != "ending_fidelity"
    ]
    schema_issues = [
        _canonical_code_issue(
            {"code": "schema", "problem": item}, category="schema",
            storyboard=storyboard, source_model=state.get("source_model", {}),
        )
        for item in validation_errors
    ]
    result: dict | None = None
    if not validation_errors:
        call_id = "review_storyboard_" + fingerprint[:16]
        audit_payload = _review_audit_payload(state.get("source_model", {}), storyboard)
        result = _call_json_cached(
            state,
            call_id,
            "Independently audit a storyboard. Return JSON only as {summary, issues:[{scene_id, "
            "shot_ids, category, severity, blocking, problem, instruction, source_evidence:[{beat_id "
            "or quote}], storyboard_evidence:[{path,value}]}]}. A blocking issue must be high/critical, "
            "objective, cite a real Source Model beat or exact quote, and cite a current storyboard "
            "JSON path with its exact value. The input is a valid JSON audit projection whose contract "
            "declares omitted duplicate/downstream fields. Python has already validated the complete "
            "candidate schema, so do not report JSON truncation, serialization, closure, or schema "
            "integrity issues. Framing, rhythm, camera, shot variety, and art taste are "
            "advisory. Executable production metadata is objective: an off-screen person included in "
            "visible_character_ids, or a visible/continuing prop omitted from visible_prop_ids/key_props, "
            "is an asset_binding or visible_entity_consistency issue and may be blocking at medium severity "
            "when both evidence sides are exact. Wardrobe assets are managed by character identity/outfit "
            "anchors and must not be reported as missing visible_prop_ids/key_props. Prefer canonical category "
            "source_fidelity for source/beat alignment. Do not expose chain-of-thought.",
            "Audit payload:\n" + json.dumps(audit_payload, ensure_ascii=False),
            max_tokens=3200,
            temperature=0.1,
        )
        contract_valid = isinstance(result, dict) and isinstance(result.get("issues"), list)
        if not contract_valid:
            result = _call_json_cached(
                state,
                call_id + "_contract",
                "Repair this review into JSON {summary:string, issues:array}. Normalize aliases: "
                "defect/description to problem, suggestion to instruction, shot_id to shot_ids. "
                "Do not add new issues or evidence.",
                json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=False),
                max_tokens=3200,
                temperature=0,
            )
    model_review = _normalize_model_issues(
        result, default_category="shootability",
        source_model=state.get("source_model", {}), storyboard=storyboard,
    )
    ignored_model_issues = [
        {**item, "ignored_reason": "deterministic_schema_authority"}
        for item in model_review["issues"] if _is_model_schema_claim(item)
    ]
    accepted_model_issues = [
        item for item in model_review["issues"] if not _is_model_schema_claim(item)
    ]
    all_issues = _dedupe_review_issues(
        [*schema_issues, *source_issues, *shootability_issues, *accepted_model_issues]
    )
    blocking = [item for item in all_issues if item.get("blocking")]
    advisory = [item for item in all_issues if not item.get("blocking")]
    review = {
        "decision": "revise" if blocking else "accept",
        "summary": (
            "Combined audit completed. Model schema claims were excluded because the full "
            "candidate passed deterministic validation."
            if ignored_model_issues else model_review["summary"] or "Combined audit completed."
        ),
        "issues": all_issues,
        "blocking_issues": blocking,
        "advisory_issues": advisory,
        "ignored_model_issues": ignored_model_issues,
        "target_scene_ids": sorted({
            str(item.get("scene_id")) for item in blocking if str(item.get("scene_id", "")).strip()
        }),
        "candidate_fingerprint": fingerprint,
    }
    pending = state.get("pending_revision", {}) if isinstance(state.get("pending_revision"), dict) else {}
    before_issues = pending.get("issues", {}) if isinstance(pending.get("issues"), dict) else {}
    current_by_id = {str(item.get("issue_id")): item for item in blocking}
    severity_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    improved_ids = [
        issue_id for issue_id, previous in before_issues.items()
        if issue_id not in current_by_id or severity_rank.get(str(current_by_id[issue_id].get("severity")), 0)
        < severity_rank.get(str(previous.get("severity")), 0)
    ]
    successful_scenes: list[str] = []
    revision_counts = dict(state.get("revision_counts", {}))
    if pending:
        for scene_id in pending.get("scene_ids", []):
            scene_issue_ids = pending.get("scene_issue_ids", {}).get(scene_id, [])
            if any(issue_id in improved_ids for issue_id in scene_issue_ids):
                successful_scenes.append(scene_id)
                revision_counts[scene_id] = revision_counts.get(scene_id, 0) + 1
    review["improved_issue_ids"] = improved_ids
    review["successful_revision_scene_ids"] = successful_scenes
    verified = not blocking
    history_entry = {
        "timestamp": _now(),
        "candidate_fingerprint": fingerprint,
        "decision": review["decision"],
        "blocking_issue_ids": [item["issue_id"] for item in blocking],
        "advisory_issue_ids": [item["issue_id"] for item in advisory],
        "pending_revision": _safe_value(pending),
        "improved_issue_ids": improved_ids,
    }
    updates: dict = {
        "validation_errors": validation_errors,
        "source_issues": source_issues,
        "shootability_issues": shootability_issues,
        "review": review,
        "review_history": [*state.get("review_history", []), history_entry],
        "candidate_fingerprint": fingerprint,
        "audited_fingerprint": fingerprint,
        "verification_state": "verified" if verified else "blocked",
        "pending_revision": {},
        "revision_counts": revision_counts,
    }
    if verified:
        updates["last_verified_storyboard"] = storyboard
        updates["last_verified_fingerprint"] = fingerprint
    return updates, {
        "ok": verified,
        "summary": review["summary"],
        "issues": blocking,
        "advisory_issues": advisory,
        "revision_effective": bool(improved_ids) if pending else None,
        "improved_issue_ids": improved_ids,
        "successful_revision_scene_ids": successful_scenes,
    }


def _all_blocking_issues(state: SupervisorState) -> list[dict]:
    items: list[dict] = []
    for key in ("source_issues", "shootability_issues"):
        value = state.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    review = _blocking_review_issues(state.get("review"))
    if review:
        items.extend(review)
    return _dedupe_review_issues(items)


def _tool_revise_scenes(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    if not isinstance(state.get("storyboard"), dict) or not state["storyboard"]:
        return {}, {"ok": False, "error": "storyboard_missing"}
    planned = _scene_ids_from_plan(state)
    candidate_fingerprint = content_fingerprint(state["storyboard"], length=24)
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    if review.get("candidate_fingerprint") != candidate_fingerprint:
        return {}, {"ok": False, "error": "stale_or_missing_audit"}
    blocking = {
        str(issue.get("issue_id")): issue
        for issue in review.get("blocking_issues", [])
        if isinstance(issue, dict) and issue.get("issue_id")
    }
    issue_ids = [str(item) for item in args.get("issue_ids", [])] if isinstance(args.get("issue_ids"), list) else []
    if not issue_ids:
        return {}, {"ok": False, "error": "blocking_issue_ids_required"}
    invalid_issue_ids = [issue_id for issue_id in issue_ids if issue_id not in blocking]
    if invalid_issue_ids:
        return {}, {
            "ok": False,
            "error": "invalid_revision_issue_ids",
            "issue_ids": invalid_issue_ids,
        }
    selected_issues = [blocking[issue_id] for issue_id in issue_ids]
    registry_required = [
        issue["issue_id"] for issue in selected_issues
        if issue.get("registry_repair_required")
    ]
    classification_required = [
        issue["issue_id"] for issue in selected_issues
        if issue.get("needs_classification") or (
            issue.get("auto_revisable") is False and not issue.get("registry_repair_required")
        )
    ]
    selected_issues = [
        issue for issue in selected_issues
        if issue["issue_id"] not in classification_required
        and issue["issue_id"] not in registry_required
    ]
    revisable_issue_ids = [str(issue["issue_id"]) for issue in selected_issues]
    if not selected_issues:
        return {}, {
            "ok": False,
            "error": "source_registry_repair_required" if registry_required else "human_issue_classification_required",
            "issue_ids": registry_required or classification_required,
        }
    requested = args.get("scene_ids") if isinstance(args.get("scene_ids"), list) else []
    issue_scene_ids = [str(issue.get("scene_id", "")) for issue in selected_issues]
    if requested:
        targets = list(dict.fromkeys(str(item) for item in requested if str(item) in planned))
        mismatched = [
            issue["issue_id"] for issue in selected_issues
            if str(issue.get("scene_id", "")) not in targets
        ]
        if mismatched:
            return {}, {"ok": False, "error": "issue_scene_mismatch", "issue_ids": mismatched}
    else:
        targets = list(dict.fromkeys(scene_id for scene_id in issue_scene_ids if scene_id in planned))
    if not targets:
        return {}, {"ok": False, "error": "no_valid_revision_targets"}
    scenes = {
        str(scene.get("scene_id")): dict(scene)
        for scene in state.get("completed_scenes", []) if isinstance(scene, dict)
    }
    attempt_counts = dict(state.get("revision_attempt_counts", {}))
    extension_counts = dict(state.get("revision_extension_counts", {}))
    revised: list[str] = []
    exhausted: list[str] = []
    convergence_extensions: list[str] = []
    failed_contracts = list(state.get("failed_revision_contracts", []))
    contract_failed_scene_ids: list[str] = []
    improving_scenes = {
        str(value) for value in review.get("successful_revision_scene_ids", []) if str(value)
    }

    def revision_limit(scene_id: str) -> int:
        extension_available = bool(
            state["max_revisions"] > 0
            and scene_id in improving_scenes
            and extension_counts.get(scene_id, 0) < 1
        )
        return state["max_revisions"] + (1 if extension_available else 0)

    eligible = [
        scene_id for scene_id in targets
        if attempt_counts.get(scene_id, 0) < revision_limit(scene_id)
    ]
    remaining_calls = state["max_calls"] - _count_model_calls(state)
    required_reserve = (2 * len(eligible)) + 4
    if eligible and remaining_calls < required_reserve:
        return {}, {
            "ok": False,
            "error": "insufficient_revision_reserve",
            "remaining_calls": remaining_calls,
            "required_calls": required_reserve,
        }
    for scene_id in targets:
        if attempt_counts.get(scene_id, 0) >= revision_limit(scene_id):
            exhausted.append(scene_id)
            continue
        scene = scenes.get(scene_id)
        if not scene:
            continue
        relevant = [issue for issue in selected_issues if str(issue.get("scene_id", "")) == scene_id]
        if not relevant:
            continue
        source = _scene_source(scene, state["chunks"])
        revision_number = attempt_counts.get(scene_id, 0) + 1
        attempt_counts[scene_id] = revision_number
        if revision_number > state["max_revisions"]:
            extension_counts[scene_id] = extension_counts.get(scene_id, 0) + 1
            convergence_extensions.append(scene_id)
        allowed_beat_ids = {
            str(beat.get("beat_id"))
            for beat in state.get("source_model", {}).get("beats", [])
            if isinstance(beat, dict) and str(beat.get("beat_id", ""))
        }
        required_beat_ids = {
            str(value) for value in scene.get("source_beat_ids", [])
            if str(value) in allowed_beat_ids
        } if isinstance(scene.get("source_beat_ids"), list) else set()
        if not required_beat_ids:
            required_beat_ids = {
                str(value)
                for shot in scene.get("shots", []) if isinstance(shot, dict)
                for value in shot.get("source_beat_ids", [])
                if str(value) in allowed_beat_ids
            }
        existing_shot_ids = {
            str(shot.get("shot_id", "")).strip()
            for shot in scene.get("shots", []) if isinstance(shot, dict)
            if str(shot.get("shot_id", "")).strip()
        }
        targeted_shot_ids: set[str] = set()
        for issue in relevant:
            shot_ids = issue.get("shot_ids")
            if isinstance(shot_ids, list):
                targeted_shot_ids.update(str(value) for value in shot_ids if str(value))
            shot_id = str(issue.get("shot_id", "")).strip()
            if shot_id:
                targeted_shot_ids.add(shot_id)
        protected_shot_ids = existing_shot_ids - targeted_shot_ids if targeted_shot_ids else set()
        result, contract_errors = _call_scene_shots_with_contract(
            state,
            f"revise_scene_{scene_id}_{revision_number}_"
            + content_fingerprint(scene, relevant, length=12),
            "Revise exactly one storyboard scene. Return the complete replacement shot list, never a partial "
            "patch. Fix the supplied objective blockers while preserving "
            "source facts, chronology, scene purpose, character anchors, and storyboard v2 shot shape. "
            "Return JSON only as {\"shots\":[...]}. Include image_cn/image_en and video/video_cn/video_en. "
            "Every shot must retain a non-empty source_beat_ids list. The complete result must cover every "
            "required source beat and retain every untargeted shot ID. Also retain visual.visible_character_ids for actually framed "
            "characters, matching visible_prop_ids plus visual.key_props for visible assets, and temporal_relations. "
            "Use only canonical prop IDs supplied in the payload; never invent an ID. Wardrobe stays in the "
            "character outfit anchor and is not a visible_prop_ids/key_props binding. "
            "An off-screen character must not remain in visible_character_ids or image identity anchors. "
            "Do not make unrelated scene or art-direction changes.",
            json.dumps({
                "source": source,
                "creative_bible": state["plan"].get("creative_bible", {}),
                "scene": scene,
                "canonical_props": state.get("source_model", {}).get("props", []),
                "blocking_issues": relevant,
                "revision_contract": {
                    "required_source_beat_ids": sorted(required_beat_ids),
                    "targeted_shot_ids": sorted(targeted_shot_ids),
                    "untargeted_shot_ids_that_must_remain": sorted(protected_shot_ids),
                },
            }, ensure_ascii=False),
            scene_id=scene_id,
            allowed_beat_ids=allowed_beat_ids,
            require_source_beat_ids=True,
            required_beat_ids=required_beat_ids,
            required_shot_ids=protected_shot_ids,
            max_tokens=7000,
            temperature=0.2,
        )
        if contract_errors or not isinstance(result, dict):
            failed_contracts.append({
                "scene_id": scene_id,
                "revision_number": revision_number,
                "issue_ids": [str(issue.get("issue_id")) for issue in relevant],
                "contract_errors": contract_errors,
            })
            contract_failed_scene_ids.append(scene_id)
            continue
        shots = result["shots"]
        scenes[scene_id] = _prepare_scene_semantics(
            state,
            _prepare_generated_scene(
                {**scene, "shots": shots}, state["plan"].get("creative_bible", {}),
            ),
        )
        revised.append(scene_id)
    if contract_failed_scene_ids and not revised:
        return {
            "revision_attempt_counts": attempt_counts,
            "revision_extension_counts": extension_counts,
            "failed_revision_contracts": failed_contracts,
        }, {
            "ok": False,
            "error": "revision_contract_failed",
            "summary": "The provider returned an unusable scene revision after a contract retry.",
            "contract_failed_scene_ids": contract_failed_scene_ids,
            "revision_limit_scene_ids": exhausted,
            "registry_issue_ids": registry_required,
            "convergence_extension_scene_ids": convergence_extensions,
        }
    ordered = [scenes[item] for item in planned if item in scenes]
    candidate = _storyboard_candidate(state, ordered)
    atomic_write_json(os.path.join(state["run_dir"], "storyboard.json"), candidate)
    next_fingerprint = content_fingerprint(candidate, length=24)
    pending = {
        "before_fingerprint": candidate_fingerprint,
        "after_fingerprint": next_fingerprint,
        "scene_ids": revised,
        "issue_ids": revisable_issue_ids,
        "issues": {issue["issue_id"]: issue for issue in selected_issues},
        "scene_issue_ids": {
            scene_id: [
                issue["issue_id"] for issue in selected_issues
                if str(issue.get("scene_id", "")) == scene_id
            ] for scene_id in revised
        },
    } if revised else {}
    return {
        "completed_scenes": ordered,
        "revision_attempt_counts": attempt_counts,
        "revision_extension_counts": extension_counts,
        "failed_revision_contracts": failed_contracts,
        "storyboard": candidate,
        "validation_errors": None,
        "source_issues": None,
        "shootability_issues": None,
        "candidate_fingerprint": next_fingerprint,
        "audited_fingerprint": "",
        "verification_state": "unverified",
        "pending_revision": pending,
    }, {
        "ok": bool(revised) and not exhausted and not contract_failed_scene_ids,
        "error": "revision_contract_failed" if contract_failed_scene_ids else None,
        "summary": f"Revised scenes: {', '.join(revised) or 'none'}.",
        "revised_scene_ids": revised,
        "revision_limit_scene_ids": exhausted,
        "pending_issue_ids": revisable_issue_ids if revised else [],
        "deferred_issue_ids": classification_required,
        "registry_issue_ids": registry_required,
        "contract_failed_scene_ids": contract_failed_scene_ids,
        "convergence_extension_scene_ids": convergence_extensions,
    }


def _tool_finalize(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    blockers = _completion_blockers(state)
    if blockers:
        return {}, {
            "ok": False,
            "error": "completion_gates_failed",
            "issues": blockers,
        }
    return {"status": "completed", "stop_reason": "completed"}, {
        "ok": True, "summary": "All code-owned completion gates passed."
    }


def _tool_stop(state: SupervisorState, args: dict) -> tuple[dict, dict]:
    reason = str(args.get("reason", "supervisor_requested_review"))[:300]
    return {"status": "needs_review", "stop_reason": reason}, {
        "ok": True, "summary": "Supervisor stopped for human review."
    }


TOOL_REGISTRY = {
    "analyze_source": _tool_analyze_source,
    "create_plan": _tool_create_plan,
    "revise_plan": _tool_revise_plan,
    "generate_scenes": _tool_generate_scenes,
    "assemble_storyboard": _tool_assemble_storyboard,
    "validate_schema": _tool_validate_schema,
    "compare_source": _tool_compare_source,
    "inspect_shootability": _tool_inspect_shootability,
    "review_storyboard": _tool_review_storyboard,
    "revise_scenes": _tool_revise_scenes,
    "finalize": _tool_finalize,
    "stop_needs_review": _tool_stop,
}


def _progress_fingerprint(state: dict) -> str:
    return content_fingerprint(
        state.get("chunks"), state.get("summaries"), state.get("plan"),
        state.get("plan_errors"), state.get("completed_scenes"), state.get("storyboard"),
        state.get("validation_errors"), state.get("source_issues"),
        state.get("shootability_issues"), state.get("review"),
        state.get("revision_counts"), state.get("revision_attempt_counts"),
        state.get("revision_extension_counts"),
        state.get("failed_revision_contracts"),
        state.get("pending_revision"), state.get("candidate_fingerprint"),
        state.get("audited_fingerprint"), state.get("verification_state"),
        state.get("status"),
        length=24,
    )


def _write_trace(path: str, history: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        for event in history:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def _execute_tool_node(state: SupervisorState) -> dict:
    pending = state.get("pending_action", {})
    action = str(pending.get("action", ""))
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    protocol_error = pending.get("protocol_error") if isinstance(pending.get("protocol_error"), dict) else None
    summary = str(pending.get("decision_summary", ""))[:500]
    before = _progress_fingerprint(state)
    steps = state.get("tool_steps", 0) + 1
    invalid = action not in TOOL_REGISTRY or protocol_error is not None
    updates: dict = {}
    if action not in TOOL_REGISTRY:
        result = {"ok": False, "error": "invalid_action", "allowed_actions": list(ALLOWED_ACTIONS)}
    elif protocol_error is not None:
        result = {"ok": False, **protocol_error}
    else:
        try:
            updates, result = TOOL_REGISTRY[action](state, args)
        except ModelBudgetExhausted:
            updates = {"status": "needs_review", "stop_reason": "budget_exhausted"}
            result = {"ok": False, "error": "budget_exhausted"}
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            result = {
                "ok": False,
                "error": "tool_error",
                "error_type": type(exc).__name__,
                "message": _redact_configured_secret(str(exc))[:500],
            }
    candidate = {**state, **updates}
    after = _progress_fingerprint(candidate)
    progressed = before != after
    if action == "review_storyboard" and result.get("revision_effective") is False:
        progressed = False
    invalid_count = state.get("invalid_action_count", 0) + 1 if invalid else 0
    signature = content_fingerprint(action, result.get("error"), result.get("issues"), length=16)
    if progressed:
        no_progress = 0
        last_signature = ""
    elif signature == state.get("last_no_progress_signature"):
        no_progress = state.get("no_progress_count", 0) + 1
        last_signature = signature
    else:
        no_progress = 1
        last_signature = signature
    stop_updates: dict = {}
    if invalid_count >= 3:
        stop_updates = {"status": "needs_review", "stop_reason": "invalid_action_limit"}
    elif no_progress >= 3:
        stop_updates = {"status": "needs_review", "stop_reason": "no_progress_limit"}
    elif steps >= state["max_steps"] and updates.get("status") not in {"completed", "needs_review"}:
        stop_updates = {"status": "needs_review", "stop_reason": "budget_exhausted"}
    tool_counts = dict(state.get("tool_counts", {}))
    tool_counts[action] = tool_counts.get(action, 0) + 1
    event = {
        "sequence": len(state.get("action_history", [])) + 1,
        "timestamp": _now(),
        "action": action,
        "args": args,
        "decision_summary": summary,
        "result": _safe_value(result),
        "counters": {
            "tool_steps": steps,
            "model_calls": _count_model_calls(state),
            "invalid_actions": invalid_count,
            "no_progress": no_progress,
        },
        "state_fingerprint": after,
    }
    history = [*state.get("action_history", []), event]
    _write_trace(state["trace_path"], history)
    return {
        **updates,
        **stop_updates,
        "pending_action": {},
        "last_result": _safe_value(result),
        "action_history": history,
        "tool_counts": tool_counts,
        "tool_steps": steps,
        "model_calls": _count_model_calls(state),
        "invalid_action_count": invalid_count,
        "no_progress_count": no_progress,
        "last_no_progress_signature": last_signature,
    }


def _route_after_supervisor(state: SupervisorState) -> Literal["tool", "end"]:
    return "end" if state.get("status") in {"completed", "needs_review"} else "tool"


def _route_after_tool(state: SupervisorState) -> Literal["supervisor", "end"]:
    return "end" if state.get("status") in {"completed", "needs_review"} else "supervisor"


def build_supervisor_graph(checkpointer: SqliteSaver):
    graph = StateGraph(SupervisorState)
    graph.add_node("initialize", _initialize_node)
    graph.add_node("supervisor", _supervisor_node)
    graph.add_node("execute_tool", _execute_tool_node)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "supervisor")
    graph.add_conditional_edges("supervisor", _route_after_supervisor, {"tool": "execute_tool", "end": END})
    graph.add_conditional_edges("execute_tool", _route_after_tool, {"supervisor": "supervisor", "end": END})
    return graph.compile(checkpointer=checkpointer)


def _review_artifact(state: SupervisorState) -> dict:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    return {
        "decision": "accept" if state.get("status") == "completed" else "needs_review",
        "completion_blockers": _completion_blockers(state),
        "schema_issues": state.get("validation_errors"),
        "source_issues": state.get("source_issues"),
        "shootability_issues": state.get("shootability_issues"),
        "model_review": review,
        "review_history": state.get("review_history", []),
        "revision_counts": state.get("revision_counts", {}),
        "revision_attempt_counts": state.get("revision_attempt_counts", {}),
        "revision_extension_counts": state.get("revision_extension_counts", {}),
        "failed_revision_contracts": state.get("failed_revision_contracts", []),
        "candidate_fingerprint": state.get("candidate_fingerprint", ""),
        "last_verified_fingerprint": state.get("last_verified_fingerprint", ""),
        "verification_state": state.get("verification_state", "not_audited"),
        "source_model_integrity_errors": (
            state.get("source_model", {}).get("integrity_errors", [])
            if isinstance(state.get("source_model"), dict) else []
        ),
    }


def _manifest(state: SupervisorState, checkpoint_path: str) -> dict:
    return {
        "engine": "agent",
        "langgraph_engine": True,
        "supervisor_agent_version": SUPERVISOR_AGENT_VERSION,
        "toolset_version": SUPERVISOR_TOOLSET_VERSION,
        "run_id": state["run_id"],
        "title": state["title"],
        "scene_count": state["scene_count"],
        "model": state.get("model_name", ""),
        "status": state.get("status", "unknown"),
        "stop_reason": state.get("stop_reason", ""),
        "tool_steps": state.get("tool_steps", 0),
        "model_calls": _count_model_calls(state),
        "tool_counts": state.get("tool_counts", {}),
        "revision_counts": state.get("revision_counts", {}),
        "revision_attempt_counts": state.get("revision_attempt_counts", {}),
        "revision_extension_counts": state.get("revision_extension_counts", {}),
        "failed_revision_contracts": state.get("failed_revision_contracts", []),
        "failed_revision_contract_count": len(state.get("failed_revision_contracts", [])),
        "no_progress_reason": (
            str(state.get("last_result", {}).get("error", ""))
            if state.get("stop_reason") == "no_progress_limit" and isinstance(state.get("last_result"), dict)
            else ""
        ),
        "source_model_integrity_errors": (
            state.get("source_model", {}).get("integrity_errors", [])
            if isinstance(state.get("source_model"), dict) else []
        ),
        "revision_count": sum(state.get("revision_counts", {}).values()),
        "budgets": {
            "max_steps": state["max_steps"],
            "requested_max_calls": (
                state.get("requested_max_calls")
                if state.get("requested_max_calls") is not None else "auto"
            ),
            "effective_max_calls": state["max_calls"],
            "max_calls": state["max_calls"],
            "remaining_calls": max(0, state["max_calls"] - _count_model_calls(state)),
            "max_revisions_per_scene": state["max_revisions"],
            "calculation": state.get("budget_factors", {}),
        },
        "candidate_fingerprint": state.get("candidate_fingerprint", ""),
        "last_verified_fingerprint": state.get("last_verified_fingerprint", ""),
        "audited_fingerprint": state.get("audited_fingerprint", ""),
        "verification_state": state.get("verification_state", "not_audited"),
        "checkpoint": os.path.relpath(checkpoint_path, os.path.dirname(state["stage_dir"])),
        "trace": os.path.basename(state["trace_path"]),
        "updated_at": _now(),
    }


def generate_storyboard_agent(
    text: str,
    title: str,
    word_count: int,
    scene_count: int,
    stage_dir: str,
    output_dir: str,
    *,
    resume: bool = True,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_calls: int | None = DEFAULT_MAX_CALLS,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
) -> dict | None:
    """Run or resume the single-supervisor storyboard Agent."""
    max_steps = max(1, int(max_steps))
    max_revisions = max(0, int(max_revisions))
    requested_max_calls = None if max_calls is None else max(1, int(max_calls))
    chunk_count = max(1, len(_chunk_text(text)))
    effective_max_calls, budget_factors = calculate_agent_call_budget(
        scene_count, chunk_count, max_revisions, requested_max_calls,
    )
    os.makedirs(stage_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    _, configured_model, _ = get_ai_config()
    model_name = configured_model or "unconfigured"
    fingerprint = content_fingerprint(
        text, title, scene_count, STAGE_VERSION, SUPERVISOR_AGENT_VERSION,
        SUPERVISOR_TOOLSET_VERSION, model_name, max_steps,
        requested_max_calls if requested_max_calls is not None else "auto",
        effective_max_calls, chunk_count, max_revisions,
    )
    stable_run_id = f"storyboard-supervisor-{fingerprint}"
    suffix = "" if resume else "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    run_id = stable_run_id + suffix
    run_dir = os.path.join(stage_dir, f"run_{fingerprint}" + suffix)
    os.makedirs(run_dir, exist_ok=True)
    checkpoint_path = os.path.join(stage_dir, "checkpoints.sqlite")
    trace_path = os.path.join(output_dir, "agent_trace.jsonl")
    manifest_path = os.path.join(output_dir, "agent_run.json")
    review_path = os.path.join(output_dir, "review.json")
    storyboard_path = os.path.join(run_dir, "storyboard.json")

    existing = read_json(manifest_path) if resume else None
    if (
        isinstance(existing, dict)
        and existing.get("run_id") == run_id
        and existing.get("status") in {"completed", "needs_review"}
        and os.path.isfile(storyboard_path)
    ):
        return read_json(storyboard_path)

    initial: SupervisorState = {
        "input_text": text,
        "title": title,
        "word_count": word_count,
        "scene_count": scene_count,
        "stage_dir": stage_dir,
        "run_dir": run_dir,
        "run_id": run_id,
        "trace_path": trace_path,
        "model_name": model_name,
        "chunk_count": chunk_count,
        "max_steps": max_steps,
        "max_calls": effective_max_calls,
        "requested_max_calls": requested_max_calls,
        "budget_factors": budget_factors,
        "max_revisions": max_revisions,
        "status": "running",
    }
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    graph = build_supervisor_graph(saver)
    config = {"configurable": {"thread_id": run_id}}
    final_state: SupervisorState = initial
    try:
        prior = saver.get(config) if resume else None
        result = graph.invoke(
            None if prior else initial,
            config={**config, "recursion_limit": max(100, max_steps * 3 + 10)},
        )
        if not isinstance(result, dict):
            raise RuntimeError("LangGraph supervisor returned no state")
        final_state = result
        storyboard = final_state.get("storyboard")
        if isinstance(storyboard, dict) and storyboard:
            atomic_write_json(storyboard_path, storyboard)
        atomic_write_json(review_path, _review_artifact(final_state))
        atomic_write_json(manifest_path, _manifest(final_state, checkpoint_path))
        atomic_write_json(os.path.join(stage_dir, "latest.json"), {
            "run_dir": os.path.relpath(run_dir, stage_dir),
            "run_id": run_id,
            "status": final_state.get("status", "unknown"),
        })
        return storyboard if isinstance(storyboard, dict) and storyboard else None
    except Exception as exc:
        try:
            snapshot = graph.get_state(config)
            if isinstance(snapshot.values, dict) and snapshot.values:
                final_state = {**initial, **snapshot.values}
        except Exception:
            pass
        final_state = {**final_state, "status": "failed", "stop_reason": "node_failure"}
        manifest = _manifest(final_state, checkpoint_path)
        manifest["error_type"] = type(exc).__name__
        manifest["error"] = _redact_configured_secret(str(exc))[:500]
        atomic_write_json(manifest_path, manifest)
        print(f"Supervisor Agent failed: {exc}", file=sys.stderr)
        return None
    finally:
        connection.close()
