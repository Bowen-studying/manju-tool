"""Pure event reduction into a ProductionSnapshot."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from manju.production.models import (
    ProductionError,
    ProductionReason,
    ProductionSnapshot,
    ProductionStatus,
    ReasonCode,
)
from manju.production.approvals import ApprovalRequest, Grant, contractual_tariff_usage
from manju.production.operations import OperationRecord
from manju.production.models import M2_DAG_VERSION


KNOWN_EVENT_TYPES = {
    "project_initialized",
    "run_created",
    "run_started",
    "stage_scheduled",
    "stage_run_attached",
    "stage_completed",
    "stage_needs_review",
    "stage_failed",
    "pause_requested",
    "run_paused",
    "run_resumed",
    "run_completed",
    "execution_lease_acquired",
    "execution_lease_released",
    "execution_lease_recovered",
    "approval_requested",
    "approval_approved",
    "approval_rejected",
    "grant_issued",
    "grant_revoked",
    "call_reserved",
    "call_submitted",
    "call_settled",
    "call_reconciled",
    "manual_dispatch_prepared",
    "manual_result_imported",
    "manual_cost_reconciled",
    "manual_contractual_tariff_settled",
    "artifact_registered",
    "artifact_version_selected",
}


def _reduce_single_run(events: list[dict[str, Any]]) -> ProductionSnapshot:
    project_id = ""
    run_id = ""
    status = ProductionStatus.PENDING.value
    stage = ""
    reason_code = ReasonCode.PROJECT_READY.value
    reason_message = ""
    progress_completed = 0
    next_actions: tuple[dict[str, Any], ...] = ()
    updated_at = ""
    last_hash = ""
    initialized = False
    run_created = False
    run_started = False
    stage_scheduled = False
    stage_attached = False
    attached_stage_run_id = ""
    stage_outcome = ""
    visual_scheduled = False
    visual_attached = False
    visual_stage_run_id = ""
    visual_outcome = ""
    dag_version = "production-m1-v1"
    pause_requested = False
    execution_lease_id = ""
    pending_approval_id = ""
    pending_request: ApprovalRequest | None = None
    approval_decision = ""
    active_grant_id = ""
    active_grant: Grant | None = None
    operations: dict[str, OperationRecord] = {}
    budget_exceeded = False
    manual_dispatches: dict[str, dict[str, Any]] = {}
    manual_dispatch_hashes: dict[str, str] = {}
    manual_results: dict[str, dict[str, Any]] = {}
    manual_costs: set[str] = set()

    def valid_final_cost(operation: OperationRecord, grant: Grant | None) -> bool:
        """A missing cost is tolerated for pre-M2.3 event compatibility."""
        usage = operation.usage or {}
        status = usage.get("cost_status", "")
        if not status:
            return True
        if status == "unknown":
            return operation.outcome == "outcome_unknown" and usage.get("cost_source") == "unknown"
        return (
            status == "final" and isinstance(usage.get("actual_amount"), str)
            and usage["actual_amount"].isdigit() and usage.get("currency") == (grant.currency if grant else "")
            and usage.get("cost_source") in {"provider_response", "provider_ledger", "test_fixture", "contractual_tariff"}
        )

    def total_final_cost() -> int:
        total = 0
        for item in operations.values():
            usage = item.usage or {}
            if usage.get("cost_status") == "final":
                total += int(usage["actual_amount"])
        return total

    def is_expired(expires_at: str, occurred_at: str) -> bool:
        return datetime.fromisoformat(occurred_at.replace("Z", "+00:00")) > datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

    for event in events:
        event_type = str(event.get("event_type", ""))
        if event_type not in KNOWN_EVENT_TYPES:
            raise ProductionError(
                ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value,
                f"未知顶层事件类型: {event_type}",
            )
        event_project_id = str(event.get("project_id", ""))
        event_run_id = str(event.get("run_id", ""))
        if initialized and event_project_id != project_id:
            raise ProductionError(
                ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                f"事件 project_id 不一致: sequence {event.get('sequence')}",
            )
        if run_created and event_run_id != run_id:
            raise ProductionError(
                ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                f"事件 run_id 不一致: sequence {event.get('sequence')}",
            )
        if event_type == "project_initialized" and event_run_id:
            raise ProductionError(
                ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                "project_initialized 不得绑定 run_id",
            )
        project_id = event_project_id or project_id
        run_id = event_run_id or run_id
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        updated_at = str(event.get("occurred_at", updated_at))
        last_hash = str(event.get("event_hash", last_hash))

        valid = True
        if event_type == "project_initialized":
            valid = not initialized and not run_created
        elif event_type == "run_created":
            valid = initialized and bool(event_run_id) and not run_created and status == ProductionStatus.READY.value
        elif event_type == "run_started":
            valid = run_created and not run_started
        elif event_type == "stage_scheduled":
            target_stage = str(payload.get("stage", ""))
            valid = (
                run_started
                and status == ProductionStatus.RUNNING.value
                and bool(payload.get("stage_invocation_id"))
                and (
                    (target_stage == "storyboard" and not stage_scheduled)
                    or (target_stage == "visual" and dag_version == M2_DAG_VERSION and stage_outcome == "completed" and not visual_scheduled)
                )
            )
        elif event_type == "stage_run_attached":
            target_stage = str(payload.get("stage", ""))
            valid = (
                bool(payload.get("stage_run_id"))
                and (
                    (target_stage == "storyboard" and stage_scheduled and not stage_attached and not stage_outcome)
                    or (target_stage == "visual" and visual_scheduled and not visual_attached and not visual_outcome)
                )
            )
        elif event_type in {"stage_completed", "stage_needs_review", "stage_failed"}:
            target_stage = str(payload.get("stage", ""))
            valid = (
                (target_stage == "storyboard" and stage_attached and not stage_outcome and payload.get("stage_run_id") == attached_stage_run_id)
                or (target_stage == "visual" and dag_version == M2_DAG_VERSION and visual_attached and not visual_outcome and payload.get("stage_run_id") == visual_stage_run_id)
            )
        elif event_type == "pause_requested":
            valid = run_started and status == ProductionStatus.RUNNING.value and not pause_requested
        elif event_type == "run_paused":
            valid = pause_requested and status == ProductionStatus.PAUSED.value
        elif event_type == "run_resumed":
            valid = status == ProductionStatus.PAUSED.value
        elif event_type == "run_completed":
            valid = stage_outcome == "completed" and status == ProductionStatus.RUNNING.value and (
                dag_version != M2_DAG_VERSION or visual_outcome == "completed"
            )
        elif event_type == "execution_lease_acquired":
            valid = initialized and not execution_lease_id and bool(payload.get("lease_id"))
        elif event_type == "execution_lease_released":
            valid = bool(execution_lease_id) and payload.get("lease_id") == execution_lease_id
        elif event_type == "execution_lease_recovered":
            recovered_id = str(payload.get("recovered_lease_id", ""))
            valid = initialized and bool(recovered_id) and (
                not execution_lease_id or recovered_id == execution_lease_id
            )
        elif event_type in {"artifact_registered", "artifact_version_selected"}:
            # The ArtifactGraph validates the record, exact dependency closure,
            # and version transition. Top-level run state is intentionally unchanged.
            valid = initialized
        elif event_type == "approval_requested":
            request = ApprovalRequest.from_dict(payload.get("approval_request", {}))
            valid = (
                run_started and not pending_approval_id and not approval_decision
                and request.project_id == event_project_id and request.run_id == event_run_id
                and not is_expired(request.expires_at, str(event.get("occurred_at", "")))
            )
        elif event_type in {"approval_approved", "approval_rejected"}:
            valid = (
                status == ProductionStatus.AWAITING_APPROVAL.value
                and payload.get("request_id") == pending_approval_id
                and not approval_decision
                and pending_request is not None and not is_expired(pending_request.expires_at, str(event.get("occurred_at", "")))
            )
        elif event_type == "grant_issued":
            grant_data = payload.get("grant", {})
            grant = Grant.from_dict(grant_data)
            valid = (
                approval_decision == "approved" and not active_grant_id
                and grant.request_id == pending_approval_id
                and grant.project_id == event_project_id and grant.run_id == event_run_id
                and pending_request is not None and grant.matches_request(pending_request)
                and grant.status == "active" and not is_expired(grant.expires_at, str(event.get("occurred_at", "")))
            )
        elif event_type == "grant_revoked":
            valid = bool(active_grant_id) and payload.get("grant_id") == active_grant_id
        elif event_type == "call_reserved":
            operation = OperationRecord.from_dict(payload.get("operation", {}))
            permitted = {item["operation_id"]: item for item in (active_grant.operation_bindings if active_grant else ())}
            valid = (
                status != ProductionStatus.BLOCKED.value and operation.status == "reserved"
                and operation.grant_id == active_grant_id and operation.operation_id not in operations
                and operation.operation_id in permitted
                and operation.input_fingerprint == permitted.get(operation.operation_id, {}).get("input_fingerprint")
                and operation.kind == permitted.get(operation.operation_id, {}).get("kind")
                and operation.provider_request == (permitted.get(operation.operation_id, {}).get("provider_request") or None)
                and active_grant is not None and operation.provider_profile == active_grant.provider_profile
                and operation.currency == active_grant.currency
                and not is_expired(active_grant.expires_at, str(event.get("occurred_at", "")))
                and sum(int(item.reservation_amount) for item in operations.values())
                + int(operation.reservation_amount) <= int(active_grant.maximum_amount)
            )
        elif event_type == "call_submitted":
            operation = OperationRecord.from_dict(payload.get("operation", {}))
            previous = operations.get(operation.operation_id)
            valid = (
                (status != ProductionStatus.BLOCKED.value or operation.operation_id in manual_dispatches)
                and previous is not None and active_grant is not None
                and active_grant.status == "active" and not is_expired(active_grant.expires_at, str(event.get("occurred_at", "")))
                and previous.submit(operation.provider_job_id) == operation
            )
        elif event_type == "call_settled":
            operation = OperationRecord.from_dict(payload.get("operation", {}))
            previous = operations.get(operation.operation_id)
            valid = (
                (status != ProductionStatus.BLOCKED.value or operation.operation_id in manual_dispatches)
                and previous is not None and active_grant is not None
                and active_grant.status == "active" and not is_expired(active_grant.expires_at, str(event.get("occurred_at", "")))
                and previous.settle(outcome=operation.outcome, result_fingerprint=operation.result_fingerprint,
                                    usage=operation.usage) == operation
                and valid_final_cost(operation, active_grant)
            )
        elif event_type == "call_reconciled":
            operation = OperationRecord.from_dict(payload.get("operation", {}))
            previous = operations.get(operation.operation_id)
            usage = operation.usage or {}
            tariff = active_grant.contractual_tariff if active_grant is not None else None
            valid = (
                status == ProductionStatus.BLOCKED.value and previous is not None
                and previous.reconcile(outcome=operation.outcome, result_fingerprint=operation.result_fingerprint,
                                       usage=operation.usage) == operation
                and valid_final_cost(operation, active_grant)
                and (
                    (active_grant is not None and active_grant.settlement_mode == "provider_evidence"
                     and (
                         not usage.get("cost_status")  # Pre-M2.3 provider-evidence event compatibility.
                         or usage.get("cost_source") in {"provider_response", "provider_ledger", "test_fixture"}
                     ))
                    or (
                        operation.operation_id in manual_costs and active_grant is not None
                        and active_grant.settlement_mode == "contractual_tariff" and isinstance(tariff, dict)
                        and usage == contractual_tariff_usage(tariff, operation.outcome)
                    )
                )
            )
        elif event_type == "manual_dispatch_prepared":
            from manju.production.manual_operations import ManualDispatchPackage
            dispatch = ManualDispatchPackage.from_dict(payload.get("dispatch"))
            permitted = {item["operation_id"]: item for item in (active_grant.operation_bindings if active_grant else ())}
            valid = (
                status == ProductionStatus.RUNNING.value and dispatch.operation_id not in manual_dispatches
                and dispatch.operation_id in operations and operations[dispatch.operation_id].status == "reserved"
                and active_grant is not None and dispatch.grant_id == active_grant_id
                and dispatch.project_id == event_project_id and dispatch.run_id == event_run_id
                and dispatch.stage_run_id == active_grant.stage_run_id
                and dispatch.provider_profile == active_grant.provider_profile
                and dispatch.operation_kind == permitted.get(dispatch.operation_id, {}).get("kind")
                and dispatch.input_fingerprint == permitted.get(dispatch.operation_id, {}).get("input_fingerprint")
                and dispatch.provider_request == (permitted.get(dispatch.operation_id, {}).get("provider_request") or {})
                and dispatch.maximum_amount == active_grant.maximum_amount and dispatch.currency == active_grant.currency
                and dispatch.sha256() == payload.get("dispatch_sha256")
                and bool(dispatch.signature)
            )
        elif event_type == "manual_result_imported":
            from manju.production.manual_operations import ManualResultPackage
            result = ManualResultPackage.from_dict(payload.get("result"))
            dispatch = manual_dispatches.get(result.operation_id)
            operation = operations.get(result.operation_id)
            valid = (
                dispatch is not None and result.operation_id not in manual_results
                and result.package_id == dispatch["package_id"] and result.claim_token == dispatch["claim_token"]
                and result.dispatch_sha256 == payload.get("dispatch_sha256") and bool(result.signature)
                and operation is not None and operation.status == "settled" and operation.outcome == "outcome_unknown"
            )
        elif event_type == "manual_cost_reconciled":
            from manju.production.manual_operations import ManualBillingEvidence
            evidence = ManualBillingEvidence.from_dict(payload.get("evidence"))
            dispatch = manual_dispatches.get(evidence.operation_id)
            result = manual_results.get(evidence.operation_id)
            valid = (
                dispatch is not None and result is not None and evidence.operation_id not in manual_costs
                and evidence.claim_token == dispatch["claim_token"] and evidence.dispatch_sha256 == payload.get("dispatch_sha256")
                and evidence.outcome == result["outcome"] and evidence.outcome in {"succeeded", "failed"}
                and active_grant is not None and active_grant.settlement_mode == "provider_evidence"
                and bool(evidence.signature)
            )
        elif event_type == "manual_contractual_tariff_settled":
            operation_id = payload.get("operation_id")
            dispatch = manual_dispatches.get(operation_id)
            result = manual_results.get(operation_id)
            operation = operations.get(operation_id)
            tariff = payload.get("tariff")
            valid = (
                isinstance(operation_id, str) and dispatch is not None and result is not None
                and operation_id not in manual_costs and operation is not None
                and operation.status == "settled" and operation.outcome == "outcome_unknown"
                and payload.get("dispatch_sha256") == manual_dispatch_hashes.get(operation_id) and payload.get("outcome") == result.get("outcome")
                and active_grant is not None and active_grant.settlement_mode == "contractual_tariff"
                and tariff == active_grant.contractual_tariff and isinstance(tariff, dict)
            )
        if not valid:
            raise ProductionError(
                ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                f"非法事件顺序: {event_type} at sequence {event.get('sequence')}",
            )

        if event_type == "project_initialized":
            initialized = True
            status = ProductionStatus.READY.value
            reason_code = ReasonCode.PROJECT_READY.value
        elif event_type == "run_created":
            run_created = True
            dag_version = str(payload.get("dag_version") or "production-m1-v1")
            if dag_version not in {"production-m1-v1", M2_DAG_VERSION}:
                raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, f"unsupported dag: {dag_version}")
            status = ProductionStatus.RUNNING.value
            reason_code = ReasonCode.PROJECT_READY.value
        elif event_type == "run_started":
            run_started = True
            status = ProductionStatus.RUNNING.value
            reason_code = ReasonCode.PROJECT_READY.value
        elif event_type in {"stage_scheduled", "stage_run_attached"}:
            target_stage = str(payload.get("stage", ""))
            if target_stage not in {"storyboard", "visual"} or (target_stage == "visual" and dag_version != M2_DAG_VERSION):
                raise ProductionError(
                    ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value,
                    f"不支持阶段: {target_stage!r}",
                )
            if event_type == "stage_scheduled":
                if target_stage == "storyboard":
                    stage_scheduled = True
                else:
                    visual_scheduled = True
            else:
                if target_stage == "storyboard":
                    stage_attached = True
                    attached_stage_run_id = str(payload.get("stage_run_id", ""))
                else:
                    visual_attached = True
                    visual_stage_run_id = str(payload.get("stage_run_id", ""))
            status = (
                ProductionStatus.PAUSED.value
                if pause_requested else ProductionStatus.RUNNING.value
            )
            stage = target_stage
            reason_code = (
                ReasonCode.PROJECT_PAUSED.value
                if pause_requested
                else ReasonCode.STORYBOARD_RUNNING.value if stage == "storyboard"
                else ReasonCode.PROJECT_READY.value
            )
            next_actions = ()
        elif event_type == "stage_completed":
            if payload.get("stage") == "storyboard":
                stage_outcome = "completed"
            else:
                visual_outcome = "completed"
            status = (
                ProductionStatus.PAUSED.value
                if pause_requested else ProductionStatus.RUNNING.value
            )
            stage = str(payload.get("stage", stage))
            progress_completed = max(progress_completed, 1 if payload.get("stage") == "storyboard" else 2)
            reason_code = (
                ReasonCode.PROJECT_PAUSED.value
                if pause_requested else ReasonCode.PROJECT_READY.value
            )
        elif event_type == "stage_needs_review":
            if payload.get("stage") == "storyboard":
                stage_outcome = "needs_review"
            else:
                visual_outcome = "needs_review"
            status = ProductionStatus.NEEDS_REVIEW.value
            stage = str(payload.get("stage", stage))
            reason_code = str(payload.get("reason_code") or ReasonCode.STORYBOARD_REVIEW_REQUIRED.value)
            reason_message = str(payload.get("message", ""))
            next_actions = tuple(payload.get("next_actions") or ())
        elif event_type == "stage_failed":
            if payload.get("stage") == "storyboard":
                stage_outcome = "failed"
            else:
                visual_outcome = "failed"
            status = ProductionStatus.FAILED.value
            stage = str(payload.get("stage", stage))
            reason_code = str(payload.get("reason_code") or ReasonCode.STORYBOARD_FAILED.value)
            reason_message = str(payload.get("message", ""))
            next_actions = tuple(payload.get("next_actions") or ())
        elif event_type in {"pause_requested", "run_paused"}:
            if event_type == "pause_requested":
                pause_requested = True
            status = ProductionStatus.PAUSED.value
            reason_code = ReasonCode.PROJECT_PAUSED.value
            next_actions = ({"action": "run"},)
        elif event_type == "run_resumed":
            pause_requested = False
            status = ProductionStatus.RUNNING.value
            reason_code = ReasonCode.STORYBOARD_RUNNING.value
            next_actions = ()
        elif event_type == "run_completed":
            status = ProductionStatus.COMPLETED.value
            progress_completed = 2 if dag_version == M2_DAG_VERSION else 1
            reason_code = ReasonCode.PROJECT_ALREADY_COMPLETED.value
            next_actions = ()
        elif event_type == "execution_lease_acquired":
            execution_lease_id = str(payload["lease_id"])
        elif event_type == "execution_lease_released":
            execution_lease_id = ""
        elif event_type == "execution_lease_recovered":
            execution_lease_id = ""
        elif event_type == "approval_requested":
            request = ApprovalRequest.from_dict(payload["approval_request"])
            pending_approval_id = request.request_id
            pending_request = request
            stage = request.stage
            status = ProductionStatus.AWAITING_APPROVAL.value
            reason_code = ReasonCode.PAID_VISUAL_BATCH_APPROVAL_REQUIRED.value
            next_actions = (
                {"action": "approve", "request_id": request.request_id},
                {"action": "reject", "request_id": request.request_id},
            )
        elif event_type == "approval_approved":
            approval_decision = "approved"
            reason_code = ReasonCode.PAID_VISUAL_BATCH_APPROVAL_REQUIRED.value
            next_actions = ({"action": "issue_grant", "request_id": pending_approval_id},)
        elif event_type == "approval_rejected":
            approval_decision = "rejected"
            status = ProductionStatus.NEEDS_REVIEW.value
            reason_code = ReasonCode.PAID_VISUAL_BATCH_REJECTED.value
            next_actions = ()
        elif event_type == "grant_issued":
            active_grant = Grant.from_dict(payload["grant"])
            active_grant_id = active_grant.grant_id
            status = ProductionStatus.RUNNING.value
            reason_code = ReasonCode.PROJECT_READY.value
            next_actions = ()
        elif event_type == "grant_revoked":
            active_grant_id = ""
            status = ProductionStatus.NEEDS_REVIEW.value
            reason_code = ReasonCode.PAID_VISUAL_BATCH_REJECTED.value
            next_actions = ()
        elif event_type == "manual_dispatch_prepared":
            dispatch = payload["dispatch"]
            manual_dispatches[dispatch["operation_id"]] = dispatch
            manual_dispatch_hashes[dispatch["operation_id"]] = payload["dispatch_sha256"]
            status = ProductionStatus.BLOCKED.value
            reason_code = ReasonCode.OPERATION_OUTCOME_UNKNOWN.value
            next_actions = ({"action": "import_manual_result", "operation_id": dispatch["operation_id"]},)
        elif event_type == "manual_result_imported":
            result = payload["result"]
            manual_results[result["operation_id"]] = result
        elif event_type == "manual_cost_reconciled":
            manual_costs.add(payload["evidence"]["operation_id"])
        elif event_type == "manual_contractual_tariff_settled":
            manual_costs.add(payload["operation_id"])
        elif event_type in {"call_reserved", "call_submitted", "call_settled", "call_reconciled"}:
            operation = OperationRecord.from_dict(payload["operation"])
            operations[operation.operation_id] = operation
            if operation.outcome == "outcome_unknown":
                status = ProductionStatus.BLOCKED.value
                reason_code = ReasonCode.OPERATION_OUTCOME_UNKNOWN.value
                next_actions = ({"action": "reconcile_operation", "operation_id": operation.operation_id},)
            elif (operation.usage or {}).get("cost_status") == "final" and active_grant is not None and (
                int(operation.usage["actual_amount"]) > int(operation.reservation_amount)
                or total_final_cost() > int(active_grant.maximum_amount)
            ):
                # The signed settlement remains in the event chain: blocking is
                # deliberate so an overspend cannot be hidden by a retry.
                budget_exceeded = True
                status = ProductionStatus.BLOCKED.value
                reason_code = ReasonCode.BUDGET_EXCEEDED.value
                next_actions = ({"action": "review_budget", "operation_id": operation.operation_id},)
            elif event_type == "call_reconciled" and not any(
                item.outcome == "outcome_unknown" for item in operations.values()
            ):
                if not budget_exceeded:
                    status = ProductionStatus.RUNNING.value
                    reason_code = ReasonCode.PROJECT_READY.value
                    next_actions = ()

    return ProductionSnapshot(
        project_id=project_id,
        run_id=run_id,
        status=status,
        current_stage=stage,
        reason=ProductionReason(reason_code, reason_message),
        progress_completed=progress_completed,
        progress_total=2 if dag_version == M2_DAG_VERSION else 1,
        next_actions=next_actions,
        updated_at=updated_at,
        last_event_hash=last_hash,
    )


def reduce_events(events: list[dict[str, Any]]) -> ProductionSnapshot:
    """Reduce the active run while preserving strict validation within each run."""
    if not events:
        return _reduce_single_run(events)
    from manju.production.revisions import RevisionProjection

    revisions = RevisionProjection.from_events(events)
    active_run_id = revisions.active_run_id
    if not active_run_id:
        # Before the first valid run exists, every event must remain visible to
        # the original strict state machine; never discard malformed run events.
        return _reduce_single_run(events)
    initialized = next((event for event in events if event.get("event_type") == "project_initialized"), None)
    if initialized is None:
        raise ProductionError(ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value, "project_initialized is missing")
    active_run_created_at, active_run_created = next(
        (index, event) for index, event in enumerate(events)
        if event.get("event_type") == "run_created" and event.get("run_id") == active_run_id
    )
    is_successor = bool((active_run_created.get("payload") or {}).get("predecessor_run_id"))
    scoped = [initialized]
    scoped.extend(
        event for index, event in enumerate(events)
        if event.get("run_id") == active_run_id
        or (
            not event.get("run_id")
            and event.get("event_type") != "project_initialized"
            # The first execution lease is necessarily acquired before the
            # original run ID exists, but released under that original run.
            # It is historical once a successor is active and must not occupy
            # the successor's independent execution-lease slot.
            and not (
                is_successor
                and index < active_run_created_at
                and event.get("event_type") in {"execution_lease_acquired", "execution_lease_recovered"}
            )
        )
    )
    snapshot = _reduce_single_run(scoped)
    latest = events[-1]
    return replace(snapshot, updated_at=str(latest.get("occurred_at", snapshot.updated_at)),
                   last_event_hash=str(latest.get("event_hash", snapshot.last_event_hash)))
