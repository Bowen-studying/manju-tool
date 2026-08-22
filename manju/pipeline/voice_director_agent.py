"""Bounded LangGraph voice-direction agent.

The agent is deliberately provider-neutral.  M4.1 accepts an injected local
model port only; the production adapter never imports the generic LLM client.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from manju.utils.runtime import atomic_write_json, read_json


VOICE_DIRECTOR_AGENT_VERSION = "voice-director-agent-m4.1-v1"
VOICE_DIRECTOR_POLICY_VERSION = "voice-director-policy-v1"


class VoiceDirectorModelPort(Protocol):
    def direct(self, *, entries: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
        """Return one structured direction for every input cue."""


@dataclass
class DeterministicVoiceDirectorModel:
    """Offline reference model used by M4.1 and its acceptance tests."""

    calls: int = 0

    def direct(self, *, entries: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls += 1
        allowed = policy.get("allowed_emotions") or ["neutral"]
        emotion = str(allowed[0])
        return [
            {
                "sequence": item["sequence"],
                "emotion": emotion,
                "rate": 1.0,
                "pitch": 0,
                "volume": 1.0,
                "pause_before_ms": 0,
                "pause_after_ms": 120 if item.get("kind") == "dialogue" else 180,
                "voice_requirements": {"speaker": item.get("speaker", "unknown")},
            }
            for item in entries
        ]


class VoiceDirectorState(TypedDict, total=False):
    storyboard_ref: dict[str, str]
    voice_script_ref: dict[str, str]
    policy_ref: dict[str, str]
    entries: list[dict[str, Any]]
    policy: dict[str, Any]
    directives: list[dict[str, Any]]
    model_calls: int
    steps: int
    max_model_calls: int
    max_steps: int
    status: str
    stop_reason: str
    call_receipts: list[dict[str, str]]
    recovery_fingerprint: str
    agent_version: str
    model_profile: str


def voice_director_state_fingerprint(state: dict[str, Any]) -> str:
    """Fingerprint the state that must be preserved across checkpoint recovery."""
    value = {
        key: state.get(key)
        for key in (
            "storyboard_ref", "voice_script_ref", "policy_ref", "entries", "policy",
            "directives", "model_calls", "steps", "max_model_calls", "max_steps",
            "status", "stop_reason", "call_receipts", "recovery_fingerprint",
            "agent_version", "model_profile",
        )
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_voice_director_checkpoint(*, checkpoint_path: str, thread_id: str) -> dict[str, Any] | None:
    """Read the latest stage-private checkpoint state for semantic sealing."""
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    try:
        prior = saver.get({"configurable": {"thread_id": thread_id}})
        if not prior:
            return None
        channels = prior.get("channel_values", {}) if isinstance(prior, dict) else {}
        state = channels.get("__root__", channels)
        return dict(state) if isinstance(state, dict) else None
    finally:
        connection.close()


def _step(state: VoiceDirectorState) -> VoiceDirectorState:
    steps = int(state.get("steps", 0)) + 1
    if steps > int(state.get("max_steps", 1)):
        raise RuntimeError("voice-director step budget exhausted")
    return {"steps": steps}


def _prepare_node(state: VoiceDirectorState) -> VoiceDirectorState:
    result = _step(state)
    entries = state.get("entries")
    policy = state.get("policy")
    if not isinstance(entries, list) or not isinstance(policy, dict):
        raise ValueError("voice-director inputs are invalid")
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("sequence"), int) or not isinstance(item.get("text"), str):
            raise ValueError("voice-script cue is invalid")
    return {**result, "status": "prepared"}


def _input_context(state: VoiceDirectorState) -> dict[str, Any]:
    """Return the complete immutable context bound to a model call."""
    return {
        "agent_version": state.get("agent_version", VOICE_DIRECTOR_AGENT_VERSION),
        "model_profile": state.get("model_profile", str((state.get("policy") or {}).get("model_profile", ""))),
        "storyboard_ref": state.get("storyboard_ref"),
        "voice_script_ref": state.get("voice_script_ref"),
        "policy_ref": state.get("policy_ref"),
        "entries": state.get("entries"),
        "policy": state.get("policy"),
        "max_model_calls": int(state.get("max_model_calls", 1)),
        "max_steps": int(state.get("max_steps", 1)),
    }


def _input_fingerprint(state: VoiceDirectorState) -> str:
    """Bind a reusable call receipt to every immutable agent input."""
    return hashlib.sha256(
        json.dumps(_input_context(state), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _receipt(state: VoiceDirectorState, *, call_index: int, input_fingerprint: str,
             output_fingerprint: str) -> dict[str, Any]:
    return {
        "call_index": str(call_index),
        "input_fingerprint": input_fingerprint,
        "output_fingerprint": output_fingerprint,
        "agent_version": str(state.get("agent_version", VOICE_DIRECTOR_AGENT_VERSION)),
        "model_profile": str(state.get("model_profile", str((state.get("policy") or {}).get("model_profile", "")))),
        "max_model_calls": str(int(state.get("max_model_calls", 1))),
        "max_steps": str(int(state.get("max_steps", 1))),
        "input_context": _input_context(state),
    }


def _direct_node_with_receipt(
    state: VoiceDirectorState, model: VoiceDirectorModelPort, reservation_path: str,
) -> VoiceDirectorState:
    result = _step(state)
    calls = int(state.get("model_calls", 0))
    if calls >= int(state.get("max_model_calls", 1)):
        raise RuntimeError("voice-director model-call budget exhausted")
    entries = list(state["entries"])
    policy = dict(state["policy"])
    input_fingerprint = _input_fingerprint(state)
    existing = read_json(reservation_path)
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or existing.get("schema_version") != "voice-director-call-v1"
            or existing.get("status") != "completed"
            or existing.get("input_fingerprint") != input_fingerprint
            or existing.get("agent_version") != state.get("agent_version", VOICE_DIRECTOR_AGENT_VERSION)
            or existing.get("model_profile") != state.get("model_profile", str(policy.get("model_profile", "")))
            or existing.get("max_model_calls") != int(state.get("max_model_calls", 1))
            or existing.get("max_steps") != int(state.get("max_steps", 1))
            or existing.get("input_context") != _input_context(state)
        ):
            raise RuntimeError("unresolved or invalid voice-director call reservation")
        directives = existing.get("output")
        if not isinstance(directives, list):
            raise RuntimeError("voice-director call receipt output is invalid")
        output_fingerprint = str(existing.get("output_fingerprint", ""))
        raw = json.dumps(directives, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != output_fingerprint:
            raise RuntimeError("voice-director call receipt was tampered")
        receipt = _receipt(
            state, call_index=int(existing.get("call_index", calls + 1)),
            input_fingerprint=input_fingerprint, output_fingerprint=output_fingerprint,
        )
        return {**result, "directives": directives, "model_calls": calls + 1,
                "call_receipts": [*state.get("call_receipts", []), receipt], "status": "directed"}
    atomic_write_json(reservation_path, {
        "schema_version": "voice-director-call-v1", "status": "reserved",
        "call_index": calls + 1, "input_fingerprint": input_fingerprint,
        "agent_version": state.get("agent_version", VOICE_DIRECTOR_AGENT_VERSION),
        "model_profile": state.get("model_profile", str(policy.get("model_profile", ""))),
        "max_model_calls": int(state.get("max_model_calls", 1)),
        "max_steps": int(state.get("max_steps", 1)),
        "input_context": _input_context(state),
    })
    directives = model.direct(entries=entries, policy=policy)
    raw = json.dumps(directives, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    output_fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    atomic_write_json(reservation_path, {
        "schema_version": "voice-director-call-v1", "status": "completed",
        "call_index": calls + 1, "input_fingerprint": input_fingerprint,
        "agent_version": state.get("agent_version", VOICE_DIRECTOR_AGENT_VERSION),
        "model_profile": state.get("model_profile", str(policy.get("model_profile", ""))),
        "max_model_calls": int(state.get("max_model_calls", 1)),
        "max_steps": int(state.get("max_steps", 1)),
        "input_context": _input_context(state),
        "output": directives, "output_fingerprint": output_fingerprint,
    })
    receipt = _receipt(
        state, call_index=calls + 1, input_fingerprint=input_fingerprint,
        output_fingerprint=output_fingerprint,
    )
    return {**result, "directives": directives, "model_calls": calls + 1,
            "call_receipts": [*state.get("call_receipts", []), receipt], "status": "directed"}


def _validate_node(state: VoiceDirectorState) -> VoiceDirectorState:
    result = _step(state)
    entries = list(state.get("entries", []))
    directives = list(state.get("directives", []))
    if len(entries) != len(directives):
        raise ValueError("voice-director must return one direction per cue")
    sequences = [item.get("sequence") for item in entries]
    output_sequences = [item.get("sequence") for item in directives]
    if output_sequences != sequences:
        raise ValueError("voice-director cue sequence changed")
    if int(state.get("model_calls", 0)) != len(state.get("call_receipts", [])):
        raise ValueError("voice-director model-call ledger is inconsistent")
    allowed = {"sequence", "emotion", "rate", "pitch", "volume", "pause_before_ms", "pause_after_ms", "voice_requirements"}
    for item in directives:
        if set(item) != allowed or not isinstance(item.get("emotion"), str) or not isinstance(item.get("voice_requirements"), dict):
            raise ValueError("voice-director direction schema is invalid")
        for name in ("rate", "pitch", "volume", "pause_before_ms", "pause_after_ms"):
            if not isinstance(item.get(name), (int, float)) or isinstance(item.get(name), bool):
                raise ValueError("voice-director numeric field is invalid")
        allowed_emotions = state.get("policy", {}).get("allowed_emotions")
        if (
            not isinstance(allowed_emotions, list)
            or not allowed_emotions
            or not all(isinstance(value, str) for value in allowed_emotions)
            or item.get("emotion") not in allowed_emotions
        ):
            raise ValueError("voice-director policy emotion constraint failed")
    raw = json.dumps(directives, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if state.get("call_receipts") and state["call_receipts"][-1].get("output_fingerprint") != hashlib.sha256(raw.encode("utf-8")).hexdigest():
        raise ValueError("voice-director output receipt does not match directions")
    return {**result, "status": "validated"}


def _finalize_node(state: VoiceDirectorState) -> VoiceDirectorState:
    result = _step(state)
    return {**result, "status": "completed", "stop_reason": "completed"}


def build_voice_director_graph(checkpointer: SqliteSaver, model: VoiceDirectorModelPort, reservation_path: str = ""):
    graph = StateGraph(VoiceDirectorState)
    graph.add_node("prepare", _prepare_node)
    graph.add_node("direct", lambda state: _direct_node_with_receipt(state, model, reservation_path))
    graph.add_node("validate", _validate_node)
    graph.add_node("finalize", _finalize_node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "direct")
    graph.add_edge("direct", "validate")
    graph.add_edge("validate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def run_voice_director(
    *,
    checkpoint_path: str,
    thread_id: str,
    initial: VoiceDirectorState,
    model: VoiceDirectorModelPort, reservation_path: str,
) -> VoiceDirectorState:
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    graph = build_voice_director_graph(saver, model, reservation_path)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        prior = saver.get(config)
        if prior:
            channels = prior.get("channel_values", {}) if isinstance(prior, dict) else {}
            prior_state = channels.get("__root__", channels)
            if not isinstance(prior_state, dict):
                raise RuntimeError("voice-director checkpoint state is invalid")
            immutable = ("storyboard_ref", "voice_script_ref", "policy_ref", "entries", "policy", "max_model_calls", "max_steps", "recovery_fingerprint", "agent_version", "model_profile")
            for key in immutable:
                if prior_state.get(key) != initial.get(key):
                    raise RuntimeError("voice-director checkpoint binding changed")
        result = graph.invoke(None if prior else initial, config=config)
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise RuntimeError("voice-director graph did not complete")
        return result
    finally:
        connection.commit()
        connection.close()
