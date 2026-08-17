from __future__ import annotations

import json
import os
import builtins

import pytest

from manju.production.adapters.base import StageResult
from manju.production.adapters.voice_script import VoiceScriptStageAdapter, build_voice_script
from manju.production.audit import export_audit_snapshot, verify_audit_snapshot
from manju.production.models import M4_DAG_VERSION, ProductionError, stages_for_dag
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json


KEY = b"m4-voice-script-test-key"


def _storyboard(spoken: str = "你好") -> dict:
    return {
        "schema_version": "2.0",
        "title": "fixture",
        "creative_bible": {"style_anchor": "clean ink", "characters": []},
        "scenes": [{
            "scene_id": "1",
            "heading": "INT. ROOM - DAY",
            "shots": [
                {
                    "shot_id": "1.1", "duration_seconds": 2.5,
                    "visual": {"description": "A speaks"},
                    "audio": {"speaker": "A", "dialogue": spoken, "narration": "旁白"},
                    "prompts": {"image_cn": "画面", "image_en": "frame"},
                },
                {
                    "shot_id": "1.2", "duration_seconds": 3,
                    "visual_description": "legacy shot", "dialogue_narration": "旧格式台词",
                    "image_prompt_cn": "画面", "image_prompt_en": "frame",
                },
                {
                    "shot_id": "1.3", "duration_seconds": 1,
                    "visual": {"description": "silent"}, "audio": {},
                    "prompts": {"image_cn": "画面", "image_en": "frame"},
                },
            ],
        }],
    }


class FixtureStoryboardAdapter:
    contract_version = "fixture-storyboard-m4-v1"

    @staticmethod
    def _result(stage_run_id: str, output_dir: str) -> StageResult | None:
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        if not os.path.isfile(artifact) or not os.path.isfile(authority):
            return None
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"path": artifact, "version_id": "sha256:" + sha256_file(artifact)},),
            authority_path=authority, authority_hash=sha256_file(authority),
            authority_files=({"path": authority, "sha256": sha256_file(authority)},),
        )

    def execute(self, *, stage_run_id, source_path, output_dir, **_kwargs):
        os.makedirs(output_dir, exist_ok=True)
        spoken = open(source_path, encoding="utf-8").read().strip()
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        atomic_write_json(artifact, _storyboard(spoken))
        atomic_write_json(authority, {"schema_version": "1", "artifact_sha256": sha256_file(artifact)})
        return self._result(stage_run_id, output_dir)

    def inspect(self, *, stage_run_id, output_dir, **_kwargs):
        return self._result(stage_run_id, output_dir)


def _service(tmp_path, *, visual=False):
    source = tmp_path / "source.txt"
    source.write_text("你好", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=True, visual_enabled=visual,
        visual_provider_profile="mock", visual_operation_kind="mock_image",
        hmac_key_id="test-key",
    )
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service


def _available(graph, logical_id):
    return next(item for item in graph["artifacts"] if item["logical_id"] == logical_id and item["state"] == "available")


def test_voice_script_builder_is_literal_ordered_and_deterministic():
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + "a" * 64}
    first = build_voice_script(_storyboard(), ref)
    second = build_voice_script(_storyboard(), ref)
    assert first == second
    assert [(item["kind"], item["speaker"], item["text"]) for item in first["entries"]] == [
        ("dialogue", "A", "你好"),
        ("narration", "narrator", "旁白"),
        ("legacy_spoken", "unknown", "旧格式台词"),
    ]
    assert [item["sequence"] for item in first["entries"]] == [1, 2, 3]
    assert [item["shot_start_seconds"] for item in first["entries"]] == [0.0, 0.0, 2.5]

    empty = build_voice_script({"schema_version": "2.0", "title": "empty", "scenes": []}, ref)
    assert empty["entry_count"] == 0 and empty["entries"] == []


def test_voice_script_adapter_reuses_bytes_and_fails_closed_on_tamper(tmp_path):
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), _storyboard())
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(storyboard_path)}
    adapter = VoiceScriptStageAdapter()
    first = adapter.execute(
        stage_run_id="voice-1", storyboard_path=str(storyboard_path), storyboard_ref=ref,
        output_dir=str(tmp_path / "voice"),
    )
    second = adapter.execute(
        stage_run_id="voice-1", storyboard_path=str(storyboard_path), storyboard_ref=ref,
        output_dir=str(tmp_path / "voice"),
    )
    assert first.status == second.status == "completed"
    assert first.artifacts[0]["version_id"] == second.artifacts[0]["version_id"]
    with open(first.artifacts[0]["path"], "wb") as handle:
        handle.write(b"tampered")
    assert adapter.inspect(stage_run_id="voice-1", output_dir=str(tmp_path / "voice"), storyboard_ref=ref).status == "failed"


def test_voice_script_recovery_rejects_self_consistent_forged_derivation(tmp_path):
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), _storyboard())
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(storyboard_path)}
    output_dir = tmp_path / "voice"
    output_dir.mkdir()
    artifact_path = output_dir / "voice_script.json"
    authority_path = output_dir / "voice_script_run.json"
    forged = build_voice_script(_storyboard(), ref)
    forged["title"] = "FORGED"
    forged["entries"][0]["text"] = "INJECTED"
    atomic_write_json(str(artifact_path), forged)
    atomic_write_json(str(authority_path), {
        "schema_version": "voice-script-run-v1",
        "stage_run_id": "voice-1",
        "status": "completed",
        "adapter_contract_version": VoiceScriptStageAdapter.contract_version,
        "storyboard": ref,
        "artifact": {
            "path": "voice_script.json",
            "sha256": sha256_file(artifact_path),
            "schema_version": "voice-script-v1",
        },
    })

    result = VoiceScriptStageAdapter().execute(
        stage_run_id="voice-1", storyboard_path=str(storyboard_path), storyboard_ref=ref,
        output_dir=str(output_dir),
    )
    assert result.status == "failed"
    assert result.reason_code == "STAGE_INTEGRITY_FAILED"
    assert json.loads(artifact_path.read_text(encoding="utf-8"))["title"] == "FORGED"


def test_voice_script_hashes_and_parses_one_storyboard_byte_snapshot(tmp_path, monkeypatch):
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), _storyboard("ORIGINAL"))
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(storyboard_path)}
    real_open = builtins.open
    storyboard_opens = 0

    def tracked_open(path, *args, **kwargs):
        nonlocal storyboard_opens
        if os.path.realpath(path) == os.path.realpath(storyboard_path):
            storyboard_opens += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("manju.production.adapters.voice_script.open", tracked_open, raising=False)
    result = VoiceScriptStageAdapter().execute(
        stage_run_id="voice-1", storyboard_path=str(storyboard_path), storyboard_ref=ref,
        output_dir=str(tmp_path / "voice"),
    )
    assert result.status == "completed"
    assert storyboard_opens == 1
    artifact = json.loads(open(result.artifacts[0]["path"], encoding="utf-8").read())
    assert artifact["entries"][0]["text"] == "ORIGINAL"


@pytest.mark.parametrize("duration", [-1, 0, True, float("nan"), float("inf"), float("-inf"), 10**1000])
def test_voice_script_rejects_invalid_duration(duration):
    storyboard = _storyboard()
    storyboard["scenes"][0]["shots"][0]["duration_seconds"] = duration
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + "a" * 64}
    with pytest.raises(ValueError, match="finite positive"):
        build_voice_script(storyboard, ref)


@pytest.mark.parametrize("schema_version", [None, "1.0", "999"])
def test_voice_script_adapter_rejects_unsupported_storyboard_schema(tmp_path, schema_version):
    storyboard = _storyboard()
    if schema_version is None:
        storyboard.pop("schema_version")
    else:
        storyboard["schema_version"] = schema_version
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), storyboard)
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(storyboard_path)}
    result = VoiceScriptStageAdapter().execute(
        stage_run_id="voice-1", storyboard_path=str(storyboard_path), storyboard_ref=ref,
        output_dir=str(tmp_path / "voice"),
    )
    assert result.status == "failed"
    assert result.reason_code == "VOICE_SCRIPT_FAILED"


@pytest.mark.parametrize("declared", [[['storyboard']], [{"stage": "storyboard"}], [1], "storyboard"])
def test_m4_stage_sequence_rejects_non_string_lists_without_type_error(declared):
    with pytest.raises(ProductionError) as error:
        stages_for_dag(M4_DAG_VERSION, declared)
    assert error.value.code == "UNSUPPORTED_SCHEMA_VERSION"


def test_m4_voice_only_run_commits_graph_and_frontend_safe_dto(tmp_path):
    service = _service(tmp_path)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert completed.progress_completed == completed.progress_total == 2
    events = [event for event in service.store.events.read() if event.get("run_id") == completed.run_id]
    assert [
        (event.get("payload") or {}).get("stage") for event in events if event["event_type"] == "stage_completed"
    ] == ["storyboard", "voice_script"]
    assert not any(event["event_type"] in {
        "approval_requested", "grant_issued", "call_reserved", "call_submitted", "call_settled",
    } for event in events)
    graph = service.get_artifact_graph()["graph"]
    voice = next(item for item in graph["artifacts"] if item["logical_id"] == "voice_script.main" and item["state"] == "current")
    assert len(voice["depends_on"]) == 1 and voice["depends_on"][0]["logical_id"] == "storyboard.output"
    dto = service.get_voice_script_status()
    assert dto["status"] == "completed" and dto["artifact"]["entry_count"] == 3
    serialized = json.dumps(dto, ensure_ascii=False)
    assert not any(token in serialized for token in ("path", "authority", ".runtime_inputs", "contract", "provider"))


def test_m4_runs_free_voice_before_visual_approval(tmp_path):
    service = _service(tmp_path, visual=True)
    blocked = service.run_until_blocked()
    assert blocked.status == "awaiting_approval" and blocked.progress_completed == 2
    events = [event for event in service.store.events.read() if event.get("run_id") == blocked.run_id]
    voice_sequence = next(event["sequence"] for event in events if event["event_type"] == "stage_completed" and (event["payload"] or {}).get("stage") == "voice_script")
    approval_sequence = next(event["sequence"] for event in events if event["event_type"] == "approval_requested")
    assert voice_sequence < approval_sequence
    assert not any(event["event_type"] in {"call_reserved", "call_submitted", "call_settled"} for event in events)


def test_source_revision_regenerates_voice_and_unrelated_revision_reuses_it(tmp_path):
    service = _service(tmp_path)
    metadata_path = os.path.join(service.paths.outputs_dir, "metadata.txt")
    with open(metadata_path, "w", encoding="utf-8") as handle:
        handle.write("unrelated")
    registered = service.register_artifact(
        logical_id="metadata.note", path=os.path.relpath(metadata_path, service.paths.root), producer_stage="metadata",
        expected_last_event_hash=service.get_status().last_event_hash,
    )
    metadata_version = _available(registered["graph"], "metadata.note")["version_id"]
    service.select_artifact_version(
        logical_id="metadata.note", version_id=metadata_version, expected_last_event_hash=registered["last_event_hash"],
    )
    predecessor = service.run_until_blocked()
    source_path = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(source_path, "w", encoding="utf-8") as handle:
        handle.write("再见")
    candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(source_path, service.paths.root),
        producer_stage="revision_candidate", expected_last_event_hash=predecessor.last_event_hash,
    )
    source_v2 = _available(candidate["graph"], "source.script")["version_id"]
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    assert [item["action"] for item in preview["execution_plan"]] == ["regenerate", "regenerate"]
    successor = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},), requested_by="tester",
        reason="source changed", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m4-source-v2",
    )["run_id"]
    assert service.run_until_blocked().run_id == successor
    dto = service.get_voice_script_status()
    assert dto["status"] == "completed"
    voice_data = next(
        (event["payload"]["artifacts"][0] for event in service.store.events.read()
         if event.get("run_id") == successor and event["event_type"] == "stage_completed"
         and (event.get("payload") or {}).get("stage") == "voice_script"),
    )
    assert "再见" in open(service.store.artifact_path(voice_data["path"]), encoding="utf-8").read()

    replacement_path = os.path.join(service.paths.outputs_dir, "metadata-v2.txt")
    with open(replacement_path, "w", encoding="utf-8") as handle:
        handle.write("unrelated-v2")
    replacement = service.register_revision_candidate(
        logical_id="metadata.note", path=os.path.relpath(replacement_path, service.paths.root),
        producer_stage="revision_candidate", expected_last_event_hash=service.get_status().last_event_hash,
    )
    metadata_v2 = _available(replacement["graph"], "metadata.note")["version_id"]
    preview = service.preview_revision(changed=({"logical_id": "metadata.note", "version_id": metadata_v2},))
    assert [item["action"] for item in preview["execution_plan"]] == ["reuse", "reuse"]
    reused_run = service.create_revision(
        changed=({"logical_id": "metadata.note", "version_id": metadata_v2},), requested_by="tester",
        reason="metadata changed", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m4-metadata-v2",
    )["run_id"]
    assert service.run_until_blocked().run_id == reused_run
    assert service.get_voice_script_status()["status"] == "reused"


def test_completed_voice_artifact_tamper_blocks_status_restart_and_advance(tmp_path):
    service = _service(tmp_path)
    completed = service.run_until_blocked()
    terminal = next(
        event for event in service.store.events.read() if event.get("run_id") == completed.run_id
        and event["event_type"] == "stage_completed" and (event["payload"] or {}).get("stage") == "voice_script"
    )
    artifact_path = service.store.artifact_path(terminal["payload"]["artifacts"][0]["path"])
    with open(artifact_path, "wb") as handle:
        handle.write(b"tampered")
    restarted = ProductionService(
        service.paths.project_file, storyboard_adapter=FixtureStoryboardAdapter(),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    restarted._configured_model = lambda: "fixture"
    with pytest.raises(ProductionError) as status_error:
        restarted.get_status()
    assert status_error.value.code == "STAGE_INTEGRITY_FAILED"
    with pytest.raises(ProductionError):
        restarted.advance()


def test_completed_m4_audit_snapshot_verifies_manifest_and_hmac(tmp_path):
    service = _service(tmp_path)
    assert service.run_until_blocked().status == "completed"
    provider = MappingHmacKeyProvider({"test-key": KEY})
    exported = export_audit_snapshot(
        project_json=service.paths.project_file, destination=str(tmp_path / "audit"), key_provider=provider,
    )
    assert exported["bundle_type"] == "evidence_snapshot"
    verified = verify_audit_snapshot(
        destination=str(tmp_path / "audit"), key_provider=provider, verify_hmac=True,
    )
    assert verified["manifest_valid"] is True and verified["hmac_verified"] is True
