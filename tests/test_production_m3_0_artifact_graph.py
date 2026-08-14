"""M3.0 artifact graph: immutable versions and deterministic invalidation."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production import ProductionService, initialize_project
from manju.production.audit import export_audit_snapshot, verify_audit_snapshot
from manju.production.models import ProductionError
from manju.production.security import MappingHmacKeyProvider


def _service(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("M3 artifact graph test", encoding="utf-8")
    initialize_project(source=str(source), source_type="script", output_dir=str(tmp_path / "project"))
    return ProductionService(str(tmp_path / "project" / "project.json"))


def _write_artifact(service, name, content):
    path = os.path.join(service.paths.outputs_dir, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return os.path.relpath(path, service.paths.root)


def _register(service, logical_id, path, *, stage, depends_on=()):
    value = service.register_artifact(
        logical_id=logical_id, path=path, producer_stage=stage, depends_on=tuple(depends_on),
        expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
    )
    normalized_path = path.replace("\\", "/")
    record = next(item for item in value["graph"]["artifacts"] if item["logical_id"] == logical_id and item["path"] == normalized_path)
    return record["version_id"], value


def _select(service, logical_id, version_id):
    return service.select_artifact_version(
        logical_id=logical_id, version_id=version_id,
        expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
    )


def _state(graph, logical_id, version_id):
    return next(item["state"] for item in graph["artifacts"] if item["logical_id"] == logical_id and item["version_id"] == version_id)


def test_selecting_new_source_version_invalidates_only_its_downstream_closure(tmp_path):
    service = _service(tmp_path)
    source_v1, _ = _register(service, "source.script", _write_artifact(service, "source-v1.txt", b"v1"), stage="source")
    _select(service, "source.script", source_v1)
    storyboard_v1, _ = _register(
        service, "storyboard.main", _write_artifact(service, "storyboard-v1.json", b"storyboard"),
        stage="storyboard", depends_on=({"logical_id": "source.script", "version_id": source_v1},),
    )
    _select(service, "storyboard.main", storyboard_v1)
    image_v1, _ = _register(
        service, "visual.shot-01", _write_artifact(service, "shot-01.png", b"image"),
        stage="visual", depends_on=({"logical_id": "storyboard.main", "version_id": storyboard_v1},),
    )
    _select(service, "visual.shot-01", image_v1)
    unrelated_v1, _ = _register(service, "voice.theme", _write_artifact(service, "theme.wav", b"voice"), stage="voice")
    _select(service, "voice.theme", unrelated_v1)
    source_v2, _ = _register(service, "source.script", _write_artifact(service, "source-v2.txt", b"v2"), stage="source")

    selected = _select(service, "source.script", source_v2)
    graph = selected["graph"]
    assert _state(graph, "source.script", source_v1) == "superseded"
    assert _state(graph, "source.script", source_v2) == "current"
    assert _state(graph, "storyboard.main", storyboard_v1) == "invalidated"
    assert _state(graph, "visual.shot-01", image_v1) == "invalidated"
    assert _state(graph, "voice.theme", unrelated_v1) == "current"
    assert graph["current"] == [
        {"logical_id": "source.script", "version_id": source_v2},
        {"logical_id": "voice.theme", "version_id": unrelated_v1},
    ]
    projection = json.loads(open(service.paths.artifacts_file, encoding="utf-8").read())
    assert projection == graph


def test_graph_rejects_unregistered_dependency_and_paths_outside_project(tmp_path):
    service = _service(tmp_path)
    valid_path = _write_artifact(service, "local.txt", b"local")
    with pytest.raises(ProductionError):
        service.register_artifact(
            logical_id="storyboard.main", path=valid_path, producer_stage="storyboard",
            depends_on=({"logical_id": "source.script", "version_id": "sha256:" + "0" * 64},),
            expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
        )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(ProductionError):
        service.register_artifact(
            logical_id="outside", path=os.path.relpath(outside, service.paths.root), producer_stage="test",
            expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
        )


def test_graph_rejects_late_artifacts_that_depend_on_superseded_versions(tmp_path):
    service = _service(tmp_path)
    source_v1, _ = _register(service, "source.script", _write_artifact(service, "source-v1.txt", b"v1"), stage="source")
    _select(service, "source.script", source_v1)
    source_v2, _ = _register(service, "source.script", _write_artifact(service, "source-v2.txt", b"v2"), stage="source")
    _select(service, "source.script", source_v2)
    with pytest.raises(ProductionError):
        _register(
            service, "storyboard.late", _write_artifact(service, "late.json", b"late"), stage="storyboard",
            depends_on=({"logical_id": "source.script", "version_id": source_v1},),
        )


def test_graph_rejects_artifacts_that_depend_on_an_unselected_version(tmp_path):
    service = _service(tmp_path)
    source_v1, _ = _register(service, "source.script", _write_artifact(service, "source-v1.txt", b"v1"), stage="source")
    with pytest.raises(ProductionError, match="current version"):
        _register(
            service, "storyboard.early", _write_artifact(service, "early.json", b"early"), stage="storyboard",
            depends_on=({"logical_id": "source.script", "version_id": source_v1},),
        )


def test_graph_rejects_modified_registered_artifact_bytes(tmp_path):
    service = _service(tmp_path)
    path = _write_artifact(service, "immutable.bin", b"original")
    version_id, _ = _register(service, "source.script", path, stage="source")
    _select(service, "source.script", version_id)
    with open(os.path.join(service.paths.root, path), "wb") as handle:
        handle.write(b"modified")
    with pytest.raises(ProductionError, match="content no longer matches"):
        service.get_artifact_graph()


def test_graph_rejects_project_relative_symlink_that_escapes_project(tmp_path):
    service = _service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "artifact.bin").write_bytes(b"outside")
    link = os.path.join(service.paths.outputs_dir, "linked")
    try:
        os.symlink(str(outside), link, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("the current Windows account cannot create a test symlink")
    with pytest.raises(ProductionError, match="link or reparse"):
        service.register_artifact(
            logical_id="outside", path="outputs/linked/artifact.bin", producer_stage="test",
            expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
        )


def test_audit_export_includes_registered_artifact_files_only(tmp_path):
    service = _service(tmp_path)
    path = _write_artifact(service, "registered.bin", b"registered")
    version_id, _ = _register(service, "source.script", path, stage="source")
    _select(service, "source.script", version_id)
    unregistered = os.path.join(service.paths.outputs_dir, "unregistered.bin")
    with open(unregistered, "wb") as handle:
        handle.write(b"unregistered")
    provider = MappingHmacKeyProvider({"manju-local-default": b"m3-test-key"})
    with pytest.raises(ProductionError):
        export_audit_snapshot(project_json=os.path.join(service.paths.root, "project.json"),
                              destination=str(tmp_path / "rejected-audit"), key_provider=provider)
    os.remove(unregistered)
    exported = export_audit_snapshot(project_json=os.path.join(service.paths.root, "project.json"),
                                     destination=str(tmp_path / "audit"), key_provider=provider)
    assert exported["manifest_entries"] > 0
    assert os.path.isfile(os.path.join(exported["destination"], "project", path))
    assert verify_audit_snapshot(destination=exported["destination"], key_provider=provider, verify_hmac=True)["hmac_verified"] is True


def test_projection_rejects_selection_with_forged_invalidation_set(tmp_path):
    service = _service(tmp_path)
    source_v1, _ = _register(service, "source.script", _write_artifact(service, "source-v1.txt", b"v1"), stage="source")
    _select(service, "source.script", source_v1)
    source_v2, _ = _register(service, "source.script", _write_artifact(service, "source-v2.txt", b"v2"), stage="source")
    project = service.store.load_project()
    service.store.events.append(
        "artifact_version_selected", project_id=project["project_id"],
        payload={"logical_id": "source.script", "version_id": source_v2,
                 "previous_version_id": source_v1, "invalidated": [{"logical_id": "forged", "version_id": "sha256:" + "0" * 64}]},
    )
    with pytest.raises(ProductionError, match="invalidated"):
        service.get_artifact_graph()


def test_artifact_cli_returns_stable_graph_dto(tmp_path):
    service = _service(tmp_path)
    path = _write_artifact(service, "source.txt", b"source")
    project_json = os.path.join(service.paths.root, "project.json")
    runner = CliRunner()
    registered = runner.invoke(cli, ["artifact", "register", project_json, "--logical-id", "source.script",
                                    "--path", path, "--producer-stage", "source",
                                    "--expected-last-event-hash", service.get_artifact_graph()["last_event_hash"], "--json"])
    assert registered.exit_code == 0, registered.output
    data = json.loads(registered.output)
    version_id = data["graph"]["artifacts"][0]["version_id"]
    selected = runner.invoke(cli, ["artifact", "select", project_json, "--logical-id", "source.script",
                                   "--version-id", version_id, "--expected-last-event-hash", data["last_event_hash"], "--json"])
    assert selected.exit_code == 0, selected.output
    status = runner.invoke(cli, ["artifact", "status", project_json, "--json"])
    assert status.exit_code == 0 and json.loads(status.output)["graph"]["current"] == [
        {"logical_id": "source.script", "version_id": version_id}
    ]
