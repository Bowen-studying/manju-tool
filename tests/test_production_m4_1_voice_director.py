from __future__ import annotations

import json
import os

import pytest

from manju.production.adapters.voice_director import VoiceDirectorStageAdapter
from manju.production.models import M4_1_DAG_VERSION, ProductionError, stages_for_dag
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json

from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter, _available


KEY = b"m4-1-voice-director-test-key"


class CountingModel:
    def __init__(self):
        self.calls = 0

    def direct(self, *, entries, policy):
        self.calls += 1
        return [
            {
                "sequence": cue["sequence"], "emotion": "neutral", "rate": 1.0,
                "pitch": 0, "volume": 1.0, "pause_before_ms": 0,
                "pause_after_ms": 100, "voice_requirements": {"speaker": cue["speaker"]},
            }
            for cue in entries
        ]


class ForgedEmotionModel:
    def direct(self, *, entries, policy):
        return [
            {
                "sequence": cue["sequence"], "emotion": "forged", "rate": 1.0,
                "pitch": 0, "volume": 1.0, "pause_before_ms": 0,
                "pause_after_ms": 100, "voice_requirements": {"speaker": cue["speaker"]},
            }
            for cue in entries
        ]


def _service(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("你好", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=True, voice_director_enabled=True,
        hmac_key_id="test-key",
    )
    model = CountingModel()
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_director_adapter=VoiceDirectorStageAdapter(model),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, model


def test_m41_stage_contract_is_explicit_and_legacy_sequences_remain_frozen():
    assert stages_for_dag(M4_1_DAG_VERSION, ["storyboard", "voice_script", "voice_director"]) == (
        "storyboard", "voice_script", "voice_director"
    )
    with pytest.raises(ProductionError):
        stages_for_dag(M4_1_DAG_VERSION, ["storyboard", "voice_director"])


def test_m41_runs_offline_with_one_model_call_and_three_graph_dependencies(tmp_path):
    service, model = _service(tmp_path)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert completed.progress_completed == completed.progress_total == 3
    assert model.calls == 1
    events = [event for event in service.store.events.read() if event.get("run_id") == completed.run_id]
    assert [
        (event.get("payload") or {}).get("stage")
        for event in events if event["event_type"] == "stage_completed"
    ] == ["storyboard", "voice_script", "voice_director"]
    assert not any(event["event_type"] in {"approval_requested", "grant_issued", "call_reserved", "call_submitted"} for event in events)
    graph = service.get_artifact_graph()["graph"]
    direction = next(item for item in graph["artifacts"] if item["logical_id"] == "voice_direction.main" and item["state"] == "current")
    assert {item["logical_id"] for item in direction["depends_on"]} == {
        "storyboard.output", "voice_script.main", "voice_director.policy",
    }
    dto = service.get_voice_director_status()
    assert dto["status"] == "completed" and dto["artifact"]["entry_count"] == 3
    assert "path" not in json.dumps(dto, ensure_ascii=False)


def test_m41_reuses_frozen_child_output_without_second_model_call(tmp_path):
    service, model = _service(tmp_path)
    assert service.run_until_blocked().status == "completed"
    assert model.calls == 1
    service.advance()
    assert model.calls == 1


def test_m41_checkpoint_and_output_tamper_fail_closed(tmp_path):
    service, _model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_director"
    )
    authority = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority_value = json.loads(open(authority, encoding="utf-8").read())
    checkpoint = os.path.join(os.path.dirname(authority), authority_value["checkpoint"]["path"])
    with open(checkpoint, "ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ProductionError) as error:
        service.get_status()
    assert error.value.code == "STAGE_INTEGRITY_FAILED"


def test_m41_policy_is_content_bound(tmp_path):
    service, _model = _service(tmp_path)
    policy = service.paths.voice_director_policy_path
    value = json.loads(open(policy, encoding="utf-8").read())
    value["allowed_emotions"] = ["angry"]
    atomic_write_json(policy, value)
    with pytest.raises(ProductionError) as error:
        service.advance()
    assert error.value.code in {"PROJECT_CONTRACT_CHANGED", "STAGE_INTEGRITY_FAILED"}


def test_m41_fast_path_rechecks_frozen_contract_before_child_execution(tmp_path):
    service, model = _service(tmp_path)
    while True:
        snapshot = service.advance()
        events = service.store.events.read()
        if any(
            event.get("run_id") == snapshot.run_id
            and event["event_type"] == "stage_completed"
            and (event.get("payload") or {}).get("stage") == "voice_script"
            for event in events
        ):
            break
    service.voice_director_adapter.contract_version = "voice-director-adapter-tampered"
    with pytest.raises(ProductionError) as error:
        service.advance()
    assert error.value.code == "PROJECT_CONTRACT_CHANGED"
    assert model.calls == 0


def test_m41_completed_receipt_binds_artifact_directions(tmp_path):
    service, _model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_director"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    output_dir = os.path.dirname(authority_path)
    artifact_path = os.path.join(output_dir, authority["artifact"]["path"])
    artifact = json.loads(open(artifact_path, encoding="utf-8").read())
    artifact["entries"][0]["direction"]["emotion"] = "forged"
    atomic_write_json(artifact_path, artifact)
    authority["artifact"]["sha256"] = sha256_file(artifact_path)
    atomic_write_json(authority_path, authority)
    result = service.voice_director_adapter.inspect(
        stage_run_id=terminal["payload"]["stage_run_id"], output_dir=output_dir,
        storyboard_ref=authority["inputs"]["storyboard"],
        voice_script_ref=authority["inputs"]["voice_script"],
        policy_ref=authority["inputs"]["policy"],
    )
    assert result is not None and result.status == "failed"
    assert result.reason_code == "STAGE_INTEGRITY_FAILED"


def test_m41_checkpoint_replacement_fails_even_if_authority_hash_is_rewritten(tmp_path):
    service, _model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_director"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    checkpoint_path = os.path.join(os.path.dirname(authority_path), authority["checkpoint"]["path"])
    with open(checkpoint_path, "wb") as handle:
        handle.write(b"forged-valid-path")
    authority["checkpoint"]["sha256"] = sha256_file(checkpoint_path)
    atomic_write_json(authority_path, authority)
    with pytest.raises(ProductionError) as error:
        service.get_status()
    assert error.value.code == "STAGE_INTEGRITY_FAILED"


def test_m41_policy_emotion_constraint_blocks_noncompliant_model(tmp_path):
    service, _model = _service(tmp_path)
    service.voice_director_adapter.model_port = ForgedEmotionModel()
    result = service.run_until_blocked()
    assert result.status == "failed"
    assert not any(
        event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_director"
        for event in service.store.events.read()
    )
