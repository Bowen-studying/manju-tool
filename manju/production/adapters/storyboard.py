"""Adapter from ProductionRun to the existing storyboard supervisor."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from collections.abc import Callable
from typing import Any

from manju.pipeline.storyboard import run_storyboard
from manju.pipeline.storyboard_supervisor import verify_storyboard_agent_authority
from manju.production.adapters.base import StageResult
from manju.production.models import ReasonCode
from manju.production.store import sha256_file
from manju.utils.runtime import read_json


StoryboardRunner = Callable[..., dict[str, Any] | None]


def storyboard_source_sha256(source_path: str) -> str:
    if source_path.lower().endswith(".docx"):
        from manju.utils.formats import read_input

        value = read_input(source_path)
        if value is None:
            raise ValueError("无法读取 DOCX source")
        raw_text = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else str(value)
    else:
        with open(source_path, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


class StoryboardStageAdapter:
    contract_version = "storyboard-adapter-m1-v1"

    def __init__(self, runner: StoryboardRunner | None = None):
        self.runner = runner or run_storyboard

    def inspect(
        self,
        *,
        stage_run_id: str,
        output_dir: str,
        expected: dict[str, Any] | None = None,
    ) -> StageResult | None:
        storyboard_path = os.path.join(output_dir, "storyboard.json")
        manifest_path = os.path.join(output_dir, "agent_run.json")
        manifest = read_json(manifest_path)
        storyboard = read_json(storyboard_path)
        if not isinstance(manifest, dict) and not isinstance(storyboard, dict):
            return None

        generation_engine = str((storyboard or {}).get("metadata", {}).get("generation_engine", ""))
        agent_status = str((storyboard or {}).get("metadata", {}).get("agent_status", ""))
        if not isinstance(manifest, dict) and (generation_engine == "agent" or agent_status):
            return None

        authority_run_id = str((manifest or {}).get("run_id", ""))
        if isinstance(manifest, dict) and not authority_run_id:
            return StageResult(
                status="failed",
                stage_run_id=stage_run_id,
                reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                message="分镜 Agent manifest 缺少 run_id",
                authority_path=manifest_path,
                authority_hash=sha256_file(manifest_path),
            )

        status = str((manifest or {}).get("status", ""))
        if status not in {"completed", "needs_review", "failed"}:
            if isinstance(storyboard, dict):
                status = str(storyboard.get("metadata", {}).get("agent_status", status))
                if not status and generation_engine in {"legacy", "workflow"}:
                    status = "completed"
        if status not in {"completed", "needs_review", "failed"}:
            return None

        expected = expected or {}
        metadata = (
            storyboard.get("metadata")
            if isinstance(storyboard, dict) and isinstance(storyboard.get("metadata"), dict)
            else {}
        )
        expected_source = str(expected.get("source_sha256", ""))
        expected_engine = str(expected.get("engine", ""))
        if isinstance(storyboard, dict) and expected_source and metadata.get("source_sha256") != expected_source:
            return StageResult(
                status="failed",
                stage_run_id=authority_run_id or stage_run_id,
                reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                message="分镜输出 source 指纹与 run 合同不一致",
                authority_path=manifest_path if os.path.isfile(manifest_path) else storyboard_path,
                authority_hash=sha256_file(manifest_path if os.path.isfile(manifest_path) else storyboard_path),
            )
        if isinstance(storyboard, dict) and expected_engine and metadata.get("generation_engine") != expected_engine:
            return StageResult(
                status="failed",
                stage_run_id=authority_run_id or stage_run_id,
                reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                message="分镜输出 engine 与 run 合同不一致",
                authority_path=manifest_path if os.path.isfile(manifest_path) else storyboard_path,
                authority_hash=sha256_file(manifest_path if os.path.isfile(manifest_path) else storyboard_path),
            )
        if isinstance(manifest, dict):
            manifest_contract = {
                "model": manifest.get("model"),
                "prompt_version": manifest.get("supervisor_agent_version"),
                "tool_version": manifest.get("toolset_version"),
                "max_steps": manifest.get("budgets", {}).get("max_steps"),
                "max_calls": manifest.get("budgets", {}).get("requested_max_calls"),
                "max_revisions": manifest.get("budgets", {}).get("max_revisions_per_scene"),
            }
            expected_contract = {
                "model": expected.get("model"),
                "prompt_version": expected.get("prompt_version"),
                "tool_version": expected.get("tool_version"),
                "max_steps": expected.get("max_steps"),
                "max_calls": expected.get("max_calls") if expected.get("max_calls") is not None else "auto",
                "max_revisions": expected.get("max_revisions"),
            }
            if expected and manifest_contract != expected_contract:
                return StageResult(
                    status="failed",
                    stage_run_id=authority_run_id or stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message="分镜 Agent manifest 与冻结模型/协议/预算合同不一致",
                    authority_path=manifest_path,
                    authority_hash=sha256_file(manifest_path),
                )

        authority_path = manifest_path if os.path.isfile(manifest_path) else storyboard_path
        authority_files = [{"path": authority_path, "sha256": sha256_file(authority_path)}]
        if isinstance(manifest, dict):
            checkpoint_ref = str(manifest.get("checkpoint", ""))
            if not checkpoint_ref:
                return StageResult(
                    status="failed",
                    stage_run_id=authority_run_id or stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message="分镜 Agent manifest 缺少 checkpoint 引用",
                    authority_path=manifest_path,
                    authority_hash=sha256_file(manifest_path),
                )
            checkpoint_path = os.path.realpath(os.path.join(output_dir, "stages", checkpoint_ref))
            stages_root = os.path.realpath(os.path.join(output_dir, "stages"))
            try:
                checkpoint_contained = os.path.commonpath([stages_root, checkpoint_path]) == stages_root
            except ValueError:
                checkpoint_contained = False
            if not checkpoint_contained or not os.path.isfile(checkpoint_path):
                return StageResult(
                    status="failed",
                    stage_run_id=authority_run_id or stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message="分镜 Agent checkpoint 缺失或越界",
                    authority_path=manifest_path,
                    authority_hash=sha256_file(manifest_path),
                )
            authority_files.append({"path": checkpoint_path, "sha256": sha256_file(checkpoint_path)})
            trace_ref = str(manifest.get("trace", ""))
            if not trace_ref:
                return StageResult(
                    status="failed",
                    stage_run_id=authority_run_id or stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message="分镜 Agent manifest 缺少 trace 引用",
                    authority_path=manifest_path,
                    authority_hash=sha256_file(manifest_path),
                )
            trace_path = os.path.realpath(os.path.join(output_dir, trace_ref))
            output_root = os.path.realpath(output_dir)
            try:
                trace_contained = os.path.commonpath([output_root, trace_path]) == output_root
            except ValueError:
                trace_contained = False
            if not trace_contained or not os.path.isfile(trace_path):
                return StageResult(
                    status="failed",
                    stage_run_id=authority_run_id or stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message="分镜 Agent trace 缺失或越界",
                    authority_path=manifest_path,
                    authority_hash=sha256_file(manifest_path),
                )
            authority_files.append({"path": trace_path, "sha256": sha256_file(trace_path)})
            valid, verification_message = verify_storyboard_agent_authority(
                checkpoint_path, trace_path, manifest, expected
            )
            if not valid:
                return StageResult(
                    status="failed",
                    stage_run_id=authority_run_id or stage_run_id,
                    reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
                    message=f"分镜 Agent 权威账本校验失败: {verification_message}",
                    authority_path=manifest_path,
                    authority_hash=sha256_file(manifest_path),
                )

        if not isinstance(storyboard, dict):
            return StageResult(
                status="failed",
                stage_run_id=authority_run_id or stage_run_id,
                reason_code=(
                    ReasonCode.STORYBOARD_FAILED.value
                    if status == "failed"
                    else ReasonCode.STAGE_INTEGRITY_FAILED.value
                ),
                message=(
                    str((manifest or {}).get("error") or (manifest or {}).get("stop_reason") or "分镜 Agent 执行失败")
                    if status == "failed"
                    else "分镜 manifest 存在，但 storyboard.json 缺失或损坏"
                ),
                authority_path=authority_path,
                authority_hash=sha256_file(authority_path),
                authority_files=tuple(authority_files),
            )

        artifact = {
            "logical_id": "storyboard.main",
            "version_id": f"sha256:{sha256_file(storyboard_path)}",
            "path": storyboard_path,
        }
        return StageResult(
            status=status,
            stage_run_id=authority_run_id or stage_run_id,
            reason_code=(
                ReasonCode.STORYBOARD_REVIEW_REQUIRED.value
                if status == "needs_review"
                else ReasonCode.STORYBOARD_FAILED.value if status == "failed" else ""
            ),
            message=str((manifest or {}).get("stop_reason", "")),
            artifacts=(artifact,),
            authority_path=authority_path,
            authority_hash=sha256_file(authority_path),
            authority_files=tuple(authority_files),
        )

    def execute(
        self,
        *,
        stage_run_id: str,
        source_path: str,
        output_dir: str,
        settings: dict[str, Any],
        expected: dict[str, Any] | None = None,
    ) -> StageResult:
        existing = self.inspect(stage_run_id=stage_run_id, output_dir=output_dir, expected=expected)
        if existing is not None:
            return existing

        os.makedirs(output_dir, exist_ok=True)
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            result = self.runner(
                source_path,
                output_dir=output_dir,
                max_scenes=settings.get("max_scenes"),
                image_api=False,
                resume=True,
                strict_exports=True,
                engine=str(settings.get("engine", "agent")),
                image_engine="legacy",
                agent_max_steps=int(settings.get("max_steps", 40)),
                agent_max_calls=settings.get("max_calls"),
                agent_max_revisions=int(settings.get("max_revisions", 2)),
            )
        inspected = self.inspect(stage_run_id=stage_run_id, output_dir=output_dir, expected=expected)
        if inspected is not None:
            return inspected
        if not isinstance(result, dict):
            return StageResult(
                status="failed",
                stage_run_id=stage_run_id,
                reason_code=ReasonCode.STORYBOARD_FAILED.value,
                message="分镜 Agent 未返回可用结果",
            )
        storyboard_path = os.path.join(output_dir, "storyboard.json")
        if str(settings.get("engine", "agent")) in {"legacy", "workflow"} and os.path.isfile(storyboard_path):
            return StageResult(
                status="completed",
                stage_run_id=stage_run_id,
                artifacts=({
                    "logical_id": "storyboard.main",
                    "version_id": f"sha256:{sha256_file(storyboard_path)}",
                    "path": storyboard_path,
                },),
                authority_path=storyboard_path,
                authority_hash=sha256_file(storyboard_path),
                authority_files=({"path": storyboard_path, "sha256": sha256_file(storyboard_path)},),
            )
        return StageResult(
            status="failed",
            stage_run_id=stage_run_id,
            reason_code=ReasonCode.STAGE_INTEGRITY_FAILED.value,
            message="分镜结果缺少可验证的 agent manifest",
        )
