"""M3.4: selected revision inputs drive execution and commit stage outputs."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from manju.production.adapters.base import StageResult
from manju.production.adapters.visual import MockVisualProvider, VisualStageAdapter
from manju.production.audit import export_audit_snapshot, verify_audit_snapshot
from manju.production.models import ProductionError
from manju.production.providers import ProviderCapabilities, ProviderObservation, VisualProviderRegistry
from manju.production.security import MappingHmacKeyProvider
from manju.production.service import ProductionService, initialize_project
from manju.utils.runtime import atomic_write_json


KEY = b"m3-4-artifact-driven-inputs"


class SourceEchoStoryboardAdapter:
    contract_version = "source-echo-storyboard-m3-4-v1"

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.source_paths: list[str] = []
        self.after_private_copy = None

    def execute(self, *, stage_run_id, source_path, output_dir, **_kwargs):
        os.makedirs(output_dir, exist_ok=True)
        self.source_paths.append(source_path)
        if self.after_private_copy is not None:
            self.after_private_copy()
        source = open(source_path, encoding="utf-8").read()
        self.sources.append(source)
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        atomic_write_json(artifact, {"schema_version": "1", "source": source})
        artifact_hash = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
        atomic_write_json(authority, {"schema_version": "1", "artifact_sha256": artifact_hash})
        authority_hash = hashlib.sha256(open(authority, "rb").read()).hexdigest()
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"path": artifact, "version_id": "sha256:" + artifact_hash},),
            authority_path=authority, authority_hash=authority_hash,
            authority_files=({"path": authority, "sha256": authority_hash},),
        )

    def inspect(self, *, stage_run_id, output_dir, **_kwargs):
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        if not os.path.isfile(artifact) or not os.path.isfile(authority):
            return None
        artifact_hash = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
        authority_hash = hashlib.sha256(open(authority, "rb").read()).hexdigest()
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"path": artifact, "version_id": "sha256:" + artifact_hash},),
            authority_path=authority, authority_hash=authority_hash,
            authority_files=({"path": authority, "sha256": authority_hash},),
        )


class ConstantStoryboardAdapter(SourceEchoStoryboardAdapter):
    def execute(self, *, stage_run_id, source_path, output_dir, **_kwargs):
        os.makedirs(output_dir, exist_ok=True)
        self.sources.append(open(source_path, encoding="utf-8").read())
        artifact = os.path.join(output_dir, "storyboard.json")
        authority = os.path.join(output_dir, "authority.json")
        atomic_write_json(artifact, {"schema_version": "1", "stable": True})
        artifact_hash = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
        atomic_write_json(authority, {"schema_version": "1", "artifact_sha256": artifact_hash})
        authority_hash = hashlib.sha256(open(authority, "rb").read()).hexdigest()
        return StageResult(
            status="completed", stage_run_id=stage_run_id,
            artifacts=({"path": artifact, "version_id": "sha256:" + artifact_hash},),
            authority_path=authority, authority_hash=authority_hash,
            authority_files=({"path": authority, "sha256": authority_hash},),
        )


class VerifiedRegistryProvider(MockVisualProvider):
    capabilities = ProviderCapabilities(True, True, True, True)

    def reconcile(self, provider_job_id):
        observed = super().reconcile(provider_job_id)
        return ProviderObservation(
            outcome=observed.outcome, provider_job_id=observed.provider_job_id,
            result_fingerprint=observed.result_fingerprint, artifact_bytes=observed.artifact_bytes,
            artifact_media_type=observed.artifact_media_type, actual_amount=observed.actual_amount,
            currency=observed.currency, usage=observed.usage, cost_source="provider_response",
        )


class LegacyVisualStageAdapter(VisualStageAdapter):
    contract_version = "visual-adapter-m2-3-v1"

def _current(graph, logical_id):
    return next(item for item in graph["artifacts"] if item["logical_id"] == logical_id and item["state"] == "current")


def _available(graph, logical_id):
    return next(item for item in graph["artifacts"] if item["logical_id"] == logical_id and item["state"] == "available")


def _register_style(service):
    path = os.path.join(service.paths.outputs_dir, "style-v1.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("ink and paper")
    registered = service.register_artifact(
        logical_id="style.reference", path=os.path.relpath(path, service.paths.root), producer_stage="style",
        expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
    )
    version = _available(registered["graph"], "style.reference")["version_id"]
    return service.select_artifact_version(
        logical_id="style.reference", version_id=version, expected_last_event_hash=registered["last_event_hash"],
    )


def _approve_and_complete(service, *, grant_id):
    awaiting = service.get_status()
    request_id = "approval-" + awaiting.run_id.removeprefix("run_")
    approved = service.decide_approval(request_id, decision="approve", reviewer="reviewer",
                                       expected_last_event_hash=awaiting.last_event_hash)
    service.issue_grant(request_id, grant_id=grant_id, issued_by="issuer",
                        expected_last_event_hash=approved.last_event_hash)
    return service.run_until_blocked()


def _service(tmp_path, *, storyboard_adapter=None, visual_adapter_type=VisualStageAdapter):
    source = tmp_path / "source.txt"
    source.write_text("version one", encoding="utf-8")
    project = tmp_path / "project"
    initialize_project(
        source=str(source), source_type="script", output_dir=str(project), engine="agent",
        visual_enabled=True, visual_maximum_paid_calls=1, visual_maximum_amount="5",
        visual_provider_profile="sync-image", visual_provider_request={
            "prompt": "Draw the approved production scene", "model": "image-test", "size": "1024x1024",
        }, visual_operation_kind="image_generation", hmac_key_id="test-key",
    )
    storyboard = storyboard_adapter or SourceEchoStoryboardAdapter()
    provider = VerifiedRegistryProvider()
    service = ProductionService(
        str(project / "project.json"), storyboard_adapter=storyboard,
        visual_adapter=visual_adapter_type(provider_registry=VisualProviderRegistry({"sync-image": provider})),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    service._configured_model = lambda: "fixture"
    return service, storyboard, provider


def test_source_revision_drives_runtime_prompt_and_current_artifact_chain(tmp_path):
    service, storyboard, provider = _service(tmp_path)
    service.advance()  # Create the predecessor and bootstrap source.script.
    _register_style(service)
    service.advance()
    service.advance()
    predecessor = _approve_and_complete(service, grant_id="predecessor-grant")
    predecessor_request = next(
        event["payload"]["approval_request"]["operation_intents"][0]["provider_request"]
        for event in service.store.events.read()
        if event["run_id"] == predecessor.run_id and event["event_type"] == "approval_requested"
    )

    candidate_path = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(candidate_path, "w", encoding="utf-8") as handle:
        handle.write("version two")
    candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(candidate_path, service.paths.root),
        producer_stage="revision_candidate", expected_last_event_hash=service.get_status().last_event_hash,
    )
    source_v2 = _available(candidate["graph"], "source.script")["version_id"]
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    successor = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},), requested_by="tester",
        reason="source update", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m3_4_source_v2",
    )["run_id"]

    service.advance()
    service.advance()
    assert storyboard.sources[-1] == "version two"
    graph = service.get_artifact_graph()["graph"]
    current_storyboard = _current(graph, "storyboard.output")
    assert current_storyboard["depends_on"] == [{"logical_id": "source.script", "version_id": source_v2}]

    awaiting = service.advance()
    successor_request = next(
        event["payload"]["approval_request"]["operation_intents"][0]["provider_request"]
        for event in service.store.events.read()
        if event["run_id"] == successor and event["event_type"] == "approval_requested"
    )
    assert "version two" in successor_request["prompt"]
    assert successor_request != predecessor_request
    assert awaiting.status == "awaiting_approval"
    assert _approve_and_complete(service, grant_id="successor-grant").run_id == successor

    graph = service.get_artifact_graph()["graph"]
    current_visual = _current(graph, "visual.asset")
    assert {item["logical_id"] for item in current_visual["depends_on"]} == {"storyboard.output", "style.reference"}
    assert sum(provider.submit_counts.values()) == 2
    before = len(graph["artifacts"])
    assert service.advance().status == "completed"
    assert len(service.get_artifact_graph()["graph"]["artifacts"]) == before
    audit_dir = tmp_path / "audit"
    exported = export_audit_snapshot(
        project_json=service.paths.project_file, destination=str(audit_dir),
        key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    assert exported["bundle_type"] == "evidence_snapshot"
    assert verify_audit_snapshot(
        destination=str(audit_dir), key_provider=MappingHmacKeyProvider({"test-key": KEY}), verify_hmac=True,
    )["hmac_verified"] is True
    assert list((audit_dir / "project" / "production" / "runs").rglob(".runtime_inputs/*.bin"))

    private_input = next((tmp_path / "project" / "production" / "runs").rglob(".runtime_inputs/*.bin"))
    private_bytes = private_input.read_bytes()
    private_input.write_bytes(b"tampered stage-private evidence")
    with pytest.raises(ProductionError):
        export_audit_snapshot(
            project_json=service.paths.project_file, destination=str(tmp_path / "private-tampered-audit"),
            key_provider=MappingHmacKeyProvider({"test-key": KEY}),
        )
    private_input.write_bytes(private_bytes)

    predecessor_contract = json.loads(open(service.paths.contract_file(predecessor.run_id), encoding="utf-8").read())
    predecessor_snapshot = service.store.artifact_path(
        predecessor_contract["runtime_inputs"]["source.script"]["snapshot_path"]
    )
    with open(predecessor_snapshot, "wb") as handle:
        handle.write(b"tampered predecessor snapshot")
    with pytest.raises(ProductionError):
        export_audit_snapshot(
            project_json=service.paths.project_file, destination=str(tmp_path / "predecessor-tampered-audit"),
            key_provider=MappingHmacKeyProvider({"test-key": KEY}),
        )


def test_selected_source_snapshot_preserves_approved_bytes_and_detects_later_tampering(tmp_path):
    service, storyboard, _provider = _service(tmp_path)
    service.advance()
    _register_style(service)
    service.advance()
    service.advance()
    _approve_and_complete(service, grant_id="predecessor-grant")
    path = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("version two")
    candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(path, service.paths.root), producer_stage="revision_candidate",
        expected_last_event_hash=service.get_status().last_event_hash,
    )
    source_v2 = _available(candidate["graph"], "source.script")["version_id"]
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    successor = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},), requested_by="tester",
        reason="tamper check", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m3_4_tamper",
    )["run_id"]
    contract = json.loads(open(service.paths.contract_file(successor), encoding="utf-8").read())
    snapshot_path = service.store.artifact_path(contract["runtime_inputs"]["source.script"]["snapshot_path"])
    assert open(snapshot_path, encoding="utf-8").read() == "version two"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("changed after approval")
    with pytest.raises(ProductionError):
        service.advance()
    assert storyboard.sources == ["version one"]


def test_style_only_revision_reuses_storyboard_and_commits_regenerated_visual(tmp_path):
    service, _storyboard, provider = _service(tmp_path)
    service.advance()
    _register_style(service)
    service.advance()
    service.advance()
    predecessor = _approve_and_complete(service, grant_id="predecessor-grant")
    style_v2_path = os.path.join(service.paths.outputs_dir, "style-v2.txt")
    with open(style_v2_path, "w", encoding="utf-8") as handle:
        handle.write("colored pencil")
    candidate = service.register_revision_candidate(
        logical_id="style.reference", path=os.path.relpath(style_v2_path, service.paths.root),
        producer_stage="revision_candidate", expected_last_event_hash=service.get_status().last_event_hash,
    )
    style_v2 = _available(candidate["graph"], "style.reference")["version_id"]
    preview = service.preview_revision(changed=({"logical_id": "style.reference", "version_id": style_v2},))
    assert [item["action"] for item in preview["execution_plan"]] == ["reuse", "regenerate"]
    successor = service.create_revision(
        changed=({"logical_id": "style.reference", "version_id": style_v2},), requested_by="tester",
        reason="style update", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m3_4_style_v2",
    )["run_id"]
    service.advance()
    service.advance()
    assert service.advance().status == "awaiting_approval"
    assert _approve_and_complete(service, grant_id="successor-grant").run_id == successor
    graph = service.get_artifact_graph()["graph"]
    visual = _current(graph, "visual.asset")
    assert {item["version_id"] for item in visual["depends_on"]} >= {style_v2}
    assert sum(provider.submit_counts.values()) == 2
    assert predecessor.run_id != successor

    contract = json.loads(open(service.paths.contract_file(successor), encoding="utf-8").read())
    style_snapshot = service.store.artifact_path(contract["runtime_inputs"]["style.reference"]["snapshot_path"])
    with open(style_snapshot, "wb") as handle:
        handle.write(b"tampered style snapshot")
    with pytest.raises(ProductionError):
        service.get_status()
    with pytest.raises(ProductionError):
        service.advance()
    assert service.doctor()["integrity_status"] == "failed"
    with pytest.raises(ProductionError):
        export_audit_snapshot(
            project_json=service.paths.project_file, destination=str(tmp_path / "tampered-audit"),
            key_provider=MappingHmacKeyProvider({"test-key": KEY}),
        )


def test_identical_storyboard_output_replays_as_new_materialization(tmp_path):
    service, storyboard, _provider = _service(tmp_path, storyboard_adapter=ConstantStoryboardAdapter())
    service.advance()
    _register_style(service)
    service.advance()
    service.advance()
    _approve_and_complete(service, grant_id="predecessor-grant")
    path = os.path.join(service.paths.outputs_dir, "source-v2.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("version two")
    candidate = service.register_revision_candidate(
        logical_id="source.script", path=os.path.relpath(path, service.paths.root), producer_stage="revision_candidate",
        expected_last_event_hash=service.get_status().last_event_hash,
    )
    source_v2 = _available(candidate["graph"], "source.script")["version_id"]
    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},), requested_by="tester",
        reason="same output", preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"], revision_id="m3_4_identical",
    )
    service.advance()
    service.advance()
    assert storyboard.sources[-1] == "version two"
    assert service.advance().status == "awaiting_approval"
    _approve_and_complete(service, grant_id="successor-grant")
    assert service.get_status().status == "completed"


def test_stage_adapter_receives_private_verified_source_copy(tmp_path):
    service, storyboard, _provider = _service(tmp_path)
    service.advance()
    _register_style(service)
    contract = json.loads(open(service.paths.contract_file(service.get_status().run_id), encoding="utf-8").read())
    snapshot = service.store.artifact_path(contract["runtime_inputs"]["source.script"]["snapshot_path"])

    def mutate_snapshot_after_copy():
        with open(snapshot, "w", encoding="utf-8") as handle:
            handle.write("replacement after the stage copy")

    storyboard.after_private_copy = mutate_snapshot_after_copy
    service.advance()
    assert storyboard.sources == ["version one"]
    assert ".runtime_inputs" in storyboard.source_paths[0]


def test_direct_stage_output_candidate_is_rejected_before_paid_side_effect(tmp_path):
    service, _storyboard, provider = _service(tmp_path)
    service.advance()
    _register_style(service)
    service.advance()
    service.advance()
    _approve_and_complete(service, grant_id="predecessor-grant")
    candidate_path = os.path.join(service.paths.outputs_dir, "manual-storyboard.json")
    with open(candidate_path, "w", encoding="utf-8") as handle:
        json.dump({"schema_version": "1", "manual": True}, handle)
    candidate = service.register_revision_candidate(
        logical_id="storyboard.output", path=os.path.relpath(candidate_path, service.paths.root),
        producer_stage="revision_candidate", expected_last_event_hash=service.get_status().last_event_hash,
    )
    candidate_version = _available(candidate["graph"], "storyboard.output")["version_id"]
    with pytest.raises(ProductionError) as error:
        service.preview_revision(changed=({"logical_id": "storyboard.output", "version_id": candidate_version},))
    assert error.value.code == "OPERATION_CONTRACT_INVALID"
    assert sum(provider.submit_counts.values()) == 1


def test_legacy_visual_authority_survives_restart_status_and_advance(tmp_path):
    service, _storyboard, provider = _service(tmp_path, visual_adapter_type=LegacyVisualStageAdapter)
    service.advance()
    _register_style(service)
    service.advance()
    service.advance()
    completed = _approve_and_complete(service, grant_id="legacy-grant")
    assert completed.status == "completed"

    restarted = ProductionService(
        str(service.paths.project_file), storyboard_adapter=service.storyboard_adapter,
        visual_adapter=VisualStageAdapter(provider_registry=VisualProviderRegistry({"sync-image": provider})),
        hmac_key_provider=MappingHmacKeyProvider({"test-key": KEY}),
    )
    restarted._configured_model = lambda: "fixture"
    assert restarted.get_status().status == "completed"
    assert restarted.advance().status == "completed"


def test_source_bootstrap_recovers_after_selection_append_interruption(tmp_path, monkeypatch):
    service, _storyboard, _provider = _service(tmp_path)
    original_append = service.store.events.append
    interrupted = False

    def interrupt_selection(event_type, **kwargs):
        nonlocal interrupted
        if event_type == "artifact_version_selected" and not interrupted:
            interrupted = True
            raise OSError("simulated selection append interruption")
        return original_append(event_type, **kwargs)

    monkeypatch.setattr(service.store.events, "append", interrupt_selection)
    with pytest.raises(OSError, match="selection append interruption"):
        service.advance()
    monkeypatch.setattr(service.store.events, "append", original_append)

    assert service.advance().status == "running"
    graph = service.get_artifact_graph()["graph"]
    source = _current(graph, "source.script")
    assert source["producer"] == {"stage": "project_source", "run_id": ""}
    registrations = [
        event for event in service.store.events.read()
        if event["event_type"] == "artifact_registered"
        and event["payload"]["artifact"]["logical_id"] == "source.script"
    ]
    assert len(registrations) == 1
