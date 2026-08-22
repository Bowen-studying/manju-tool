from __future__ import annotations

import json
import os
import wave

import pytest

from manju.production.adapters.voice_tts import (
    MAX_VOICE_TTS_CUE_DURATION_MS,
    DeterministicVoiceTTSModel,
    VoiceTTSStageAdapter,
    _direction_entries,
    _idempotency_key,
)
from manju.production.models import M4_2_DAG_VERSION, ProductionError, stages_for_dag
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json

from tests.test_production_m4_0_voice_script import FixtureStoryboardAdapter


KEY = b"m4-2-voice-tts-test-key"


def _service(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("你好", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=True, voice_director_enabled=True, voice_tts_enabled=True,
        voice_tts_model_profile="counting-fake-tts",
        hmac_key_id="test-key",
    )
    model = DeterministicVoiceTTSModel(model_profile="counting-fake-tts")
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        voice_tts_adapter=VoiceTTSStageAdapter(model),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, model


def test_m42_dag_is_strict_and_m41_sequence_remains_frozen():
    assert stages_for_dag(M4_2_DAG_VERSION, ["storyboard", "voice_script", "voice_director", "voice_tts"]) == (
        "storyboard", "voice_script", "voice_director", "voice_tts"
    )
    with pytest.raises(ProductionError):
        stages_for_dag(M4_2_DAG_VERSION, ["storyboard", "voice_script", "voice_tts"])


def test_m42_offline_fake_tts_publishes_manifest_and_wav(tmp_path):
    service, model = _service(tmp_path)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert completed.progress_completed == completed.progress_total == 4
    assert model.calls == 1
    events = [event for event in service.store.events.read() if event.get("run_id") == completed.run_id]
    assert [
        (event.get("payload") or {}).get("stage")
        for event in events if event["event_type"] == "stage_completed"
    ] == ["storyboard", "voice_script", "voice_director", "voice_tts"]
    assert not any(event["event_type"] in {"approval_requested", "grant_issued", "call_reserved", "call_submitted"} for event in events)
    graph = service.get_artifact_graph()["graph"]
    audio = next(item for item in graph["artifacts"] if item["logical_id"] == "voice_audio.main" and item["state"] == "current")
    assert [item["logical_id"] for item in audio["depends_on"]] == ["voice_direction.main"]
    dto = service.get_voice_tts_status()
    assert dto["status"] == "completed" and dto["artifact"]["audio"]["media_type"] == "audio/wav"
    assert "path" not in json.dumps(dto, ensure_ascii=False)


def test_m42_reuses_audio_without_second_fake_tts_call(tmp_path):
    service, model = _service(tmp_path)
    assert service.run_until_blocked().status == "completed"
    assert model.calls == 1
    service.advance()
    assert model.calls == 1


def test_m42_recovery_reuses_durable_receipt_after_publish_crash(tmp_path):
    service, model = _service(tmp_path)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_tts"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    output_dir = os.path.dirname(authority_path)
    os.remove(os.path.join(output_dir, authority["artifact"]["path"]))
    os.remove(authority_path)
    direction_ref = authority["inputs"]["voice_direction"]
    direction_path = os.path.join(
        output_dir, ".runtime_inputs", f"voice_direction.main-{direction_ref['version_id'][7:]}.bin"
    )
    recovered = service.voice_tts_adapter.execute(
        stage_run_id=terminal["payload"]["stage_run_id"], output_dir=output_dir,
        voice_direction_path=direction_path, voice_direction_ref=direction_ref,
    )
    assert recovered.status == "completed"
    assert model.calls == 1


def test_m42_recovery_republishes_authority_after_manifest_publish_crash(tmp_path):
    service, model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_tts"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    output_dir = os.path.dirname(authority_path)
    os.remove(authority_path)
    direction_ref = authority["inputs"]["voice_direction"]
    direction_path = os.path.join(
        output_dir, ".runtime_inputs", f"voice_direction.main-{direction_ref['version_id'][7:]}.bin"
    )
    recovered = service.voice_tts_adapter.execute(
        stage_run_id=terminal["payload"]["stage_run_id"], output_dir=output_dir,
        voice_direction_path=direction_path, voice_direction_ref=direction_ref,
    )
    assert recovered.status == "completed"
    assert model.calls == 1
    assert os.path.isfile(authority_path)


def test_m42_recovery_retries_reserved_receipt_with_same_idempotency_key(tmp_path):
    service, model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_tts"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    output_dir = os.path.dirname(authority_path)
    direction_ref = authority["inputs"]["voice_direction"]
    direction_path = os.path.join(
        output_dir, ".runtime_inputs", f"voice_direction.main-{direction_ref['version_id'][7:]}.bin"
    )
    receipt_path = os.path.join(output_dir, authority["receipt"]["path"])
    receipt = json.loads(open(receipt_path, encoding="utf-8").read())
    receipt["status"] = "reserved"
    receipt["output"] = {"path": "voice_audio.wav", "sha256": "", "size": 0}
    atomic_write_json(receipt_path, receipt)
    for name in (authority["artifact"]["path"], authority["audio"]["path"]):
        os.remove(os.path.join(output_dir, name))
    os.remove(authority_path)
    recovered = service.voice_tts_adapter.execute(
        stage_run_id=terminal["payload"]["stage_run_id"], output_dir=output_dir,
        voice_direction_path=direction_path, voice_direction_ref=direction_ref,
    )
    assert recovered.status == "completed"
    assert model.calls == 1
    assert receipt["idempotency_key"] == _idempotency_key(terminal["payload"]["stage_run_id"], direction_ref)


def test_m42_rejects_model_profile_drift_and_unbounded_cues(tmp_path):
    service, _model = _service(tmp_path)
    project = service.store.load_project()
    project["production"]["voice_tts"]["model_profile"] = "unexpected-model"
    atomic_write_json(service.paths.project_file, project)
    with pytest.raises(ProductionError) as error:
        service.run_until_blocked()
    assert error.value.code == "PROJECT_CONTRACT_CHANGED"
    ref = {"logical_id": "voice_direction.main", "version_id": "sha256:" + "0" * 64}
    script_ref = {"logical_id": "voice_script.main", "version_id": "sha256:" + "1" * 64}
    value = {
        "schema_version": "voice-direction-v1", "voice_script": script_ref,
        "entry_count": 1, "entries": [{
            "sequence": 1, "text": "x", "direction": {"sequence": 1},
            "shot_duration_seconds": MAX_VOICE_TTS_CUE_DURATION_MS / 1000 + 1,
        }],
    }
    with pytest.raises(ValueError):
        _direction_entries(value, ref)


def test_m42_audio_and_manifest_tamper_fail_closed(tmp_path):
    service, _model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_tts"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    output_dir = os.path.dirname(authority_path)
    audio_path = os.path.join(output_dir, authority["audio"]["path"])
    with open(audio_path, "ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ProductionError) as error:
        service.get_status()
    assert error.value.code == "STAGE_INTEGRITY_FAILED"


def test_m42_manifest_binds_direction_input(tmp_path):
    service, _model = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read()
        if event.get("run_id") == completed.run_id and event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "voice_tts"
    )
    authority_path = service.store.artifact_path(terminal["payload"]["authority_path"])
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    manifest_path = os.path.join(os.path.dirname(authority_path), authority["artifact"]["path"])
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    manifest["entries"][0]["text_sha256"] = "0" * 64
    atomic_write_json(manifest_path, manifest)
    authority["artifact"]["sha256"] = sha256_file(manifest_path)
    atomic_write_json(authority_path, authority)
    with pytest.raises(ProductionError) as error:
        service.get_status()
    assert error.value.code == "STAGE_INTEGRITY_FAILED"
