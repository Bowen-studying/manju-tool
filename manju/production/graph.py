"""The deterministic M1 production DAG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DagNode:
    node_id: str
    stage: str
    dependencies: tuple[str, ...] = ()


M1_NODES = (
    DagNode(node_id="storyboard", stage="storyboard", dependencies=("source",)),
)
M2_NODES = (
    DagNode(node_id="storyboard", stage="storyboard", dependencies=("source",)),
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)
M4_NODES = (
    DagNode(node_id="storyboard", stage="storyboard", dependencies=("source",)),
    DagNode(node_id="voice_script", stage="voice_script", dependencies=("storyboard",)),
)
M4_VISUAL_NODES = M4_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)
M4_1_NODES = M4_NODES + (
    DagNode(node_id="voice_director", stage="voice_director", dependencies=("voice_script",)),
)
M4_1_VISUAL_NODES = M4_1_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)
M4_2_NODES = M4_1_NODES + (
    DagNode(node_id="voice_tts", stage="voice_tts", dependencies=("voice_director",)),
)
M4_2_VISUAL_NODES = M4_2_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)

# M5.0 keeps video_prompt as a sibling of the voice chain.  The linear
# scheduler still visits it after enabled voice stages, while the artifact
# graph intentionally binds it only to storyboard.
M5_NODES = M1_NODES + (
    DagNode(node_id="video_prompt", stage="video_prompt", dependencies=("storyboard",)),
)
M5_VISUAL_NODES = M5_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)
M5_VOICE_SCRIPT_NODES = M1_NODES + (
    DagNode(node_id="voice_script", stage="voice_script", dependencies=("storyboard",)),
    DagNode(node_id="video_prompt", stage="video_prompt", dependencies=("storyboard",)),
)
M5_VOICE_SCRIPT_VISUAL_NODES = M5_VOICE_SCRIPT_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)
M5_VOICE_DIRECTOR_NODES = M1_NODES + (
    DagNode(node_id="voice_script", stage="voice_script", dependencies=("storyboard",)),
    DagNode(node_id="voice_director", stage="voice_director", dependencies=("voice_script",)),
    DagNode(node_id="video_prompt", stage="video_prompt", dependencies=("storyboard",)),
)
M5_VOICE_DIRECTOR_VISUAL_NODES = M5_VOICE_DIRECTOR_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)
M5_VOICE_TTS_NODES = M1_NODES + (
    DagNode(node_id="voice_script", stage="voice_script", dependencies=("storyboard",)),
    DagNode(node_id="voice_director", stage="voice_director", dependencies=("voice_script",)),
    DagNode(node_id="voice_tts", stage="voice_tts", dependencies=("voice_director",)),
    DagNode(node_id="video_prompt", stage="video_prompt", dependencies=("storyboard",)),
)
M5_VOICE_TTS_VISUAL_NODES = M5_VOICE_TTS_NODES + (
    DagNode(node_id="visual", stage="visual", dependencies=("storyboard",)),
)


def stage_event_state(events: list[dict[str, Any]], run_id: str, stage: str) -> str:
    state = "pending"
    for event in events:
        if event.get("run_id") != run_id:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("stage") != stage:
            continue
        event_type = event.get("event_type")
        if event_type in {"stage_scheduled", "stage_run_attached"}:
            state = "running"
        elif event_type == "stage_completed":
            state = "completed"
        elif event_type == "stage_needs_review":
            state = "needs_review"
        elif event_type == "stage_failed":
            state = "failed"
    return state


def has_event(events: list[dict[str, Any]], run_id: str, event_type: str, stage: str = "") -> bool:
    for event in events:
        if event.get("run_id") != run_id or event.get("event_type") != event_type:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not stage or payload.get("stage") == stage:
            return True
    return False
