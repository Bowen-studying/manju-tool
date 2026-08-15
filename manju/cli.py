"""manju-tool CLI — AI 漫剧制作：两种剧本入口 → 分镜 → 配音 → 视频。"""

import json
import os
import sys
from datetime import datetime

import click

from manju.pipeline.adapt import run_adapt
from manju.pipeline.create import run_create
from manju.pipeline.storyboard import run_storyboard
from manju.pipeline.video import run_video
from manju.pipeline.voice import run_voice
from manju.pipeline.generate_video import run_generate
from manju.pipeline.generate_image import (
    count_batch_lines as count_image_batch_lines,
    run_image, run_batch_from_file,
)
from manju.pipeline.generate_voice import (
    count_batch_lines as count_voice_batch_lines,
    run_speak, run_batch_speak, run_batch_speak_file,
)
from manju.utils.use_guide import write_use_guide
from manju.utils.runtime import atomic_write_json, safe_filename

OUTPUT_BASE = os.path.join(os.getcwd(), "manju-output")


def _parse_agent_max_calls(ctx, param, value):
    if value is None or str(value).strip().lower() == "auto":
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise click.BadParameter("must be 'auto' or a positive integer") from exc
    if parsed < 1:
        raise click.BadParameter("must be 'auto' or a positive integer")
    return parsed


@click.group()
def cli():
    """manju-tool: AI 漫剧制作工具 — 从剧本到AI短视频素材。

    两种入口：\n
      manju adapt <小说.txt>  — 小说→剧本\n
      manju create              — AI创作剧本\n
    然后接：storyboard → voice → video → pipeline
    \n
    直接生视频：\n
      manju generate <描述>    — 文字/图片→AI视频"""


def _echo_production_payload(value, *, json_output=False):
    payload = value.to_dict() if hasattr(value, "to_dict") else value
    if json_output:
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    reason = payload.get("reason", {}) if isinstance(payload, dict) else {}
    click.echo(f"项目: {payload.get('project_id', '')}")
    if payload.get("run_id"):
        click.echo(f"运行: {payload['run_id']}")
    click.echo(f"状态: {payload.get('status', 'unknown')}")
    if payload.get("current_stage"):
        click.echo(f"阶段: {payload['current_stage']}")
    if reason:
        click.echo(f"原因: {reason.get('code', '')} - {reason.get('message', '')}")


def _handle_production_error(exc, *, json_output=False):
    if json_output:
        click.echo(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), err=True)
    else:
        click.echo(f"ProductionRun 失败 [{exc.code}]: {exc.message}", err=True)
    raise click.exceptions.Exit(exc.exit_code)


def _production_service(project_json, *, assemble_visual_provider=True):
    """Resolve an ephemeral operator key; it is intentionally never written to a project."""
    from manju.production import ProductionService
    from manju.production.adapters.visual import VisualStageAdapter
    from manju.production.runtime_profiles import is_manual_sync_profile, resolve_visual_provider_registry
    from manju.production.security import MappingHmacKeyProvider

    key = os.environ.get("MANJU_PRODUCTION_HMAC_KEY", "")
    with open(project_json, "r", encoding="utf-8") as handle:
        project = json.load(handle)
    key_id = project.get("integrity", {}).get("hmac_key_id", "")
    visual = project.get("production", {}).get("visual", {})
    profile = visual.get("provider_profile", "") if visual.get("enabled") else ""
    adapter = None
    # A manual_sync profile intentionally has no automatic provider instance.
    # This still lets the deterministic local stages reach approval/grant; the
    # later reserved call can only be advanced by prepare-manual + worker.
    if assemble_visual_provider and profile and profile != "mock" and not is_manual_sync_profile(profile):
        adapter = VisualStageAdapter(provider_registry=resolve_visual_provider_registry(required_profile=profile))
    hmac_provider = MappingHmacKeyProvider({key_id: key.encode("utf-8")}) if key else None
    return ProductionService(project_json, visual_adapter=adapter, hmac_key_provider=hmac_provider)


@cli.group()
def project():
    """创建和导入可恢复的 ProductionRun 项目。"""


@project.command("init")
@click.option("--source", type=click.Path(exists=True, dir_okay=False), required=True,
              help="小说、剧本或分镜源文件")
@click.option("--source-type", type=click.Choice(["novel", "script", "storyboard"]), required=True)
@click.option("-o", "--output-dir", type=click.Path(file_okay=False), required=True,
              help="新项目目录")
@click.option("--engine", type=click.Choice(["legacy", "workflow", "agent"]), default="agent",
              show_default=True, help="分镜引擎")
@click.option("--max-scenes", type=click.IntRange(1, 8), default=None)
@click.option("--agent-max-steps", type=click.IntRange(min=1), default=40, show_default=True)
@click.option("--agent-max-calls", type=str, default="auto", callback=_parse_agent_max_calls,
              show_default=True)
@click.option("--agent-max-revisions", type=click.IntRange(min=0), default=2, show_default=True)
@click.option("--provider-profile", default="default", show_default=True)
@click.option("--hmac-key-id", default="manju-local-default", show_default=True)
@click.option("--visual-mock", is_flag=True, help="启用仅离线的 mock 视觉阶段")
@click.option("--visual-provider-profile", default="", help="部署侧已配置的异步视觉 Provider profile")
@click.option("--visual-request-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="仅含已审批公开生图字段的 JSON 文件")
@click.option("--visual-operation-kind", default="image_generation", show_default=True)
@click.option("--visual-max-calls", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--visual-max-amount", default="0", show_default=True)
@click.option("--visual-settlement-mode", type=click.Choice(["provider_evidence", "contractual_tariff"]), default="provider_evidence", show_default=True)
@click.option("--visual-contractual-tariff-id", default="", help="Pre-agreed tariff identifier; never an upstream invoice")
@click.option("--visual-contractual-tariff-amount", default="", help="Pre-agreed amount in project minor currency units")
@click.option("--json", "json_output", is_flag=True, help="输出稳定 JSON DTO")
def project_init(source, source_type, output_dir, engine, max_scenes, agent_max_steps,
                  agent_max_calls, agent_max_revisions, provider_profile, hmac_key_id,
                  visual_mock, visual_provider_profile, visual_request_file, visual_operation_kind,
                  visual_max_calls, visual_max_amount, visual_settlement_mode,
                  visual_contractual_tariff_id, visual_contractual_tariff_amount, json_output):
    """创建项目合同并接收一个持久化源文件。"""
    from manju.production import ProductionError, initialize_project

    try:
        if visual_mock and visual_provider_profile:
            raise ProductionError("OPERATION_CONTRACT_INVALID", "visual mock 与真实 Provider profile 不能同时启用")
        visual_request = None
        if visual_request_file:
            with open(visual_request_file, "r", encoding="utf-8") as handle:
                visual_request = json.load(handle)
        snapshot = initialize_project(
            source=source,
            source_type=source_type,
            output_dir=output_dir,
            engine=engine,
            max_scenes=max_scenes,
            max_steps=agent_max_steps,
            max_calls=agent_max_calls,
            max_revisions=agent_max_revisions,
            provider_profile=provider_profile,
            hmac_key_id=hmac_key_id,
            visual_enabled=visual_mock or bool(visual_provider_profile),
            visual_maximum_paid_calls=visual_max_calls,
            visual_maximum_amount=visual_max_amount,
            visual_provider_profile=visual_provider_profile or "mock",
            visual_provider_request=visual_request,
            visual_operation_kind="mock_image" if visual_mock else visual_operation_kind,
            visual_settlement_mode=visual_settlement_mode,
            visual_contractual_tariff_id=visual_contractual_tariff_id,
            visual_contractual_tariff_amount=visual_contractual_tariff_amount,
        )
        _echo_production_payload(snapshot, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("run")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True, help="输出稳定 JSON DTO")
def production_run(project_json, json_output):
    """幂等推进项目，直到完成或遇到人工/外部阻塞。"""
    from manju.production import ProductionError, ProductionService

    try:
        snapshot = _production_service(project_json).run_until_blocked()
        _echo_production_payload(snapshot, json_output=json_output)
        if snapshot.exit_code:
            raise click.exceptions.Exit(snapshot.exit_code)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("status")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True, help="输出稳定 JSON DTO")
def production_status(project_json, json_output):
    """读取并验证当前项目状态，不推进阶段。"""
    from manju.production import ProductionError, ProductionService

    try:
        snapshot = _production_service(project_json, assemble_visual_provider=False).get_status()                                            
        _echo_production_payload(snapshot, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("pause")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True, help="输出稳定 JSON DTO")
def production_pause(project_json, json_output):
    """在节点边界暂停活动运行。"""
    from manju.production import ProductionError, ProductionService

    try:
        snapshot = _production_service(project_json).request_pause()
        _echo_production_payload(snapshot, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("doctor")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True, help="输出稳定 JSON 诊断结果")
def production_doctor(project_json, json_output):
    """校验项目、源文件、事件链、合同和已有分镜子 run。"""
    from manju.production import ProductionService

    report = _production_service(project_json, assemble_visual_provider=False).doctor()
    if json_output:
        click.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        click.echo(f"诊断状态: {report.get('status', 'failed')}")
        for check in report.get("checks", []):
            click.echo(f"- {check.get('name')}: {check.get('status')}")
    if report.get("status") != "passed":
        raise click.exceptions.Exit(1)


@cli.command("approvals")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True, help="输出稳定 JSON DTO")
def production_approvals(project_json, json_output):
    """列出顶层、签名的视觉付费审批记录。"""
    from manju.production import ProductionError
    try:
        value = {"schema_version": "1", "approvals": _production_service(project_json).list_approvals()}
        if json_output:
            click.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
        else:
            click.echo(f"审批数: {len(value['approvals'])}")
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


def _decision_command(project_json, request_id, reviewer, expected_last_event_hash, decision, json_output):
    from manju.production import ProductionError
    try:
        snapshot = _production_service(project_json).decide_approval(
            request_id, decision=decision, reviewer=reviewer,
            expected_last_event_hash=expected_last_event_hash,
        )
        _echo_production_payload(snapshot, json_output=json_output)
        if snapshot.exit_code:
            raise click.exceptions.Exit(snapshot.exit_code)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("approve")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.argument("request_id")
@click.option("--reviewer", required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_approve(project_json, request_id, reviewer, expected_last_event_hash, json_output):
    """签名记录人工同意；仍须显式签发 grant。"""
    _decision_command(project_json, request_id, reviewer, expected_last_event_hash, "approve", json_output)


@cli.command("reject")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.argument("request_id")
@click.option("--reviewer", required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_reject(project_json, request_id, reviewer, expected_last_event_hash, json_output):
    """签名记录人工拒绝。"""
    _decision_command(project_json, request_id, reviewer, expected_last_event_hash, "reject", json_output)


@cli.command("issue-grant")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.argument("request_id")
@click.option("--grant-id", required=True)
@click.option("--issued-by", required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_issue_grant(project_json, request_id, grant_id, issued_by, expected_last_event_hash, json_output):
    """生成签名 grant；M2.0 不会据此调用 Provider。"""
    from manju.production import ProductionError
    try:
        snapshot = _production_service(project_json).issue_grant(
            request_id, grant_id=grant_id, issued_by=issued_by,
            expected_last_event_hash=expected_last_event_hash,
        )
        _echo_production_payload(snapshot, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("prepare-manual")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_prepare_manual(project_json, expected_last_event_hash, json_output):
    """Create a signed manual_sync dispatch package. It never calls a Provider."""
    from manju.production import ProductionError
    try:
        value = _production_service(project_json, assemble_visual_provider=False).prepare_manual_dispatch(
            expected_last_event_hash=expected_last_event_hash
        )
        _echo_production_payload(value, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("import-manual-result")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--result-file", type=click.Path(exists=True, dir_okay=False), required=True)
@click.option("--package-dir", type=click.Path(exists=True, file_okay=False), required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_import_manual_result(project_json, result_file, package_dir, expected_last_event_hash, json_output):
    """Import a signed manual worker result; cost remains blocked until reconciliation."""
    from manju.production import ProductionError
    from manju.production.manual_operations import ManualResultPackage
    try:
        with open(result_file, "r", encoding="utf-8") as handle:
            result = ManualResultPackage.from_dict(json.load(handle))
        snapshot = _production_service(project_json, assemble_visual_provider=False).import_manual_result(
            result, package_dir=package_dir, expected_last_event_hash=expected_last_event_hash
        )
        _echo_production_payload(snapshot, json_output=json_output)
        if snapshot.exit_code:
            raise click.exceptions.Exit(snapshot.exit_code)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("reconcile-manual")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--operation-id", required=True)
@click.option("--actual-amount", required=True)
@click.option("--currency", required=True)
@click.option("--provider-reference", required=True)
@click.option("--reviewer", required=True)
@click.option("--evidence-file", type=click.Path(exists=True, dir_okay=False), required=True)
@click.option("--package-dir", type=click.Path(exists=True, file_okay=False), required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_reconcile_manual(project_json, operation_id, actual_amount, currency, provider_reference, reviewer, evidence_file, package_dir, expected_last_event_hash, json_output):
    """Sign and reconcile human-reviewed billing evidence; fixed nominal price is not accepted."""
    from manju.production import ProductionError
    from manju.production.manual_operations import ManualBillingEvidence, sha256_file
    from manju.production.models import utc_now
    try:
        service = _production_service(project_json, assemble_visual_provider=False)
        project = service.store.load_project()
        snapshot = service.store.snapshot()
        found = service._manual_dispatch(service.store.events.read(), snapshot.run_id, operation_id)
        if found is None:
            raise ProductionError("OPERATION_CONTRACT_INVALID", "manual dispatch is unavailable")
        dispatch, digest = found
        key_id, key = service._manual_key(project)
        evidence_path = ""
        evidence_sha256 = ""
        evidence_path = os.path.basename(evidence_file)
        evidence_sha256 = sha256_file(evidence_file)
        result_event = next((event for event in service.store.events.read() if event.get("run_id") == snapshot.run_id and event.get("event_type") == "manual_result_imported"), None)
        outcome = ((result_event or {}).get("payload") or {}).get("result", {}).get("outcome", "")
        evidence = ManualBillingEvidence(digest, operation_id, dispatch.claim_token, outcome, actual_amount, currency,
                                         provider_reference, evidence_path, evidence_sha256, reviewer, utc_now(), key_id).sign(key)
        value = service.reconcile_manual_cost(evidence, package_dir=package_dir, expected_last_event_hash=expected_last_event_hash)
        _echo_production_payload(value, json_output=json_output)
        if value.exit_code:
            raise click.exceptions.Exit(value.exit_code)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.command("settle-manual-contractual-tariff")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--operation-id", required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_settle_manual_contractual_tariff(project_json, operation_id, expected_last_event_hash, json_output):
    """Settle at the Grant's pre-agreed tariff, not actual upstream cost."""
    from manju.production import ProductionError
    try:
        snapshot = _production_service(project_json, assemble_visual_provider=False).settle_manual_contractual_tariff(
            operation_id=operation_id, expected_last_event_hash=expected_last_event_hash
        )
        _echo_production_payload(snapshot, json_output=json_output)
        if snapshot.exit_code:
            raise click.exceptions.Exit(snapshot.exit_code)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.group("audit")
def production_audit():
    """Export and verify credential-free ProductionRun evidence snapshots."""


@production_audit.command("export")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--destination", type=click.Path(file_okay=False), required=True)
@click.option("--worker-result-dir", type=click.Path(exists=True, file_okay=False), default=None)
@click.option("--worker-state-dir", type=click.Path(exists=True, file_okay=False), default=None)
@click.option("--json", "json_output", is_flag=True)
def production_audit_export(project_json, destination, worker_result_dir, worker_state_dir, json_output):
    """Export an evidence snapshot. HMAC keys and Provider credentials are excluded."""
    from manju.production import ProductionError
    from manju.production.audit import export_audit_snapshot
    try:
        service = _production_service(project_json, assemble_visual_provider=False)
        project = service.store.load_project()
        service.store.validate_source(project)
        service.store.events.read()
        snapshot = service.store.snapshot()
        if snapshot.run_id:
            service.store.validate_contract(project, snapshot.run_id)
        value = export_audit_snapshot(project_json=project_json, destination=destination,
                                      worker_result_dir=worker_result_dir or "", worker_state_dir=worker_state_dir or "",
                                      key_provider=service.store.events.key_provider)
        _echo_production_payload(value, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@production_audit.command("verify")
@click.argument("snapshot_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--verify-hmac", is_flag=True, help="Require the external operator HMAC key and verify signed events")
@click.option("--json", "json_output", is_flag=True)
def production_audit_verify(snapshot_dir, verify_hmac, json_output):
    """Verify snapshot hashes; HMAC verification always requires the external key."""
    from manju.production import ProductionError
    from manju.production.audit import verify_audit_snapshot
    try:
        project_json = os.path.join(snapshot_dir, "project", "project.json")
        provider = _production_service(project_json, assemble_visual_provider=False).store.events.key_provider if verify_hmac else None
        value = verify_audit_snapshot(destination=snapshot_dir, key_provider=provider, verify_hmac=verify_hmac)
        _echo_production_payload(value, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.group("artifact")
def production_artifact():
    """Inspect immutable artifact versions and their dependency graph."""


@production_artifact.command("status")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True)
def production_artifact_status(project_json, json_output):
    from manju.production import ProductionError
    try:
        _echo_production_payload(_production_service(project_json, assemble_visual_provider=False).get_artifact_graph(), json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@production_artifact.command("register")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--logical-id", required=True)
@click.option("--path", "artifact_path", required=True, help="Project-relative file path")
@click.option("--producer-stage", required=True)
@click.option("--depends-on", multiple=True, help="Dependency JSON: {logical_id, version_id}")
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_artifact_register(project_json, logical_id, artifact_path, producer_stage, depends_on, expected_last_event_hash, json_output):
    from manju.production import ProductionError
    try:
        dependencies = tuple(json.loads(item) for item in depends_on)
        value = _production_service(project_json, assemble_visual_provider=False).register_artifact(
            logical_id=logical_id, path=artifact_path, producer_stage=producer_stage,
            depends_on=dependencies, expected_last_event_hash=expected_last_event_hash,
        )
        _echo_production_payload(value, json_output=json_output)
    except (ProductionError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProductionError):
            _handle_production_error(exc, json_output=json_output)
        else:
            _handle_production_error(ProductionError("OPERATION_CONTRACT_INVALID", "depends-on must be valid JSON"), json_output=json_output)


@production_artifact.command("select")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--logical-id", required=True)
@click.option("--version-id", required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--json", "json_output", is_flag=True)
def production_artifact_select(project_json, logical_id, version_id, expected_last_event_hash, json_output):
    from manju.production import ProductionError
    try:
        value = _production_service(project_json, assemble_visual_provider=False).select_artifact_version(
            logical_id=logical_id, version_id=version_id, expected_last_event_hash=expected_last_event_hash,
        )
        _echo_production_payload(value, json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@cli.group("revision")
def production_revision():
    """Preview and create immutable successor runs."""


@production_revision.command("list")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True)
def production_revision_list(project_json, json_output):
    from manju.production import ProductionError
    try:
        _echo_production_payload(_production_service(project_json, assemble_visual_provider=False).list_revisions(), json_output=json_output)
    except ProductionError as exc:
        _handle_production_error(exc, json_output=json_output)


@production_revision.command("preview")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--changed", multiple=True, required=True, help="Current artifact JSON: {logical_id, version_id}")
@click.option("--json", "json_output", is_flag=True)
def production_revision_preview(project_json, changed, json_output):
    from manju.production import ProductionError
    try:
        value = _production_service(project_json, assemble_visual_provider=False).preview_revision(
            changed=tuple(json.loads(item) for item in changed)
        )
        _echo_production_payload(value, json_output=json_output)
    except (ProductionError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _handle_production_error(exc if isinstance(exc, ProductionError) else ProductionError("OPERATION_CONTRACT_INVALID", "changed must be valid JSON"), json_output=json_output)


@production_revision.command("create")
@click.argument("project_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--changed", multiple=True, required=True, help="Current artifact JSON: {logical_id, version_id}")
@click.option("--requested-by", required=True)
@click.option("--reason", required=True)
@click.option("--preview-fingerprint", required=True)
@click.option("--expected-last-event-hash", required=True)
@click.option("--revision-id", default="")
@click.option("--json", "json_output", is_flag=True)
def production_revision_create(project_json, changed, requested_by, reason, preview_fingerprint, expected_last_event_hash, revision_id, json_output):
    from manju.production import ProductionError
    try:
        value = _production_service(project_json, assemble_visual_provider=False).create_revision(
            changed=tuple(json.loads(item) for item in changed), requested_by=requested_by, reason=reason,
            preview_fingerprint=preview_fingerprint, expected_last_event_hash=expected_last_event_hash,
            revision_id=revision_id,
        )
        _echo_production_payload(value, json_output=json_output)
    except (ProductionError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _handle_production_error(exc if isinstance(exc, ProductionError) else ProductionError("OPERATION_CONTRACT_INVALID", "changed must be valid JSON"), json_output=json_output)


# ── 剧本入口 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("-g", "--genre", default="",
              help="类型提示（古风/现代/科幻/悬疑/甜宠...）")
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认 OUTPUT_BASE/<date>/）")
def adapt(file, genre, output_dir):
    """小说→剧本：从小说文本提取角色/场景/对白为结构化剧本。

    FILE: 小说TXT文件路径
    """
    try:
        result = run_adapt(file, output_dir=output_dir, genre=genre)
        if result:
            click.echo(f"\n✅ 剧本适配完成: {len(result.get('scenes', []))} 场")
        else:
            click.echo("\n❌ 剧本适配失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--title", default="", help="剧名")
@click.option("--genre", default="", help="类型（古风/现代/科幻/悬疑...）")
@click.option("--premise", default="", help="一句话梗概（故事核）")
@click.option("--protagonist", default="", help="主角设定（姓名+性格+外貌）")
@click.option("--conflict", default="", help="核心冲突")
@click.option("--world-rules", default="", help="世界观规则（特殊设定）")
@click.option("--scenes", default="", help="目标场次（如：6-8场）")
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认 OUTPUT_BASE/<date>/）")
@click.option("--no-interactive", is_flag=True,
              help="纯命令行模式（不交互，需提供所有参数）")
def create(title, genre, premise, protagonist, conflict, world_rules,
           scenes, output_dir, no_interactive):
    """AI创作剧本：根据用户提供的关键信息生成完整剧本。

    无参数时进入交互模式，逐步引导填写。"""
    try:
        params = {}
        if title:
            params["title"] = title
        if genre:
            params["genre"] = genre
        if premise:
            params["premise"] = premise
        if protagonist:
            params["protagonist"] = protagonist
        if conflict:
            params["conflict"] = conflict
        if world_rules:
            params["world_rules"] = world_rules
        if scenes:
            params["target_duration"] = scenes

        # Non-interactive mode: require at least premise
        if no_interactive and not premise:
            click.echo("❌ 非交互模式需要至少 --premise 参数", err=True)
            sys.exit(1)

        interactive = not no_interactive

        result = run_create(
            params=params if params else None,
            output_dir=output_dir,
            interactive=interactive,
        )
        if result:
            click.echo(f"\n✅ 剧本创作完成: {len(result.get('scenes', []))} 场")
        elif not params and no_interactive:
            click.echo("\n❌ 参数不足或生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 制作命令 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认 OUTPUT_BASE/<date>/storyboard/）")
@click.option("--max-scenes", type=int, default=None,
              help="目标场景数（1-8，默认按字数自动决定）")
@click.option("--image-api/--no-image-api", default=False,
              help="逐镜生图 (需配置生图API)")
@click.option("--resume/--no-resume", default=True,
              help="从相同源文件的已完成阶段续跑")
@click.option("--engine", type=click.Choice(["legacy", "workflow", "agent"]), default="legacy",
              show_default=True, help="分镜编排引擎")
@click.option("--image-engine", type=click.Choice(["legacy", "agent"]), default="legacy",
              show_default=True, help="图片生成引擎")
@click.option("--agent-max-steps", type=click.IntRange(min=1), default=40,
              show_default=True, help="主管 Agent 最多工具步骤")
@click.option("--agent-max-calls", type=str, default="auto", callback=_parse_agent_max_calls,
              show_default=True, help="主管 Agent 最多模型调用")
@click.option("--agent-max-revisions", type=click.IntRange(min=0), default=2,
              show_default=True, help="主管 Agent 每场最多修订次数")
def storyboard(file, output_dir, max_scenes, image_api, resume, engine, image_engine,
               agent_max_steps, agent_max_calls, agent_max_revisions):
    """分镜生成：读取剧本JSON或小说 → LLM生成分镜 → 可选生图。

    FILE: 剧本JSON或改编后小说TXT。
    输出 v2 storyboard.json + storyboard.md + storyboard.xlsx，
    并在 stages/ 保留各生成阶段产物。
    """
    try:
        result = run_storyboard(
            file, output_dir=output_dir,
            max_scenes=max_scenes, image_api=image_api, resume=resume,
            strict_exports=True, engine=engine,
            image_engine=image_engine,
            agent_max_steps=agent_max_steps, agent_max_calls=agent_max_calls,
            agent_max_revisions=agent_max_revisions,
        )
        if result:
            if result.get("metadata", {}).get("agent_status") == "needs_review":
                click.echo("\nAgent output needs human review; media stages were not started.", err=True)
                sys.exit(2)
            visual_status = result.get("metadata", {}).get("visual_agent_status")
            if visual_status == "awaiting_approval":
                click.echo("\n图像 Agent 已暂停，填写 approvals 中的审批文件后使用 --resume 续跑。", err=True)
                sys.exit(3)
            if visual_status == "needs_review":
                click.echo("\n图像 Agent 需要人工质量检查；媒体下游未启动。", err=True)
                sys.exit(2)
            if visual_status == "failed":
                click.echo("\n图像 Agent 运行失败。", err=True)
                sys.exit(1)
            click.echo(f"\n✅ 分镜完成: {sum(len(s.get('shots', [])) for s in result.get('scenes', []))} 镜")
        else:
            click.echo("\n❌ 分镜生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("storyboard_json", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认与 storyboard.json 同目录）")
def video(storyboard_json, output_dir):
    """视频提示词：读取分镜JSON → 中英双版视频提示词。

    输出 video_prompts.json + video_prompts.md"""
    try:
        result = run_video(storyboard_json, output_dir=output_dir, strict_exports=True)
        if result is not None:
            click.echo("\n✅ 视频提示词完成")
        else:
            click.echo("\n❌ 生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("storyboard_json", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认与 storyboard.json 同目录）")
@click.option("--speak/--no-speak", default=False,
              help="生成配音音频文件")
def voice(storyboard_json, output_dir, speak):
    """配音脚本：读取分镜JSON → 提取对白 → 情绪推断。

    输出 voice_scripts.json + voice_scripts.pdf。
    加 --speak 同时生成 MP3 音频。"""
    try:
        result = run_voice(storyboard_json, output_dir=output_dir, strict_exports=True)
        if result is not None:
            click.echo("\n✅ 配音脚本完成")
            if speak:
                vdir = output_dir or os.path.dirname(os.path.abspath(storyboard_json))
                paths = run_batch_speak(result, vdir, return_paths=True)
                expected = sum(1 for line in result if line.get("text") not in ("（无对白）", "（无有效台词）"))
                with open(storyboard_json, encoding="utf-8") as handle:
                    state = json.load(handle)
                base = os.path.dirname(os.path.abspath(storyboard_json))
                for scene in state.get("scenes", []):
                    for shot in scene.get("shots", []):
                        shot_id = str(shot.get("shot_id", ""))
                        if shot_id in paths:
                            shot.setdefault("assets", {})["voice"] = os.path.relpath(paths[shot_id], base)
                            shot.setdefault("status", {})["voice"] = "completed"
                atomic_write_json(storyboard_json, state)
                if len(paths) != expected:
                    raise click.ClickException(f"配音未完全成功: {len(paths)}/{expected}")
                click.echo(f"\n🎙️  配音完成: {len(paths)} 个音频")
        else:
            click.echo("\n❌ 生成失败", err=True)
            sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 视频生成 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("prompt")
@click.option("-i", "--image", default="",
              help="参考图URL（img2video模式）")
@click.option("--frames", type=int, default=121,
              help="帧数 (8n+1, ≤441, 默认121≈5s)")
@click.option("--fps", type=int, default=24,
              help="帧率 (默认24)")
@click.option("--size", default="768x512",
              help="分辨率 (默认768x512)")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
def generate(prompt, image, frames, fps, size, output_dir):
    """生成视频：文本描述 → AI视频（可选参考图）。

    PROMPT: 视频内容描述（中文或英文皆可）

    使用前需在 ~/.manju.env 中配置视频API：
      MANJU_VIDEO_API_KEY=your-key
      MANJU_VIDEO_API_BASE=https://your-api.example.com/v1
    """
    try:
        if not prompt or not prompt.strip():
            click.echo("❌ 请提供视频内容描述", err=True)
            sys.exit(1)

        result = run_generate(
            prompt, image_path=image,
            num_frames=frames, frame_rate=fps,
            size=size, output_dir=output_dir,
        )
        if result:
            click.echo(f"\n✅ 视频已保存: {result}")
        else:
            click.echo("\n⚠ 视频生成未完成（可稍后重试）", err=True)
            raise click.ClickException("视频生成失败")
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 图片生成 ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("prompt", required=False, default=None)
@click.option("-i", "--image", default="",
              help="参考图URL（img2img模式）")
@click.option("--size", default="1024x1024",
              help="分辨率 (默认1024x1024)")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
@click.option("-n", "--name", default="",
              help="输出文件名（不含扩展名）")
@click.option("--batch", "batch_file", type=click.Path(exists=True), default=None,
              help="批量模式：从文件读取提示词（每行一条，跳过空行/#注释）")
def image(prompt, image, size, output_dir, name, batch_file):
    """生成图片：文本描述 → AI图片（可选参考图）。

    PROMPT: 图片内容描述（中英文皆可）
    使用 --batch 文件路径 切换批量模式。

    使用前需在 ~/.manju.env 中配置生图API：
      MANJU_IMAGE_API_KEY=your-key
      MANJU_IMAGE_API_BASE=https://your-api.example.com/v1
      MANJU_IMAGE_MODEL=your-model-name
    """
    try:
        if batch_file:
            # Batch mode: read prompts from file
            count = run_batch_from_file(batch_file, output_dir=output_dir, size=size)
            total = count_image_batch_lines(batch_file)
            if total > 0 and count == total:
                click.echo(f"\n✅ 批量生图完成: {count} 张")
            else:
                raise click.ClickException(f"批量生图未完全成功: {count}/{total}")
            return

        # Single prompt mode — validate input
        if not prompt or not prompt.strip():
            click.echo("❌ 请提供提示词描述，或使用 --batch 从文件批量生成", err=True)
            sys.exit(1)

        result = run_image(
            prompt, image_path=image,
            size=size,
            output_dir=output_dir, output_name=name,
        )
        if result:
            click.echo(f"\n✅ 图片已保存: {result}")
        else:
            click.echo("\n⚠ 图片生成失败", err=True)
            raise click.ClickException("图片生成失败")
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


@cli.command("image-agent")
@click.argument("storyboard_json", type=click.Path(exists=True))
@click.option("-o", "--output-dir", default=None,
              help="输出目录（默认与 storyboard.json 同目录）")
@click.option("--image-api/--no-image-api", default=False,
              help="明确授权执行审批范围内的付费生图调用")
@click.option("--resume/--no-resume", default=True,
              help="从相同输入、模型和预算的状态续跑")
@click.option("--resume-needs-review", is_flag=True, default=False,
              help="经人工检查后，显式恢复主管主动停止的 needs_review 运行")
@click.option("--resume-reviewer", default="",
              help="恢复 needs_review 的人工审核人标识")
@click.option("--resume-note", default="",
              help="恢复 needs_review 的具体人工判断说明")
@click.option("--recheck-vision", is_flag=True, default=False,
              help="仅复核现有镜头图片，不重新生图；必须配合 --no-image-api")
@click.option("--repair-vision-blockers", is_flag=True, default=False,
              help="从最近一次完整 vision-only 阻塞结果创建或续跑定向修复")
@click.option("--reset-foundation-references", is_flag=True, default=False,
              help="按修复计划准备定向 Foundation 参考重置；本步骤不调用 API")
@click.option("--prepare-provider-escalation", is_flag=True, default=False,
              help="从非收敛终态生成单约束 provider escalation 计划；本步骤零 API")
@click.option("--vision-calibration-file", type=click.Path(exists=True, dir_okay=False),
              default=None, help="本地人工标注的 vision confidence 校准样本或报告")
@click.option("--reconcile-metadata", is_flag=True, default=False,
              help="Backfill local revision provenance without calling any API")
@click.option("--reconcile-paid-artifacts", is_flag=True, default=False,
              help="Recover from the event store, rebuild projections, and normalize paid artifacts without any API")
@click.option("--foundation-candidates", type=click.IntRange(min=1), default=3,
              show_default=True, help="每项基础资产候选数")
@click.option("--max-auto-retries", type=click.IntRange(min=0), default=1,
              show_default=True, help="每个场景组自动修正轮次")
@click.option("--image-parallelism", type=click.IntRange(min=1, max=16), default=None,
              help="共享锁定参考的并行生图数（默认读取配置，通常为 4）")
@click.option("--size", default="auto", show_default=True,
              help="图片请求尺寸；auto 根据 storyboard 画幅和供应商支持尺寸选择")
@click.option("--visual-max-steps", type=click.IntRange(min=1), default=None,
              help="图像主管最多工具步骤（默认自动）")
@click.option("--visual-max-calls", type=str, default="auto", callback=_parse_agent_max_calls,
              show_default=True, help="兼容 supervisor 诊断模型调用上限；v4 默认确定性路由为 0")
def image_agent(storyboard_json, output_dir, image_api, resume,
                 resume_needs_review, resume_reviewer, resume_note, recheck_vision,
                 repair_vision_blockers, reset_foundation_references,
                 prepare_provider_escalation, vision_calibration_file,
                 reconcile_metadata, reconcile_paid_artifacts,
                 foundation_candidates, max_auto_retries, image_parallelism, size,
                visual_max_steps, visual_max_calls):
    """审批驱动的图像主管 Agent：基础资产锁定后按场景组生图。"""
    from manju.pipeline.visual_agent import (
        prepare_provider_escalation as prepare_provider_escalation_run,
        reconcile_paid_artifacts as reconcile_paid_artifacts_run,
        reconcile_visual_metadata,
        run_image_agent,
    )

    if prepare_provider_escalation:
        if (
            image_api or not resume or recheck_vision or repair_vision_blockers
            or reset_foundation_references or reconcile_metadata or reconcile_paid_artifacts
        ):
            raise click.ClickException(
                "provider escalation preparation requires --resume --no-image-api and cannot "
                "be combined with reconcile, recheck, repair, or Foundation reset modes"
            )
        try:
            result = prepare_provider_escalation_run(
                storyboard_json, output_dir=output_dir
            )
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if reconcile_metadata or reconcile_paid_artifacts:
        if image_api or recheck_vision or repair_vision_blockers or reset_foundation_references:
            raise click.ClickException(
                "reconcile mode requires --no-image-api and cannot be combined "
                "with vision recheck or repair"
            )
        if reconcile_metadata and reconcile_paid_artifacts:
            raise click.ClickException("select only one reconcile mode")
        try:
            result = (
                reconcile_paid_artifacts_run(storyboard_json, output_dir=output_dir)
                if reconcile_paid_artifacts else
                reconcile_visual_metadata(storyboard_json, output_dir=output_dir)
            )
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if reconcile_metadata and result.get("status") != "completed":
            raise click.exceptions.Exit(2)
        return
    try:
        manifest = run_image_agent(
            storyboard_json, output_dir=output_dir, execute_paid_calls=image_api,
            resume=resume, resume_needs_review=resume_needs_review,
            resume_reviewer=resume_reviewer, resume_note=resume_note,
            recheck_vision=recheck_vision,
            repair_vision_blockers=repair_vision_blockers,
            reset_foundation_references=reset_foundation_references,
            foundation_candidates=foundation_candidates,
            max_auto_retries=max_auto_retries, size=size,
            image_parallelism=image_parallelism,
            vision_calibration_file=vision_calibration_file,
            max_steps=visual_max_steps, max_calls=visual_max_calls,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    status = manifest.get("status")
    click.echo(f"图像 Agent 状态: {status} ({manifest.get('stop_reason', '')})")
    if status == "completed":
        return
    if status == "awaiting_approval":
        click.echo("填写输出目录 approvals 中当前 decision.json，然后使用 --resume 续跑。", err=True)
        raise click.exceptions.Exit(3)
    if status == "needs_review":
        raise click.exceptions.Exit(2)
    raise click.exceptions.Exit(1)


@cli.command("hybrid-render")
@click.argument("scene_json", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output-dir", required=True, type=click.Path(file_okay=False),
              help="混合渲染产物目录；不会调用任何模型或外部 API")
def hybrid_render(scene_json, output_dir):
    """离线合成 v4.1 图层场景，并校验尺寸、方向、数量和保护区。"""
    from manju.pipeline.visual.hybrid import render_hybrid_scene_file

    try:
        manifest = render_hybrid_scene_file(scene_json, output_dir)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(manifest, ensure_ascii=False, indent=2))
    if manifest["status"] != "auto_verified":
        raise click.exceptions.Exit(2)


@cli.command("hybrid-approve")
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--reviewer", required=True, help="人工复核人标识")
@click.option("--note", required=True, help="具体复核结论")
@click.option("--run-id", default=None, help="默认使用当前 hybrid run")
def hybrid_approve(output_dir, reviewer, note, run_id):
    """签核已通过硬校验但需要人工复核的 hybrid 渲染。"""
    from manju.pipeline.visual.hybrid import record_hybrid_human_verification

    try:
        verification = record_hybrid_human_verification(
            output_dir, reviewer=reviewer, note=note, run_id=run_id,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(verification, ensure_ascii=False, indent=2))


@cli.command("hybrid-asset-inspect")
@click.argument("image_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--asset-id", required=True)
@click.option("--revision", required=True)
@click.option("--asset-type", required=True)
@click.option("--source-kind", type=click.Choice(["provider", "local", "fixture"]), default="provider", show_default=True)
@click.option("--technical-requirements-json", help="可选技术要求 JSON；仅记录和校验本地素材，不调用 provider")
@click.option("--derivation-json", help="可选派生谱系 JSON；必须含可校验的 parent_image_path 和哈希")
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False))
def hybrid_asset_inspect(image_path, asset_id, revision, asset_type, source_kind, technical_requirements_json, derivation_json, output):
    """Inspect an existing local asset; this command never calls a provider."""
    from manju.pipeline.visual.asset_intake import inspect_asset_candidate

    try:
        technical_requirements = json.loads(technical_requirements_json) if technical_requirements_json else None
        derivation = json.loads(derivation_json) if derivation_json else None
        report = inspect_asset_candidate(
            image_path, output, asset_id=asset_id, revision=revision,
            asset_type=asset_type, source_kind=source_kind,
            technical_requirements=technical_requirements, derivation=derivation,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@cli.command("hybrid-asset-promote")
@click.argument("candidate_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--reviewer", required=True)
@click.option("--note", required=True)
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False))
def hybrid_asset_promote(candidate_json, reviewer, note, output):
    """Sign a reviewed asset inspection for provider use; no provider is called."""
    from manju.pipeline.visual.asset_intake import promote_asset_candidate

    try:
        promotion = promote_asset_candidate(candidate_json, output, reviewer=reviewer, note=note)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(promotion, ensure_ascii=False, indent=2))


@cli.command("hybrid-plan")
@click.argument("storyboard_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--assets", "asset_registry_json", required=True, type=click.Path(exists=True, dir_okay=False),
              help="版本化资产注册表；只读取其中声明的资产根目录")
@click.option("-o", "--output-dir", required=True, type=click.Path(file_okay=False),
              help="新的不可变规划产物目录；不会调用模型或外部 API")
@click.option("--allow-fixtures", is_flag=True, default=False,
              help="仅允许显式标记为 fixture 的本地测试素材参与规划")
@click.option("--timeout-seconds", type=click.FloatRange(min=0.01, max=5.0), default=5.0,
              show_default=True, help="每镜头本地确定性求解的硬超时")
def hybrid_plan(storyboard_json, asset_registry_json, output_dir, allow_fixtures, timeout_seconds):
    """离线将结构化 storyboard 规划为可审计的 hybrid scene JSON。"""
    from manju.pipeline.visual.planner import plan_hybrid_storyboard_file

    try:
        result = plan_hybrid_storyboard_file(
            storyboard_json, asset_registry_json, output_dir,
            allow_fixtures=allow_fixtures, timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result["manifest"], ensure_ascii=False, indent=2))
    if result["plan"]["status"] != "ready":
        raise click.exceptions.Exit(2)


@cli.command("hybrid-plan-verify")
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
def hybrid_plan_verify(output_dir):
    """校验规划文件、scene JSON 和不可变内容指纹链。"""
    from manju.pipeline.visual.planner import verify_hybrid_plan

    try:
        result = verify_hybrid_plan(output_dir)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command("hybrid-plan-approve")
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--reviewer", required=True, help="人工复核人标识")
@click.option("--note", required=True, help="具体的构图复核结论")
def hybrid_plan_approve(output_dir, reviewer, note):
    """签核高风险模型软建议参与的离线规划。"""
    from manju.pipeline.visual.planner import record_hybrid_plan_human_verification

    try:
        result = record_hybrid_plan_human_verification(
            output_dir, reviewer=reviewer, note=note,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command("hybrid-plan-replan")
@click.argument("plan_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("render_output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("-o", "--output-dir", required=True, type=click.Path(file_okay=False),
              help="新的不可变重排计划目录；最多三个连续重排版本")
def hybrid_plan_replan(plan_dir, render_output_dir, output_dir):
    """根据已记录的本地像素可见性失败创建一次软布局重排。"""
    from manju.pipeline.visual.planner import replan_hybrid_plan_from_render

    try:
        result = replan_hybrid_plan_from_render(plan_dir, render_output_dir, output_dir)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result["manifest"], ensure_ascii=False, indent=2))
    if result["plan"]["status"] != "ready":
        raise click.exceptions.Exit(2)


# ── 配音生成 ──────────────────────────────────────────────────────────────────

@cli.command("speak")
@click.argument("text", required=False, default=None)
@click.option("-v", "--voice", default="xiaoxiao",
              help="音色 (xiaoxiao/yunxi/yunjian/yunyang/xiaoyi/yunxia)")
@click.option("--speed", type=float, default=1.0,
              help="语速 (0.25-4.0, 默认1.0)")
@click.option("--pitch", type=int, default=5,
              help="声调 1-10 (默认5)")
@click.option("--volume", type=int, default=5,
              help="音量 1-10 (默认5)")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
@click.option("-n", "--name", default="",
              help="输出文件名（不含扩展名）")
@click.option("--batch", "batch_file", type=click.Path(exists=True), default=None,
              help="批量模式：从文件读取文本行（每行一条，跳过空行/#注释）")
def speak(text, voice, speed, pitch, volume, output_dir, name, batch_file):
    """文字转语音：文本 → MP3音频。

    零配置即可使用（需 pip install edge-tts）。
    也可在 ~/.manju.env 中配置自选API：
      MANJU_VOICE_API_KEY=sk-...
      MANJU_VOICE_API_BASE=https://...
    """
    try:
        if batch_file:
            # Batch mode: read lines from file
            count = run_batch_speak_file(batch_file, output_dir=output_dir)
            total = count_voice_batch_lines(batch_file)
            if total > 0 and count == total:
                click.echo(f"\n✅ 批量配音完成: {count} 个音频")
            else:
                raise click.ClickException(f"批量配音未完全成功: {count}/{total}")
            return

        # Single text mode — validate input
        if not text or not text.strip():
            click.echo("❌ 请提供要朗读的文本，或使用 --batch 从文件批量配音", err=True)
            sys.exit(1)

        result = run_speak(
            text, voice=voice, speed=speed,
            pitch=pitch, volume=volume,
            output_dir=output_dir, output_name=name,
        )
        if result:
            click.echo(f"\n✅ 音频已保存: {result}")
        else:
            click.echo("\n⚠ 生成失败", err=True)
            raise click.ClickException("配音生成失败")
    except KeyboardInterrupt:
        click.echo("\n⚠ 用户中断")
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 出错: {e}", err=True)
        sys.exit(1)


# ── 全流程 ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--script", "script_path", type=click.Path(exists=True),
              default=None, help="已有剧本JSON（跳过adapt/create）")
@click.option("--storyboard-json", type=click.Path(exists=True), default=None,
              help="已有分镜JSON（直接进入配音/视频阶段）")
@click.option("--novel", type=click.Path(exists=True),
              default=None, help="小说TXT（自动 adapt → storyboard）")
@click.option("--genre", default="", help="类型提示")
@click.option("-o", "--output-dir", default=None,
              help="输出目录")
@click.option("--storyboard/--no-storyboard", "do_storyboard",
              default=True, help="生成分镜")
@click.option("--video/--no-video", "do_video",
              default=True, help="生成视频提示词")
@click.option("--voice/--no-voice", "do_voice",
              default=True, help="生成配音脚本")
@click.option("--speak/--no-speak", "do_speak",
              default=False, help="生成配音音频文件（需先启用 --voice）")
@click.option("--image-api/--no-image-api", default=False,
              help="生图")
@click.option("--render-videos/--no-render-videos", default=False,
              help="按镜头调用视频API生成视频素材（可能产生费用）")
@click.option("--resume/--no-resume", default=True,
              help="续跑相同输入的分镜阶段与素材缓存")
@click.option("--max-scenes", type=int, default=None,
              help="目标场景数（1-8）")
@click.option("--engine", type=click.Choice(["legacy", "workflow", "agent"]), default="legacy",
              show_default=True, help="分镜编排引擎")
@click.option("--image-engine", type=click.Choice(["legacy", "agent"]), default="legacy",
              show_default=True, help="图片生成引擎")
@click.option("--agent-max-steps", type=click.IntRange(min=1), default=40,
              show_default=True, help="主管 Agent 最多工具步骤")
@click.option("--agent-max-calls", type=str, default="auto", callback=_parse_agent_max_calls,
              show_default=True, help="主管 Agent 最多模型调用")
@click.option("--agent-max-revisions", type=click.IntRange(min=0), default=2,
              show_default=True, help="主管 Agent 每场最多修订次数")
def pipeline(script_path, storyboard_json, novel, genre, output_dir, do_storyboard,
             do_video, do_voice, do_speak, image_api, render_videos, resume, max_scenes,
             engine, image_engine, agent_max_steps, agent_max_calls, agent_max_revisions):
    """一键全流程：剧本 → 分镜 → 配音 → 视频提示词。

    三种启动方式：
      manju pipeline --script <剧本.json>         # 已有剧本
      manju pipeline --novel <小说.txt>           # 从小说开始
      manju pipeline                               # 交互式创作
    """
    click.echo("=" * 60)
    if do_speak and not do_voice:
        raise click.UsageError("--speak 需要同时启用 --voice")
    if render_videos and not do_video:
        raise click.UsageError("--render-videos 需要同时启用 --video")
    click.echo("  manju pipeline — AI 漫剧全流程")
    click.echo("=" * 60)

    now = datetime.now()
    today = now.strftime("%Y.%m.%d_%H%M%S")
    out_dir = output_dir or os.path.join(OUTPUT_BASE, today)
    os.makedirs(out_dir, exist_ok=True)

    # ── Step 0: Get script ────────────────────────────────────────────────
    if storyboard_json:
        click.echo(f"\n🎬 已有分镜: {storyboard_json}")
        script_file = ""
    elif script_path:
        click.echo(f"\n📄 已有剧本: {script_path}")
        script_file = script_path
    elif novel:
        click.echo(f"\n📖 从小说适配: {novel}")
        result = run_adapt(novel, output_dir=out_dir, genre=genre)
        if not result:
            click.echo("❌ 适配失败", err=True)
            sys.exit(1)
        script_file = result.get("_output_path") or os.path.join(
            out_dir, os.path.splitext(os.path.basename(novel))[0] + "_script.json")
    else:
        click.echo("\n🎬 交互式创作剧本")
        result = run_create(output_dir=out_dir)
        if not result:
            click.echo("❌ 创作取消或失败", err=True)
            sys.exit(1)
        script_file = result.get("_output_path", "")

    if not storyboard_json and not os.path.exists(script_file):
        click.echo(f"❌ 剧本文件不存在: {script_file}", err=True)
        sys.exit(1)

    # ── Step 1: Storyboard ────────────────────────────────────────────────
    storyboard_file = storyboard_json
    if storyboard_json:
        sb_dir = os.path.dirname(os.path.abspath(storyboard_json))
    elif do_storyboard:
        click.echo(f"\n🎬 生成分镜...")
        sb_dir = os.path.join(out_dir, "storyboard")
        result = run_storyboard(script_file, output_dir=sb_dir,
                                 max_scenes=max_scenes, image_api=image_api,
                                 resume=resume, strict_exports=True, engine=engine,
                                 image_engine=image_engine,
                                 agent_max_steps=agent_max_steps,
                                agent_max_calls=agent_max_calls,
                                agent_max_revisions=agent_max_revisions)
        if not result:
            click.echo("❌ 分镜生成失败", err=True)
            sys.exit(1)
        if result.get("metadata", {}).get("agent_status") == "needs_review":
            click.echo("Agent output needs human review; pipeline stopped before all media stages.", err=True)
            raise click.exceptions.Exit(2)
        visual_status = result.get("metadata", {}).get("visual_agent_status")
        if visual_status == "awaiting_approval":
            click.echo("图像 Agent 等待审批；pipeline 已在配音和视频之前停止。", err=True)
            raise click.exceptions.Exit(3)
        if visual_status == "needs_review":
            click.echo("图像 Agent 需要人工检查；pipeline 已在配音和视频之前停止。", err=True)
            raise click.exceptions.Exit(2)
        if visual_status == "failed":
            raise click.ClickException("图像 Agent 失败")
        storyboard_file = os.path.join(sb_dir, "storyboard.json")
    else:
        # Find existing storyboard
        sb_dir = os.path.join(out_dir, "storyboard")
        for d in [sb_dir, out_dir]:
            candidate = os.path.join(d, "storyboard.json")
            if os.path.exists(candidate):
                storyboard_file = candidate
                sb_dir = d
                break

    if not storyboard_file or not os.path.exists(storyboard_file):
        raise click.ClickException("没有可用的 storyboard.json；请启用分镜生成或使用 --storyboard-json")
    else:
        # ── Step 2: Voice ─────────────────────────────────────────────────
        try:
            with open(storyboard_file, encoding="utf-8") as handle:
                existing_storyboard = json.load(handle)
        except (OSError, ValueError, TypeError):
            existing_storyboard = {}
        if existing_storyboard.get("metadata", {}).get("agent_status") == "needs_review":
            click.echo("Agent output needs human review; pipeline stopped before all media stages.", err=True)
            raise click.exceptions.Exit(2)
        if storyboard_json and image_engine == "agent":
            from manju.pipeline.visual_agent import run_image_agent

            visual_manifest = run_image_agent(
                storyboard_file, output_dir=sb_dir,
                execute_paid_calls=image_api, resume=resume,
            )
            if visual_manifest.get("status") == "awaiting_approval":
                click.echo("图像 Agent 等待审批；pipeline 已在配音和视频之前停止。", err=True)
                raise click.exceptions.Exit(3)
            if visual_manifest.get("status") == "needs_review":
                click.echo("图像 Agent 需要人工检查；pipeline 已在配音和视频之前停止。", err=True)
                raise click.exceptions.Exit(2)
            if visual_manifest.get("status") != "completed":
                raise click.ClickException("图像 Agent 失败")
        if do_voice:
            click.echo(f"\n🎙 生成配音脚本...")
            voice_result = run_voice(storyboard_file, output_dir=out_dir, strict_exports=True)
            if voice_result is None:
                click.echo("❌ 配音脚本生成失败", err=True)
                raise click.ClickException("全流程在配音阶段停止")

            if do_speak and voice_result is not None:
                click.echo(f"\n🎙️  生成配音音频...")
                audio_paths = run_batch_speak(voice_result, out_dir, return_paths=True)
                expected = sum(1 for line in voice_result if line.get("text") not in ("（无对白）", "（无有效台词）"))
                if len(audio_paths) != expected:
                    raise click.ClickException(f"配音未完全成功: {len(audio_paths)}/{expected}")
                click.echo(f"   ✅ 配音完成: {len(audio_paths)} 个音频")
                with open(storyboard_file, encoding="utf-8") as handle:
                    storyboard_state = json.load(handle)
                for scene in storyboard_state.get("scenes", []):
                    for shot in scene.get("shots", []):
                        shot_id = str(shot.get("shot_id", ""))
                        if shot_id in audio_paths:
                            shot.setdefault("assets", {})["voice"] = os.path.relpath(audio_paths[shot_id], sb_dir)
                            shot.setdefault("status", {})["voice"] = "completed"
                atomic_write_json(storyboard_file, storyboard_state)

        # ── Step 3: Video ─────────────────────────────────────────────────
        if do_video:
            click.echo(f"\n🎥 生成视频提示词...")
            video_prompts = run_video(storyboard_file, output_dir=out_dir, strict_exports=True)
            if video_prompts is None:
                click.echo("❌ 视频提示词生成失败", err=True)
                raise click.ClickException("全流程在视频提示词阶段停止")

            if render_videos:
                with open(storyboard_file, encoding="utf-8") as handle:
                    storyboard_state = json.load(handle)
                shot_map = {str(shot.get("shot_id", "")): shot
                            for scene in storyboard_state.get("scenes", [])
                            for shot in scene.get("shots", [])}
                rendered = 0
                for prompt in video_prompts:
                    shot_id = str(prompt.get("shot_id", ""))
                    shot = shot_map.get(shot_id, {})
                    image_rel = shot.get("assets", {}).get("image", "")
                    image_path = os.path.join(sb_dir, image_rel) if image_rel else ""
                    result_path = run_generate(
                        prompt.get("video_prompt_en") or prompt.get("video_prompt_cn", ""),
                        image_path=image_path, output_dir=os.path.join(out_dir, "videos"),
                        output_name=f"shot_{safe_filename(shot_id, 'unknown')}",
                        num_frames=max(25, int(float(shot.get("duration_seconds", 3)) * 24)),
                    )
                    if result_path:
                        rendered += 1
                        shot.setdefault("assets", {})["video"] = os.path.relpath(result_path, sb_dir)
                        shot.setdefault("status", {})["video"] = "completed"
                    else:
                        shot.setdefault("status", {})["video"] = "failed"
                atomic_write_json(storyboard_file, storyboard_state)
                if rendered != len(video_prompts):
                    raise click.ClickException(f"逐镜视频未完全成功: {rendered}/{len(video_prompts)}")

        # Merge video prompts into storyboard xlsx
        vp_json = os.path.join(out_dir, "video_prompts.json")
        sb_json = storyboard_file
        if os.path.exists(vp_json) and os.path.exists(sb_json):
            with open(vp_json, encoding="utf-8") as f: vp = json.load(f)
            with open(sb_json, encoding="utf-8") as f: sb = json.load(f)
            vp_map = {s["shot_id"]: s for s in vp.get("shots", [])}
            for scene in sb.get("scenes", []):
                for shot in scene.get("shots", []):
                    sid = shot.get("shot_id", "")
                    if sid in vp_map:
                        shot.setdefault("prompts", {})["video_cn"] = vp_map[sid].get("video_prompt_cn", "")
                        shot.setdefault("prompts", {})["video_en"] = vp_map[sid].get("video_prompt_en", "")
            atomic_write_json(sb_json, sb)
            try:
                from manju.utils.formats import write_xlsx
                write_xlsx(sb, os.path.join(sb_dir, "storyboard.xlsx"))
            except Exception as exc:
                raise click.ClickException(f"更新分镜Excel失败: {exc}") from exc

    click.echo(f"\n{'═' * 50}")
    click.echo(f"  ✅ 全流程完成")
    click.echo(f"  输出目录: {out_dir}")
    click.echo(f"{'═' * 50}")

    # ── Step 4: Use Guide ─────────────────────────────────────────────────
    sb_dir = os.path.dirname(os.path.abspath(storyboard_file)) if storyboard_file else os.path.join(out_dir, "storyboard")
    gathered = {}
    xlsx_path = os.path.join(sb_dir, "storyboard.xlsx")
    if os.path.exists(xlsx_path):
        gathered["storyboard_xlsx"] = "storyboard.xlsx"
    voice_pdf_path = os.path.join(out_dir, "voice_scripts.pdf")
    if os.path.exists(voice_pdf_path):
        gathered["voice_pdf"] = "voice_scripts.pdf"
    video_pdf_path = os.path.join(out_dir, "video_prompts.pdf")
    if os.path.exists(video_pdf_path):
        gathered["video_pdf"] = "video_prompts.pdf"
    click.echo(f"\n📋 生成使用指南...")
    guide_result = write_use_guide(out_dir, gathered)
    if not guide_result.get("pdf") or not guide_result.get("docx"):
        raise click.ClickException("使用指南未完整生成，请检查 weasyprint/python-docx")
    guide_pdf = os.path.join(out_dir, "使用指南.pdf")
    guide_docx = os.path.join(out_dir, "使用指南.docx")
    if os.path.exists(guide_pdf):
        click.echo(f"  📋 使用指南.pdf → {guide_pdf}")
    if os.path.exists(guide_docx):
        click.echo(f"  📋 使用指南.docx → {guide_docx}")



def main():
    cli()


if __name__ == "__main__":
    main()
