"""M3.2 offline closure: a successor run must re-authorize its one paid call."""

from __future__ import annotations

import os

import pytest

from manju.production.models import ProductionError
from tests.test_production_m2_7_contractual_tariff import _import_success, _service


def _register_select(service, *, logical_id: str, name: str, content: bytes) -> str:
    path = os.path.join(service.paths.outputs_dir, name)
    with open(path, "wb") as handle:
        handle.write(content)
    registered = service.register_artifact(
        logical_id=logical_id,
        path=os.path.relpath(path, service.paths.root),
        producer_stage="m3_2_fixture",
        expected_last_event_hash=service.get_artifact_graph()["last_event_hash"],
    )
    version = next(
        item["version_id"]
        for item in registered["graph"]["artifacts"]
        if item["logical_id"] == logical_id and item["state"] == "available"
    )
    service.select_artifact_version(
        logical_id=logical_id,
        version_id=version,
        expected_last_event_hash=registered["last_event_hash"],
    )
    return version


def test_completed_predecessor_revision_requires_new_grant_and_settles_one_successor_operation(tmp_path, monkeypatch):
    service, predecessor_grant, _project = _service(tmp_path, monkeypatch)

    # Complete the predecessor through the fixture worker. Its paid authority
    # is historical before any successor is created.
    _prepared, predecessor_dispatch, imported = _import_success(service, predecessor_grant, tmp_path)
    service.settle_manual_contractual_tariff(
        operation_id=predecessor_dispatch.operation_id,
        expected_last_event_hash=imported.last_event_hash,
    )
    predecessor = service.run_until_blocked()
    assert predecessor.status == "completed"

    _register_select(service, logical_id="source.script", name="source-v1.txt", content=b"v1")
    style_v1 = _register_select(service, logical_id="style.reference", name="style-v1.txt", content=b"style")
    source_v2 = _register_select(service, logical_id="source.script", name="source-v2.txt", content=b"v2")

    preview = service.preview_revision(changed=({"logical_id": "source.script", "version_id": source_v2},))
    assert preview["run_id"] == predecessor.run_id
    assert preview["reused_artifacts"] == [{"logical_id": "style.reference", "version_id": style_v1}]

    successor_data = service.create_revision(
        changed=({"logical_id": "source.script", "version_id": source_v2},),
        requested_by="m3.2-fixture",
        reason="replace only the source artifact",
        preview_fingerprint=preview["preview_fingerprint"],
        expected_last_event_hash=preview["last_event_hash"],
        revision_id="m3_2_source_v2",
    )
    successor_run_id = successor_data["run_id"]
    successor = service.get_status()
    assert successor.run_id == successor_run_id and successor.status == "running"

    service.advance()
    service.advance()
    awaiting = service.advance()
    assert awaiting.run_id == successor_run_id and awaiting.status == "awaiting_approval"
    successor_request_id = "approval-" + successor_run_id.removeprefix("run_")

    # The successor is now in the only status from which a Grant can be issued.
    # Therefore this rejection proves predecessor and successor authority are
    # run-bound, rather than merely failing because of the earlier run state.
    with pytest.raises(ProductionError):
        service.issue_grant(
            "approval-" + predecessor.run_id.removeprefix("run_"),
            grant_id="forbidden-predecessor-grant",
            issued_by="m3.2-fixture",
            expected_last_event_hash=awaiting.last_event_hash,
        )

    approved = service.decide_approval(
        successor_request_id,
        decision="approve",
        reviewer="m3.2-reviewer",
        expected_last_event_hash=awaiting.last_event_hash,
    )
    successor_grant = service.issue_grant(
        successor_request_id,
        grant_id="m3_2_successor_grant",
        issued_by="m3.2-issuer",
        expected_last_event_hash=approved.last_event_hash,
    )
    prepared, successor_dispatch, successor_imported = _import_success(service, successor_grant, tmp_path / "successor")
    assert successor_dispatch.run_id == successor_run_id
    assert successor_dispatch.grant_id == "m3_2_successor_grant"
    assert successor_dispatch.operation_id != predecessor_dispatch.operation_id

    settled = service.settle_manual_contractual_tariff(
        operation_id=successor_dispatch.operation_id,
        expected_last_event_hash=successor_imported.last_event_hash,
    )
    completed = service.run_until_blocked()
    assert settled.run_id == successor_run_id and completed.status == "completed"

    events = service.store.events.read()
    successor_grants = [
        event["payload"]["grant"] for event in events
        if event.get("event_type") == "grant_issued" and event.get("run_id") == successor_run_id
    ]
    successor_submissions = [
        event for event in events
        if event.get("event_type") == "call_submitted" and event.get("run_id") == successor_run_id
    ]
    successor_settlements = [
        event["payload"]["operation"] for event in events
        if event.get("event_type") == "call_reconciled" and event.get("run_id") == successor_run_id
    ]
    assert [grant["grant_id"] for grant in successor_grants] == ["m3_2_successor_grant"]
    assert len(successor_submissions) == 1
    assert len(successor_settlements) == 1
    assert successor_settlements[0]["usage"]["cost_disclosure"] == "pre_agreed_price_not_upstream_actual_cost"
