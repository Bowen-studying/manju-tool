"""Metadata common to every compatibility projection."""

from __future__ import annotations

from .ownership import ownership_manifest


def projection_metadata(state: dict, source_sequence: int = 0) -> dict:
    return {
        "projection_schema_version": 1,
        "authoritative_source": "visual_event_store",
        "source_run_id": str(state.get("run_id", "")),
        "source_event_sequence": int(source_sequence or state.get("event_sequence", 0) or 0),
        "read_for_recovery": False,
    }


def architecture_manifest() -> dict:
    return {
        "visual_core": "4.0.0-rc2",
        "state_mutation_model": "event_reducer_with_legacy_snapshot_bridge",
        "workflow_routing": "deterministic_code_owned",
        "fact_ownership": ownership_manifest(),
    }
