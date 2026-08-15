"""M3.3 atomic candidate selection and runtime selective stage reuse."""

from __future__ import annotations

import os

import pytest

from manju.production.artifacts import ArtifactRef
from manju.production.models import ProductionError

from tests.test_production_m3_2_revision_paid_closure import _register_candidate, _register_select
from tests.test_production_m2_7_contractual_tariff import _import_success, _service


def _register_derived(service, *, logical_id, name, content, stage, depends_on):
    path = os.path.join(service.paths.outputs_dir, name)
    with open(path, "wb") as handle:
        handle.write(content)
    registered = service.register_artifact(
        logical_id=logical_id, path=os.path.relpath(path, service.paths.root), producer_stage=stage,
        depends_on=tuple(item.to_dict() for item in depends_on),
        expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
    )
    version = next(item["version_id"] for item in registered["graph"]["artifacts"]
                   if item["logical_id"] == logical_id and item["state"] == "available")
    service.select_artifact_version(logical_id=logical_id, version_id=version,
                                    expected_last_event_hash=registered["last_event_hash"])
    from manju.production.artifacts import ArtifactRef
    return ArtifactRef(logical_id, version)


def _complete_predecessor_with_graph(tmp_path, monkeypatch):
    service, grant, _project = _service(tmp_path, monkeypatch)
    from manju.production.artifacts import ArtifactRef
    source = ArtifactRef("source.script", _register_select(service, logical_id="source.script", name="source-v1.txt", content=b"v1"))
    style = ArtifactRef("style.reference", _register_select(service, logical_id="style.reference", name="style-v1.txt", content=b"style"))
    storyboard = _register_derived(service, logical_id="storyboard.output", name="storyboard-v1.json", content=b"storyboard-v1",
                                    stage="storyboard", depends_on=(source,))
    visual = _register_derived(service, logical_id="visual.asset", name="visual-v1.png", content=b"visual-v1",
                                stage="visual", depends_on=(storyboard, style))
    metadata = ArtifactRef("metadata.note", _register_select(service, logical_id="metadata.note", name="metadata-v1.txt", content=b"old"))
    _prepared, dispatch, imported = _import_success(service, grant, tmp_path)
    service.settle_manual_contractual_tariff(operation_id=dispatch.operation_id, expected_last_event_hash=imported.last_event_hash)
    predecessor = service.run_until_blocked()
    assert predecessor.status == "completed"
    return service, predecessor, source, style, storyboard, visual, metadata


def test_candidate_revision_freezes_graph_scope_and_binds_source_style_to_successor_approval(tmp_path, monkeypatch):
    service, predecessor, source, style, storyboard, visual, metadata = _complete_predecessor_with_graph(tmp_path, monkeypatch)
    source_v2 = _register_candidate(service, logical_id="source.script", name="source-v2.txt", content=b"v2")
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    assert preview["predecessor_selection"] == [item.to_dict() for item in sorted((source, style, storyboard, visual, metadata))]
    assert preview["affected_artifacts"] == [item.to_dict() for item in sorted((storyboard, visual))]
    assert preview["reused_artifacts"] == [item.to_dict() for item in sorted((style, metadata))]
    assert [item["action"] for item in preview["execution_plan"]] == ["regenerate", "regenerate"]
    created = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},), requested_by="m3.3-fixture",
        reason="source invalidates storyboard and visual", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m3_3_source_v2",
    )
    successor = created["run_id"]
    events = service.store.events.read()
    completed_at = next(event["sequence"] for event in events if event["run_id"] == predecessor.run_id and event["event_type"] == "run_completed")
    assert not any(event["sequence"] > completed_at and event["run_id"] == predecessor.run_id
                   and event["event_type"] in {"artifact_registered", "artifact_version_selected"} for event in events)
    contract = service.store.validate_contract(service.store.load_project(), successor)
    assert contract["predecessor_selection"] == preview["predecessor_selection"]
    assert contract["successor_selection"] == preview["successor_selection"]
    service.advance(); service.advance(); service.advance()
    awaiting = service.get_status()
    assert awaiting.status == "awaiting_approval"
    approval = next(item["approval_request"] for item in service.list_approvals() if item["approval_request"]["run_id"] == successor)
    bound = {(item["artifact_id"], item["version_id"]) for item in approval["artifact_versions"]}
    assert ("source.script", source_v2) in bound and ("style.reference", style.version_id) in bound


def test_unaffected_candidate_reuses_both_stages_without_approval_or_provider_operation(tmp_path, monkeypatch):
    service, predecessor, _source, _style, _storyboard, _visual, _metadata = _complete_predecessor_with_graph(tmp_path, monkeypatch)
    metadata_v2 = _register_candidate(service, logical_id="metadata.note", name="metadata-v2.txt", content=b"new")
    preview = service.preview_revision(changed=({"logical_id": "metadata.note", "version_id": metadata_v2},))
    assert preview["affected_artifacts"] == []
    assert [item["action"] for item in preview["execution_plan"]] == ["reuse", "reuse"]
    created = service.create_revision(
        changed=({"logical_id": "metadata.note", "version_id": metadata_v2},), requested_by="m3.3-fixture",
        reason="metadata only", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m3_3_metadata_v2",
    )
    successor = created["run_id"]
    service.advance(); service.advance(); service.advance(); service.advance()
    completed = service.get_status()
    assert completed.run_id == successor and completed.status == "completed"
    successor_events = [event for event in service.store.events.read() if event.get("run_id") == successor]
    assert not any(event["event_type"] in {"approval_requested", "grant_issued", "call_reserved", "call_submitted"}
                   for event in successor_events)
    reused = [event for event in successor_events if event["event_type"] == "stage_completed"]
    assert [event["payload"]["reused_from"]["run_id"] for event in reused] == [predecessor.run_id, predecessor.run_id]


def test_completed_predecessor_rejects_legacy_register_and_project_scoped_selection(tmp_path, monkeypatch):
    service, predecessor, source, _style, storyboard, visual, _metadata = _complete_predecessor_with_graph(tmp_path, monkeypatch)
    rogue_path = os.path.join(service.paths.outputs_dir, "rogue.txt")
    with open(rogue_path, "wb") as handle:
        handle.write(b"rogue")
    with pytest.raises(ProductionError):
        service.register_artifact(
            logical_id="source.script", path=os.path.relpath(rogue_path, service.paths.root), producer_stage="rogue",
            expected_last_event_hash=service.get_status().last_event_hash,
        )
    source_v2 = _register_candidate(service, logical_id="source.script", name="source-v2.txt", content=b"v2")
    invalidated = service.store.artifact_graph().invalidated_by(source)
    service.store.events.append(
        "artifact_version_selected", project_id=service.store.load_project()["project_id"], run_id="",
        payload={"logical_id": "source.script", "version_id": source_v2, "previous_version_id": source.version_id,
                 "invalidated": [item.to_dict() for item in invalidated]},
    )
    with pytest.raises(ProductionError):
        service.get_status()


def test_same_revision_candidate_cannot_depend_on_replaced_logical_id(tmp_path, monkeypatch):
    service, _predecessor, _source, style, _storyboard, _visual, _metadata = _complete_predecessor_with_graph(tmp_path, monkeypatch)
    source_path = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(source_path, "wb") as handle:
        handle.write(b"v2")
    source_candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(source_path, service.paths.root), producer_stage="candidate",
        depends_on=(style.to_dict(),), expected_last_event_hash=service.get_status().last_event_hash,
    )
    source_v2 = next(item["version_id"] for item in source_candidate["graph"]["artifacts"]
                     if item["logical_id"] == "source.script" and item["state"] == "available")
    style_v2 = _register_candidate(service, logical_id="style.reference", name="style-v2.txt", content=b"style-v2")
    with pytest.raises(ProductionError):
        service.preview_revision(changed=(
            {"logical_id": "source.script", "version_id": source_v2},
            {"logical_id": "style.reference", "version_id": style_v2},
        ))


def test_candidate_cannot_depend_on_its_own_replacement_invalidation_closure(tmp_path, monkeypatch):
    service, _predecessor, _source, _style, storyboard, _visual, _metadata = _complete_predecessor_with_graph(tmp_path, monkeypatch)
    path = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(path, "wb") as handle:
        handle.write(b"v2")
    candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(path, service.paths.root), producer_stage="candidate",
        depends_on=(storyboard.to_dict(),), expected_last_event_hash=service.get_status().last_event_hash,
    )
    source_v2 = next(item["version_id"] for item in candidate["graph"]["artifacts"]
                     if item["logical_id"] == "source.script" and item["state"] == "available")
    with pytest.raises(ProductionError):
        service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
