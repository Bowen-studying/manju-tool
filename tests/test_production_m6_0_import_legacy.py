from __future__ import annotations

import copy
import importlib
import json
import os

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production import ProductionError, ProductionService, import_legacy_storyboard
from manju.production.import_legacy import LEGACY_IMPORT_MAX_INPUT_BYTES
from manju.production.models import M6_DAG_VERSION, ReasonCode, stages_for_dag
from manju.production.security import MappingHmacKeyProvider


def _storyboard() -> dict:
    return {
        "schema_version": "2.0",
        "title": "M6 legacy fixture",
        "metadata": {"generation_engine": "legacy"},
        "creative_bible": {"style_anchor": "clean ink", "characters": []},
        "scenes": [{
            "scene_id": "1",
            "heading": "INT. ROOM - DAY",
            "shots": [{
                "shot_id": "1.1",
                "duration_seconds": 2.0,
                "visual": {"description": "角色站在窗边", "camera_movement": "slow pan"},
                "prompts": {
                    "image_cn": "室内窗边人物，干净线稿",
                    "image_en": "a character by a window, clean ink",
                    "video_cn": "角色缓慢转身",
                    "video_en": "the character slowly turns",
                },
                "audio": {"speaker": "A", "dialogue": "你好"},
            }],
        }],
    }


def _legacy_v1_storyboard() -> dict:
    return {
        "schema_version": "1.0",
        "title": "M6 old v1 fixture",
        "style_anchor": "flat watercolor",
        "scenes": [{
            "scene_id": "1",
            "scene_heading": "EXT. STREET - NIGHT",
            "shots": [{
                "shot_id": "1.1",
                "visual_description": "一个人走过街灯",
                "image_prompt_cn": "街灯下的人物",
                "image_prompt_en": "a person walking under a street lamp",
                "dialogue_narration": "夜色很静。",
            }],
        }],
    }


def _write_storyboard(path, value=None) -> bytes:
    raw = json.dumps(value or _storyboard(), ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _event_types(project) -> list[str]:
    return [
        item["event_type"]
        for item in ProductionService(str(project / "project.json")).store.events.read()
    ]


def test_m6_imports_legacy_json_and_continues_voice_and_video_prompt(tmp_path):
    source = tmp_path / "legacy-storyboard.json"
    raw = _write_storyboard(source)
    project = tmp_path / "new-project"

    imported = import_legacy_storyboard(
        str(source), str(project), voice_script_enabled=True, video_prompt_enabled=True,
    )
    assert imported.status == "running"
    assert imported.current_stage == "storyboard"
    service = ProductionService(
        str(project / "project.json"),
        hmac_key_provider=MappingHmacKeyProvider({"manju-local-default": b"m6-visual-test-key"}),
    )
    completed = service.run_until_blocked()
    assert completed.status == "completed"
    assert completed.current_stage == "video_prompt"
    assert service.doctor()["status"] == "passed"

    events = service.store.events.read()
    assert [
        (event.get("payload") or {}).get("stage")
        for event in events
        if event["event_type"] == "stage_completed"
    ] == ["storyboard", "voice_script", "video_prompt"]
    assert not any(event["event_type"].startswith(("approval", "grant", "call_")) for event in events)
    assert source.read_bytes() == raw

    stage = next(
        event for event in events
        if event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "storyboard"
    )
    payload = stage["payload"]
    artifact_path = project / payload["artifacts"][0]["path"]
    assert artifact_path.read_bytes() == raw
    assert payload["legacy_import"]["verification_status"] == "unverified"
    graph = service.get_artifact_graph()["graph"]
    current = next(item for item in graph["artifacts"] if item["logical_id"] == "storyboard.output")
    assert current["state"] == "current"
    assert M6_DAG_VERSION in json.dumps(service.store.load_project(), ensure_ascii=False)
    assert os.path.abspath(str(project)) not in json.dumps(events, ensure_ascii=False)
    assert os.path.abspath(str(project)) not in json.dumps(completed.to_dict(), ensure_ascii=False)
    source_absolute = os.path.abspath(str(source))
    for json_file in project.rglob("*.json"):
        assert source_absolute not in json_file.read_text(encoding="utf-8")


def test_m6_accepts_repository_legacy_v1_shape_without_normalizing_bytes(tmp_path):
    source = tmp_path / "old-v1.json"
    raw = _write_storyboard(source, _legacy_v1_storyboard())
    project = tmp_path / "project"

    import_legacy_storyboard(str(source), str(project), video_prompt_enabled=True)
    assert source.read_bytes() == raw
    final = ProductionService(str(project / "project.json")).run_until_blocked()
    assert final.status == "completed"


def test_m6_cli_requires_explicit_file_and_output_and_returns_path_free_dto(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "cli-project"
    result = CliRunner().invoke(
        cli,
        ["project", "import-legacy", str(source), "-o", str(project), "--video-prompts", "--json"],
    )
    assert result.exit_code == 0, result.output
    dto = json.loads(result.output)
    assert dto["status"] == "running"
    assert "path" not in json.dumps(dto, ensure_ascii=False).lower()
    assert (project / "project.json").is_file()


def test_m6_repeated_import_is_idempotent_and_does_not_append_events(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    first = import_legacy_storyboard(str(source), str(project), video_prompt_enabled=True)
    before = (project / "production" / "events.jsonl").read_bytes()
    second = import_legacy_storyboard(str(source), str(project), video_prompt_enabled=True)
    after = (project / "production" / "events.jsonl").read_bytes()
    assert first.to_dict() == second.to_dict()
    assert before == after


def test_m6_repeated_import_rejects_configuration_drift(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    import_legacy_storyboard(str(source), str(project))
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(
            str(source), str(project), voice_script_enabled=True, video_prompt_enabled=True,
        )
    assert exc_info.value.code == ReasonCode.PROJECT_CONTRACT_CHANGED.value


def test_m6_visual_only_contract_keeps_visual_stage(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    imported = import_legacy_storyboard(str(source), str(project), visual_enabled=True)
    service = ProductionService(
        str(project / "project.json"),
        hmac_key_provider=MappingHmacKeyProvider({"manju-local-default": b"m6-visual-test-key"}),
    )
    contract = service.store.validate_contract(service.store.load_project(), imported.run_id)
    assert contract["stage_sequence"] == ["storyboard", "visual"]
    awaiting = service.run_until_blocked()
    assert awaiting.status == "awaiting_approval"
    assert awaiting.current_stage == "visual"


def test_m6_source_revision_requires_a_new_explicit_import(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    import_legacy_storyboard(str(source), str(project))
    service = ProductionService(str(project / "project.json"))
    replacement = project / "outputs" / "replacement-storyboard.json"
    _write_storyboard(replacement, _legacy_v1_storyboard())
    candidate = service.register_revision_candidate(
        logical_id="source.script",
        path=os.path.relpath(replacement, project),
        producer_stage="revision_candidate",
        expected_last_event_hash=service.get_status().last_event_hash,
    )
    available = next(
        item for item in candidate["graph"]["artifacts"]
        if item["logical_id"] == "source.script" and item["state"] == "available"
    )
    with pytest.raises(ProductionError, match="new explicit import"):
        service.preview_revision(changed=({
            "logical_id": "source.script", "version_id": available["version_id"],
        },))


def test_m6_rejects_nonempty_or_unrelated_project_without_overwrite(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(source), str(occupied))
    assert exc_info.value.code == ReasonCode.PROJECT_CONTRACT_CHANGED.value
    assert marker.read_text(encoding="utf-8") == "keep"

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    project_file = unrelated / "project.json"
    project_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(source), str(unrelated))
    assert exc_info.value.code == ReasonCode.PROJECT_CONTRACT_CHANGED.value
    assert project_file.read_text(encoding="utf-8") == "{}"


def test_m6_rejects_invalid_and_oversized_json(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"{not-json")
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(invalid), str(tmp_path / "invalid-project"))
    assert exc_info.value.code == ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (LEGACY_IMPORT_MAX_INPUT_BYTES + 1))
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(oversized), str(tmp_path / "oversized-project"))
    assert exc_info.value.code == ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value

    malformed_schema = tmp_path / "malformed-schema.json"
    _write_storyboard(malformed_schema, {"schema_version": "2.0", "scenes": []})
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(malformed_schema), str(tmp_path / "schema-project"))
    assert exc_info.value.code == ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value

    non_finite = tmp_path / "non-finite.json"
    raw = json.dumps(_storyboard(), ensure_ascii=False).replace('"duration_seconds": 2.0', '"duration_seconds": 1e999999')
    non_finite.write_text(raw, encoding="utf-8")
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(non_finite), str(tmp_path / "non-finite-project"))
    assert exc_info.value.code == ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value


def test_m6_rejects_source_and_target_links_when_host_allows_links(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    linked_source = tmp_path / "linked-source.json"
    try:
        os.symlink(source, linked_source)
    except OSError as exc:
        pytest.skip(f"file links unavailable: {exc}")
    with pytest.raises(ProductionError):
        import_legacy_storyboard(str(linked_source), str(tmp_path / "source-link-project"))

    external = tmp_path / "external"
    external.mkdir()
    linked_target = tmp_path / "linked-target"
    try:
        os.symlink(external, linked_target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")
    with pytest.raises(ProductionError):
        import_legacy_storyboard(str(source), str(linked_target))
    assert not (external / "project.json").exists()


def test_m6_rejects_reparse_points_deterministically(tmp_path, monkeypatch):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    target = tmp_path / "reparse-target"
    target.mkdir()
    importer = importlib.import_module("manju.production.import_legacy")
    real_lstat = importer.os.lstat
    target_abs = os.path.normcase(os.path.abspath(target))

    def fake_lstat(path):
        value = real_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == target_abs:
            class ReparseStat:
                st_mode = value.st_mode
                st_file_attributes = 0x400
            return ReparseStat()
        return value

    monkeypatch.setattr(importer.os, "lstat", fake_lstat)
    with pytest.raises(ProductionError):
        import_legacy_storyboard(str(source), str(target))


def test_m6_tamper_is_fail_closed_and_crash_before_publish_leaves_no_project(tmp_path, monkeypatch):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    import_legacy_storyboard(str(source), str(project))
    stage = next(
        event for event in ProductionService(str(project / "project.json")).store.events.read()
        if event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "storyboard"
    )
    artifact = project / stage["payload"]["artifacts"][0]["path"]
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(ProductionError):
        ProductionService(str(project / "project.json")).get_status()

    clean_source = tmp_path / "clean.json"
    _write_storyboard(clean_source)
    clean_target = tmp_path / "crash-target"
    importer = importlib.import_module("manju.production.import_legacy")

    def crash(*_args, **_kwargs):
        raise RuntimeError("simulated publish interruption")

    monkeypatch.setattr(importer, "_publish_staging", crash)
    with pytest.raises(RuntimeError):
        import_legacy_storyboard(str(clean_source), str(clean_target))
    assert not clean_target.exists()
    assert not (clean_target / "project.json").exists()


def test_m6_source_change_during_preflight_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    importer = importlib.import_module("manju.production.import_legacy")
    original = importer._read_stable_file

    def read_then_change(path, *, max_bytes):
        content = original(path, max_bytes=max_bytes)
        source.write_bytes(content + b"\n")
        return content

    monkeypatch.setattr(importer, "_read_stable_file", read_then_change)
    with pytest.raises(ProductionError) as exc_info:
        import_legacy_storyboard(str(source), str(tmp_path / "project"))
    assert exc_info.value.code == ReasonCode.SOURCE_HASH_MISMATCH.value


def test_m6_recovers_matching_stale_deterministic_staging(tmp_path, monkeypatch):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    importer = importlib.import_module("manju.production.import_legacy")

    with monkeypatch.context() as context:
        context.setattr(importer, "_cleanup_staging", lambda *_args, **_kwargs: None)
        context.setattr(importer, "_publish_staging", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("hard crash")))
        with pytest.raises(RuntimeError, match="hard crash"):
            import_legacy_storyboard(str(source), str(project))

    stale = list(tmp_path.glob("project.m6-import-*"))
    assert len(stale) == 1
    marker = stale[0] / ".legacy-import-transaction.json"
    transaction = json.loads(marker.read_text(encoding="utf-8"))
    lock_file = tmp_path / "project.m6-import.lock"
    lock_file.write_text(json.dumps({
        "pid": 2_147_483_647,
        "lease_id": transaction["lease_id"],
        "created_at": "stale-test",
    }), encoding="ascii")
    imported = import_legacy_storyboard(str(source), str(project))
    assert imported.run_id
    assert project.is_dir()
    assert list(tmp_path.glob("project.m6-import-*")) == []


def test_m6_directory_identity_detects_replacement(tmp_path):
    importer = importlib.import_module("manju.production.import_legacy")
    directory = tmp_path / "identity"
    directory.mkdir()
    identity = importer._directory_identity(str(directory))
    os.rmdir(directory)
    directory.mkdir()
    with pytest.raises(ProductionError, match="directory changed"):
        importer._assert_directory_identity(str(directory), identity)


def test_m6_cleanup_preserves_replaced_unrelated_directory(tmp_path):
    importer = importlib.import_module("manju.production.import_legacy")
    staging = tmp_path / "staging"
    staging.mkdir()
    transaction = {"schema_version": "legacy-import-transaction-v1", "lease_id": "lease"}
    (staging / ".legacy-import-transaction.json").write_text(json.dumps(transaction), encoding="utf-8")
    identity = importer._directory_identity(str(staging))
    os.remove(staging / ".legacy-import-transaction.json")
    os.rmdir(staging)
    staging.mkdir()
    sentinel = staging / "unrelated-user-data.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ProductionError, match="directory changed"):
        importer._cleanup_staging(
            str(staging), expected_identity=identity, expected_transaction=transaction,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_m6_rejects_duplicate_authority_file_records(tmp_path):
    source = tmp_path / "legacy.json"
    _write_storyboard(source)
    project = tmp_path / "project"
    imported = import_legacy_storyboard(str(source), str(project))
    service = ProductionService(str(project / "project.json"))
    project_value = service.store.load_project()
    contract = service.store.validate_contract(project_value, imported.run_id)
    terminal = next(
        event for event in service.store.events.read()
        if event["event_type"] == "stage_completed"
        and (event.get("payload") or {}).get("stage") == "storyboard"
    )
    tampered = copy.deepcopy(terminal)
    tampered["payload"]["authority_files"].append(
        copy.deepcopy(tampered["payload"]["authority_files"][0])
    )
    with pytest.raises(ProductionError, match="authority files"):
        service._validate_legacy_import_stage(
            project=project_value, terminal=tampered,
            snapshot=service.get_status(), contract=contract,
        )


def test_m6_contract_is_distinct_and_old_dag_contracts_remain_readable():
    assert stages_for_dag(M6_DAG_VERSION, ["storyboard"])
    assert stages_for_dag(M6_DAG_VERSION, ["storyboard", "voice_script", "video_prompt"])
