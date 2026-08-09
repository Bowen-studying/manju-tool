"""Code-owned visual workflow routing."""

from __future__ import annotations


STAGE_COMMANDS = {
    "new": "inspect_storyboard",
    "inspected": "build_visual_bible",
    "planned": "request_foundation_approval",
    "foundation_retry_approval": "request_foundation_approval",
    "foundation_generate": "generate_foundation_candidates",
    "foundation_rank": "rank_foundation_candidates",
    "foundation_lock": "request_foundation_lock",
    "foundation_complete": "build_scene_groups",
    "group_approval": "request_scene_group_approval",
    "group_generate": "generate_scene_group",
    "group_review": "inspect_scene_group",
    "group_retry": "revise_scene_group",
    "group_finalize": "finalize_scene_group",
    "manual_review": "request_manual_review",
    "vision_recheck_finalize": "finalize_vision_recheck",
    "ready_finalize": "finalize",
}

TERMINAL_STATUSES = frozenset({"completed", "failed", "needs_review", "awaiting_approval"})


def next_visual_command(state: dict) -> str:
    """Return the deterministic command for a runnable workflow state."""
    if str(state.get("status", "")) != "running":
        return ""
    return STAGE_COMMANDS.get(str(state.get("stage", "")), "")


def recommended_visual_command(state: dict) -> str:
    return next_visual_command({**state, "status": "running"}) or "stop_needs_review"


def assert_command_allowed(state: dict, command: str) -> None:
    expected = next_visual_command(state)
    if not expected:
        raise ValueError(
            f"workflow state {state.get('status')!r}/{state.get('stage')!r} has no runnable command"
        )
    if command != expected:
        raise ValueError(
            f"command {command!r} is invalid for stage {state.get('stage')!r}; expected {expected!r}"
        )
