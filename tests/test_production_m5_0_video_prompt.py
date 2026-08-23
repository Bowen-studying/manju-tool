from __future__ import annotations

import json
import os
import stat

import pytest

from manju.production.adapters.base import StageResult
from manju.production.adapters.video_prompt import VideoPromptStageAdapter, build_video_prompt
from manju.production.models import M4_2_DAG_VERSION, M5_DAG_VERSION, ProductionError, stages_for_dag
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.production.store import sha256_file
from manju.utils.runtime import atomic_write_json


KEY = b"m5-video-prompt-test-key"


def _storyboard(source: str = "原始文本", shot_count: int = 2) -> dict:
    shots = []
    for index in range(1, shot_count + 1):
        shots.append({
            "shot_id": f"1.{index}",
            "duration_seconds": 2.0 + index / 10,
            "visual": {
                "description": f"{source} 的镜头 {index}",
                "camera_movement": "slow pan",
                "composition": "medium shot",
            },
            "prompts": {"video_cn": f"角色向前走 {index}", "video_en": f"character walks {index}"},
            "audio": {"speaker": "A", "dialogue": "你好" if index == 1 else ""},
        })
    return {
        "schema_version": "2.0",
        "title": "M5 fixture",
        "creative_bible": {"style_anchor": "clean ink", "characters": []},
        "scenes": [{"scene_id": "1", "heading": "INT. ROOM - DAY", "shots": shots}],
    }


class FixtureStoryboardAdapter:
    contract_version = "fixture-storyboard-m5-v1"

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
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read().strip()
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        atomic_write_json(artifact, _storyboard(source))
        atomic_write_json(authority, {"schema_version": "fixture", "artifact_sha256": sha256_file(artifact)})
        return self._result(stage_run_id, output_dir)

    def inspect(self, *, stage_run_id, output_dir, **_kwargs):
        return self._result(stage_run_id, output_dir)


def _service(tmp_path, *, voices: bool = False, visual: bool = False):
    source = tmp_path / "source.txt"
    source.write_text("原始文本", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        voice_script_enabled=voices, voice_director_enabled=False, voice_tts_enabled=False,
        video_prompt_enabled=True, visual_enabled=visual,
        visual_provider_profile="mock", visual_operation_kind="mock_image",
        visual_maximum_amount="0", hmac_key_id="test-key",
    )
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=FixtureStoryboardAdapter(),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service


def _completed_events(service, run_id):
    return [
        (event.get("payload") or {}).get("stage")
        for event in service.store.events.read()
        if event.get("run_id") == run_id and event.get("event_type") == "stage_completed"
    ]


def _available(graph, logical_id):
    return next(item for item in graph["artifacts"] if item["logical_id"] == logical_id and item["state"] == "available")


def test_m5_dag_is_strict_and_old_m42_remains_unchanged():
    assert stages_for_dag(M4_2_DAG_VERSION, ["storyboard", "voice_script", "voice_director", "voice_tts"])
    assert stages_for_dag(M5_DAG_VERSION, ["storyboard", "video_prompt"])
    assert stages_for_dag(M5_DAG_VERSION, ["storyboard", "voice_script", "voice_director", "voice_tts", "video_prompt", "visual"])
    with pytest.raises(ProductionError):
        stages_for_dag(M5_DAG_VERSION, ["storyboard", "video_prompt", "voice_script"])
    with pytest.raises(ProductionError):
        stages_for_dag(M5_DAG_VERSION, ["storyboard", "voice_script", "visual", "video_prompt"])


def test_m5_storyboard_only_is_one_to_one_and_has_no_paid_events(tmp_path):
    service = _service(tmp_path)
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert _completed_events(service, completed.run_id) == ["storyboard", "video_prompt"]
    events = service.store.events.read()
    assert not any(event["event_type"] in {
        "approval_requested", "approval_approved", "grant_issued", "call_reserved", "call_submitted", "call_settled",
    } for event in events)
    graph = service.get_artifact_graph()["graph"]
    prompt = next(item for item in graph["artifacts"] if item["logical_id"] == "video_prompt.main" and item["state"] == "current")
    assert [item["logical_id"] for item in prompt["depends_on"]] == ["storyboard.output"]
    artifact = json.loads(open(service.store.artifact_path(prompt["path"]), encoding="utf-8").read())
    assert artifact["schema_version"] == "video-prompt-v1"
    assert artifact["shot_count"] == len(artifact["shots"]) == 2
    assert [(item["sequence"], item["scene_id"], item["shot_id"]) for item in artifact["shots"]] == [
        (1, "1", "1.1"), (2, "1", "1.2")
    ]
    dto = service.get_video_prompt_status()
    assert dto["status"] == "completed" and dto["artifact"]["shot_count"] == 2
    assert "path" not in json.dumps(dto, ensure_ascii=False)


def test_m5_voice_then_video_prompt_then_visual_approval(tmp_path):
    service = _service(tmp_path, voices=True, visual=True)
    awaiting = service.run_until_blocked()
    assert awaiting.status == "awaiting_approval"
    assert _completed_events(service, awaiting.run_id) == ["storyboard", "voice_script", "video_prompt"]
    events = service.store.events.read()
    prompt_done = next(event["sequence"] for event in events if event["event_type"] == "stage_completed" and (event.get("payload") or {}).get("stage") == "video_prompt")
    approval = next(event["sequence"] for event in events if event["event_type"] == "approval_requested")
    assert prompt_done < approval
    assert not any(event["event_type"].startswith("call_") for event in events)


def test_video_prompt_adapter_reuses_complete_bytes_and_fails_closed_on_tamper(tmp_path):
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), _storyboard())
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(str(storyboard_path))}
    output_dir = tmp_path / "video_prompt"
    adapter = VideoPromptStageAdapter()
    first = adapter.execute(stage_run_id="video-1", storyboard_path=str(storyboard_path), storyboard_ref=ref, output_dir=str(output_dir))
    artifact_path = first.artifacts[0]["path"]
    artifact_hash = sha256_file(artifact_path)
    second = adapter.execute(stage_run_id="video-1", storyboard_path=str(storyboard_path), storyboard_ref=ref, output_dir=str(output_dir))
    assert first.status == second.status == "completed"
    assert sha256_file(artifact_path) == artifact_hash
    value = json.loads(open(artifact_path, encoding="utf-8").read())
    value["shots"][0]["prompt"] = "tampered"
    atomic_write_json(artifact_path, value)
    assert adapter.execute(stage_run_id="video-1", storyboard_path=str(storyboard_path), storyboard_ref=ref, output_dir=str(output_dir)).reason_code == "STAGE_INTEGRITY_FAILED"


def test_video_prompt_recovers_one_file_partial_publications(tmp_path):
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), _storyboard())
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(str(storyboard_path))}
    output_dir = tmp_path / "video_prompt"
    adapter = VideoPromptStageAdapter()
    completed = adapter.execute(
        stage_run_id="video-1", storyboard_path=str(storyboard_path),
        storyboard_ref=ref, output_dir=str(output_dir),
    )
    artifact_path = completed.artifacts[0]["path"]
    authority_path = completed.authority_path
    os.remove(authority_path)
    assert adapter.execute(
        stage_run_id="video-1", storyboard_path=str(storyboard_path),
        storyboard_ref=ref, output_dir=str(output_dir),
    ).status == "completed"
    os.remove(artifact_path)
    assert adapter.execute(
        stage_run_id="video-1", storyboard_path=str(storyboard_path),
        storyboard_ref=ref, output_dir=str(output_dir),
    ).status == "completed"


def test_video_prompt_failure_status_is_readable_and_adapter_type_is_fixed(tmp_path):
    service = _service(tmp_path)
    for _ in range(8):
        service.advance()
        events = service.store.events.read()
        if any(event["event_type"] == "stage_completed" and (event.get("payload") or {}).get("stage") == "storyboard" for event in events):
            break
    snapshot = service.get_status()
    stage_run_id = f"video-prompt-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.video_prompt_dir(snapshot.run_id, stage_run_id)
    os.makedirs(output_dir, exist_ok=True)
    atomic_write_json(os.path.join(output_dir, "video_prompt.json"), {"invalid": True})
    atomic_write_json(os.path.join(output_dir, "video_prompt_run.json"), {"invalid": True})
    assert service.advance().status == "failed"
    dto = service.get_video_prompt_status()
    assert dto["status"] == "failed"
    assert "path" not in json.dumps(dto, ensure_ascii=False).lower()

    class ForeignAdapter:
        contract_version = "video-prompt-adapter-m5.0-v1"
        model_profile = "deterministic-offline"

    with pytest.raises(TypeError):
        ProductionService(str(service.paths.project_file), video_prompt_adapter=ForeignAdapter())


def test_video_prompt_private_input_refuses_linked_runtime_directory(tmp_path):
    service = _service(tmp_path)
    for _ in range(8):
        service.advance()
        events = service.store.events.read()
        if any(event["event_type"] == "stage_completed" and (event.get("payload") or {}).get("stage") == "storyboard" for event in events):
            break
    snapshot = service.get_status()
    stage_run_id = f"video-prompt-{snapshot.run_id.removeprefix('run_')}"
    output_dir = service.paths.video_prompt_dir(snapshot.run_id, stage_run_id)
    external_dir = tmp_path / "external-runtime-inputs"
    external_dir.mkdir()
    os.makedirs(output_dir, exist_ok=True)
    runtime_link = os.path.join(output_dir, ".runtime_inputs")
    try:
        os.symlink(str(external_dir), runtime_link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links are unavailable on this host: {exc}")

    with pytest.raises(ProductionError, match="private-input directory"):
        service.advance()
    assert list(external_dir.iterdir()) == []


def test_private_input_refuses_windows_reparse_directory_without_writing(tmp_path, monkeypatch):
    service = _service(tmp_path)
    output_dir = os.path.join(service.paths.run_dir("reparse-fixture"), "stages", "video_prompt")
    runtime_dir = os.path.abspath(os.path.join(output_dir, ".runtime_inputs"))
    real_lstat = os.lstat

    def fake_lstat(path):
        value = real_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(runtime_dir):
            class ReparseStat:
                st_mode = value.st_mode
                st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            return ReparseStat()
        return value

    writes = []
    monkeypatch.setattr("manju.production.service.os.lstat", fake_lstat)
    monkeypatch.setattr("manju.production.service.atomic_write_bytes", lambda *_args, **_kwargs: writes.append(True))
    with pytest.raises(ProductionError, match="cannot contain links"):
        service._write_stage_private_copy(
            output_dir=output_dir, logical_id="storyboard.output",
            content_hash="a" * 64, content=b"fixture",
        )
    assert writes == []


def test_video_prompt_input_and_authority_tamper_fail_closed(tmp_path):
    storyboard_path = tmp_path / "storyboard.json"
    atomic_write_json(str(storyboard_path), _storyboard())
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + sha256_file(str(storyboard_path))}
    output_dir = tmp_path / "video_prompt"
    adapter = VideoPromptStageAdapter()
    result = adapter.execute(stage_run_id="video-1", storyboard_path=str(storyboard_path), storyboard_ref=ref, output_dir=str(output_dir))
    authority_path = result.authority_path
    authority = json.loads(open(authority_path, encoding="utf-8").read())
    authority["limits"]["max_shots"] = 1
    atomic_write_json(authority_path, authority)
    assert adapter.inspect(stage_run_id="video-1", output_dir=str(output_dir), storyboard_ref=ref, storyboard_path=str(storyboard_path)).status == "failed"
    atomic_write_json(authority_path, json.loads(open(authority_path, encoding="utf-8").read()))
    storyboard = _storyboard("changed")
    atomic_write_json(str(storyboard_path), storyboard)
    assert adapter.execute(stage_run_id="video-1", storyboard_path=str(storyboard_path), storyboard_ref=ref, output_dir=str(output_dir)).status == "failed"


def test_video_prompt_resource_limit_is_enforced(tmp_path):
    storyboard = _storyboard(shot_count=2)
    ref = {"logical_id": "storyboard.output", "version_id": "sha256:" + "a" * 64}
    with pytest.raises(ValueError, match="shot count"):
        build_video_prompt(storyboard, ref, settings={"max_shots": 1})


def test_source_revision_invalidates_and_regenerates_video_prompt(tmp_path):
    service = _service(tmp_path)
    predecessor = service.run_until_blocked()
    source_v2 = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(source_v2, "w", encoding="utf-8") as handle:
        handle.write("新文本")
    candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(source_v2, service.paths.root),
        producer_stage="revision_candidate", expected_last_event_hash=predecessor.last_event_hash,
    )
    source_v2_ref = _available(candidate["graph"], "source.script")
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2_ref["version_id"]},))
    assert [item["action"] for item in preview["execution_plan"]] == ["regenerate", "regenerate"]
    created = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2_ref["version_id"]},),
        requested_by="tester", reason="source changed", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m5-source-v2",
    )
    successor = created["revision_history"]["revisions"][0]["successor_run_id"]
    assert service.run_until_blocked().run_id == successor
    assert _completed_events(service, successor) == ["storyboard", "video_prompt"]
    graph = service.get_artifact_graph()["graph"]
    prompt = next(item for item in graph["artifacts"] if item["logical_id"] == "video_prompt.main" and item["state"] == "current")
    assert prompt["depends_on"][0]["logical_id"] == "storyboard.output"
