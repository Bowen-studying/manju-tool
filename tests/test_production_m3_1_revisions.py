"""M3.1 revision successor runs and immutable reuse manifests."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from manju.cli import cli
from manju.production import ProductionService, initialize_project
from manju.production.models import ProductionError, fingerprint


def _service(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("revision test", encoding="utf-8")
    initialize_project(source=str(source), source_type="script", output_dir=str(tmp_path / "project"))
    service = ProductionService(str(tmp_path / "project" / "project.json"))
    assert service.advance().status == "running"
    assert service.request_pause().status == "paused"
    return service


def _write(service, name, content):
    path = os.path.join(service.paths.outputs_dir, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return os.path.relpath(path, service.paths.root)


def _register_select(service, logical_id, name, content):
    registered = service.register_artifact(
        logical_id=logical_id, path=_write(service, name, content), producer_stage="test",
        expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
    )
    version = next(item["version_id"] for item in registered["graph"]["artifacts"] if item["logical_id"] == logical_id and item["state"] == "available")
    service.select_artifact_version(logical_id=logical_id, version_id=version,
                                    expected_last_event_hash=registered["last_event_hash"])
    return version


def test_revision_creates_successor_run_and_reuses_only_unaffected_current_versions(tmp_path):
    service = _service(tmp_path)
    _register_select(service, "source.script", "source-v1.txt", b"v1")
    voice_v1 = _register_select(service, "voice.theme", "voice.wav", b"voice")
    source_v2 = _register_select(service, "source.script", "source-v2.txt", b"v2")

    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    assert preview["reused_artifacts"] == [{"logical_id": "voice.theme", "version_id": voice_v1}]
    created = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},), requested_by="operator",
        reason="replace source", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="rev_source_v2",
    )
    revision = created["revision_history"]["revisions"][0]
    successor = revision["successor_run_id"]
    assert revision["predecessor_run_id"] != successor
    assert created["run_id"] == successor
    contract = json.loads(open(service.paths.contract_file(successor), encoding="utf-8").read())
    assert contract["predecessor_run_id"] == revision["predecessor_run_id"]
    assert contract["revision_id"] == "rev_source_v2"
    assert contract["reuse_manifest"] == [{"logical_id": "voice.theme", "version_id": voice_v1}]
    assert "grant_id" not in contract and not any(
        event.get("run_id") == successor and event.get("event_type") == "grant_issued"
        for event in service.store.events.read()
    )
    assert json.loads(open(service.paths.revisions_file, encoding="utf-8").read()) == created["revision_history"]


def test_revision_rejects_stale_preview_after_the_graph_changes(tmp_path):
    service = _service(tmp_path)
    source_v1 = _register_select(service, "source.script", "source-v1.txt", b"v1")
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v1},))
    _register_select(service, "voice.theme", "voice.wav", b"voice")
    with pytest.raises(ProductionError):
        service.create_revision(
            changed=({"logical_id": "source.script", "version_id": source_v1},), requested_by="operator",
            reason="stale", preview_fingerprint=preview["preview_fingerprint"],
            expected_last_event_hash=preview["last_event_hash"],
        )


def test_revision_projection_rejects_successor_without_embedded_revision(tmp_path):
    service = _service(tmp_path)
    project = service.store.load_project()
    predecessor = service.get_status().run_id
    service.store.events.append("run_created", project_id=project["project_id"], run_id="run_forged",
                                payload={"dag_version": "production-m1-v1", "predecessor_run_id": predecessor,
                                         "revision_id": "missing"})
    with pytest.raises(ProductionError):
        service.get_status()


def test_revision_append_failure_leaves_no_half_committed_ledger_fact(tmp_path, monkeypatch):
    service = _service(tmp_path)
    version = _register_select(service, "source.script", "source.txt", b"source")
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": version},))
    runs_dir = os.path.join(service.paths.production_dir, "runs")
    before = set(os.listdir(runs_dir))
    original_append = service.store.events.append

    def fail_successor(event_type, **kwargs):
        if event_type == "run_created":
            raise OSError("simulated append failure")
        return original_append(event_type, **kwargs)

    monkeypatch.setattr(service.store.events, "append", fail_successor)
    with pytest.raises(OSError):
        service.create_revision(
            changed=({"logical_id": "source.script", "version_id": version},), requested_by="operator", reason="failure",
            preview_fingerprint=preview["preview_fingerprint"], expected_last_event_hash=preview["last_event_hash"],
        )
    assert not any(event.get("event_type") == "revision_created" for event in service.store.events.read())
    assert set(os.listdir(runs_dir)) == before
    assert service.get_status().run_id == preview["run_id"]
    assert service.list_revisions()["revision_history"]["revisions"] == []


def test_successor_contract_must_match_ledger_fingerprint_and_reuse_manifest(tmp_path):
    service = _service(tmp_path)
    version = _register_select(service, "source.script", "source.txt", b"source")
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": version},))
    created = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": version},), requested_by="operator", reason="tamper",
        preview_fingerprint=preview["preview_fingerprint"], expected_last_event_hash=preview["last_event_hash"],
    )
    successor = created["run_id"]
    path = service.paths.contract_file(successor)
    contract = json.loads(open(path, encoding="utf-8").read())
    contract["reuse_manifest"] = [{"logical_id": "forged", "version_id": "sha256:" + "0" * 64}]
    contract["contract_fingerprint"] = fingerprint({key: value for key, value in contract.items() if key != "contract_fingerprint"})
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(contract, handle)
    with pytest.raises(ProductionError):
        service.store.validate_contract(service.store.load_project(), successor)


def test_revision_post_commit_append_error_preserves_recoverable_contract(tmp_path, monkeypatch):
    service = _service(tmp_path)
    version = _register_select(service, "source.script", "source.txt", b"source")
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": version},))
    original_append = service.store.events.append

    def append_then_interrupt(event_type, **kwargs):
        value = original_append(event_type, **kwargs)
        if event_type == "run_created":
            raise KeyboardInterrupt("simulated post-commit interrupt")
        return value

    monkeypatch.setattr(service.store.events, "append", append_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.create_revision(
            changed=({"logical_id": "source.script", "version_id": version},), requested_by="operator", reason="interrupt",
            preview_fingerprint=preview["preview_fingerprint"], expected_last_event_hash=preview["last_event_hash"],
        )
    successor = service.get_status().run_id
    assert successor != preview["run_id"] and os.path.isfile(service.paths.contract_file(successor))
    assert service.store.validate_contract(service.store.load_project(), successor)["run_id"] == successor


def test_revision_cli_returns_preview_and_successor_dtos(tmp_path):
    service = _service(tmp_path)
    version = _register_select(service, "source.script", "source.txt", b"source")
    project_json = os.path.join(service.paths.root, "project.json")
    changed = json.dumps({"logical_id": "source.script", "version_id": version})
    runner = CliRunner()
    preview = runner.invoke(cli, ["revision", "preview", project_json, "--changed", changed, "--json"])
    assert preview.exit_code == 0, preview.output
    data = json.loads(preview.output)
    created = runner.invoke(cli, ["revision", "create", project_json, "--changed", changed, "--requested-by", "operator",
                                  "--reason", "CLI", "--preview-fingerprint", data["preview_fingerprint"],
                                  "--expected-last-event-hash", data["last_event_hash"], "--json"])
    assert created.exit_code == 0, created.output
    listed = runner.invoke(cli, ["revision", "list", project_json, "--json"])
    assert listed.exit_code == 0 and len(json.loads(listed.output)["revision_history"]["revisions"]) == 1
