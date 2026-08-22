"""Stable data contracts for ProductionRun."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


PROJECT_SCHEMA_VERSION = "1"
EVENT_VERSION = "1"
DAG_VERSION = "production-m1-v1"
M2_DAG_VERSION = "production-m2-v1"
M4_DAG_VERSION = "production-m4-v1"
M4_1_DAG_VERSION = "production-m4.1-v1"
M2_CONTRACT_VERSION = "1"


def stages_for_dag(dag_version: str, declared: Any = None) -> tuple[str, ...]:
    """Return the only legal stage sequence for a frozen DAG contract."""
    if declared is not None and (
        not isinstance(declared, list)
        or any(not isinstance(stage, str) for stage in declared)
    ):
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "DAG stage sequence is invalid")
    if dag_version == DAG_VERSION:
        expected = ("storyboard",)
    elif dag_version == M2_DAG_VERSION:
        expected = ("storyboard", "visual")
    elif dag_version == M4_DAG_VERSION:
        candidate = tuple(declared) if declared is not None else ()
        if candidate not in {
            ("storyboard", "voice_script"),
            ("storyboard", "voice_script", "visual"),
        }:
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "M4 stage sequence is invalid")
        return candidate
    elif dag_version == M4_1_DAG_VERSION:
        candidate = tuple(declared) if declared is not None else ()
        if candidate not in {
            ("storyboard", "voice_script", "voice_director"),
            ("storyboard", "voice_script", "voice_director", "visual"),
        }:
            raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "M4.1 stage sequence is invalid")
        return candidate
    else:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, f"unsupported dag: {dag_version}")
    if declared is not None and tuple(declared) != expected:
        raise ProductionError(ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value, "DAG stage sequence is invalid")
    return expected


class ProductionStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class ReasonCode(str, Enum):
    PROJECT_READY = "PROJECT_READY"
    PROJECT_ALREADY_COMPLETED = "PROJECT_ALREADY_COMPLETED"
    PROJECT_PAUSED = "PROJECT_PAUSED"
    PROJECT_LOCKED = "PROJECT_LOCKED"
    PROJECT_CONTRACT_CHANGED = "PROJECT_CONTRACT_CHANGED"
    PROJECT_EVENT_CHAIN_INVALID = "PROJECT_EVENT_CHAIN_INVALID"
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    STORYBOARD_RUNNING = "STORYBOARD_RUNNING"
    STORYBOARD_REVIEW_REQUIRED = "STORYBOARD_REVIEW_REQUIRED"
    STORYBOARD_FAILED = "STORYBOARD_FAILED"
    STAGE_INTEGRITY_FAILED = "STAGE_INTEGRITY_FAILED"
    DEPENDENCY_UNSATISFIED = "DEPENDENCY_UNSATISFIED"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PAID_VISUAL_BATCH_APPROVAL_REQUIRED = "PAID_VISUAL_BATCH_APPROVAL_REQUIRED"
    PAID_VISUAL_BATCH_REJECTED = "PAID_VISUAL_BATCH_REJECTED"
    HMAC_KEY_UNAVAILABLE = "HMAC_KEY_UNAVAILABLE"
    SENSITIVE_EVENT_SIGNATURE_INVALID = "SENSITIVE_EVENT_SIGNATURE_INVALID"
    APPROVAL_CONTRACT_INVALID = "APPROVAL_CONTRACT_INVALID"
    GRANT_CONTRACT_INVALID = "GRANT_CONTRACT_INVALID"
    OPERATION_CONTRACT_INVALID = "OPERATION_CONTRACT_INVALID"
    OPERATION_OUTCOME_UNKNOWN = "OPERATION_OUTCOME_UNKNOWN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    VISUAL_FAILED = "VISUAL_FAILED"
    VOICE_SCRIPT_FAILED = "VOICE_SCRIPT_FAILED"
    VOICE_DIRECTOR_FAILED = "VOICE_DIRECTOR_FAILED"


REASON_DEFAULTS: dict[str, tuple[str, int]] = {
    ReasonCode.PROJECT_READY.value: ("项目已准备，可以继续运行", 0),
    ReasonCode.PROJECT_ALREADY_COMPLETED.value: ("项目已经完成", 0),
    ReasonCode.PROJECT_PAUSED.value: ("项目已暂停", 0),
    ReasonCode.PROJECT_LOCKED.value: ("另一个进程正在推进该项目", 4),
    ReasonCode.PROJECT_CONTRACT_CHANGED.value: ("项目配置与活动运行合同不一致", 1),
    ReasonCode.PROJECT_EVENT_CHAIN_INVALID.value: ("项目事件链校验失败", 1),
    ReasonCode.SOURCE_MISSING.value: ("项目源文件不存在", 1),
    ReasonCode.SOURCE_HASH_MISMATCH.value: ("项目源文件内容已改变", 1),
    ReasonCode.STORYBOARD_RUNNING.value: ("分镜阶段正在运行", 0),
    ReasonCode.STORYBOARD_REVIEW_REQUIRED.value: ("当前分镜需要人工判断", 2),
    ReasonCode.STORYBOARD_FAILED.value: ("分镜阶段失败", 1),
    ReasonCode.STAGE_INTEGRITY_FAILED.value: ("子阶段完整性校验失败", 1),
    ReasonCode.DEPENDENCY_UNSATISFIED.value: ("阶段依赖尚未满足", 4),
    ReasonCode.UNSUPPORTED_SCHEMA_VERSION.value: ("项目或事件版本不受支持", 1),
    ReasonCode.INTERNAL_ERROR.value: ("ProductionRun 内部错误", 1),
}


REASON_DEFAULTS.update({
    ReasonCode.PAID_VISUAL_BATCH_APPROVAL_REQUIRED.value: ("付费视觉批次等待人工审批", 3),
    ReasonCode.PAID_VISUAL_BATCH_REJECTED.value: ("付费视觉批次已被拒绝", 2),
    ReasonCode.HMAC_KEY_UNAVAILABLE.value: ("敏感操作所需 HMAC 密钥不可用", 4),
    ReasonCode.SENSITIVE_EVENT_SIGNATURE_INVALID.value: ("敏感事件签名无效", 1),
    ReasonCode.APPROVAL_CONTRACT_INVALID.value: ("审批合同无效", 1),
    ReasonCode.GRANT_CONTRACT_INVALID.value: ("授权合同无效", 1),
    ReasonCode.OPERATION_CONTRACT_INVALID.value: ("外部操作合同或状态迁移无效", 1),
    ReasonCode.OPERATION_OUTCOME_UNKNOWN.value: ("外部操作结果未知，必须先对账", 4),
    ReasonCode.BUDGET_EXCEEDED.value: ("Provider 实际费用超出已签名预算，需人工处理", 4),
    ReasonCode.VISUAL_FAILED.value: ("mock 视觉阶段失败", 1),
    ReasonCode.VOICE_SCRIPT_FAILED.value: ("配音脚本阶段失败", 1),
    ReasonCode.VOICE_DIRECTOR_FAILED.value: ("配音导演阶段失败", 1),
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProductionReason:
    code: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        default_message = REASON_DEFAULTS.get(self.code, (self.code, 1))[0]
        return {"code": self.code, "message": self.message or default_message}


@dataclass(frozen=True)
class ProductionSnapshot:
    project_id: str
    run_id: str = ""
    status: str = ProductionStatus.PENDING.value
    current_stage: str = ""
    reason: ProductionReason = field(
        default_factory=lambda: ProductionReason(ReasonCode.PROJECT_READY.value)
    )
    progress_completed: int = 0
    progress_total: int = 1
    next_actions: tuple[dict[str, Any], ...] = ()
    updated_at: str = ""
    last_event_hash: str = ""

    @property
    def exit_code(self) -> int:
        return REASON_DEFAULTS.get(self.reason.code, ("", 1))[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "status": self.status,
            "current_stage": self.current_stage,
            "reason": self.reason.to_dict(),
            "progress": {
                "completed": self.progress_completed,
                "total": self.progress_total,
            },
            "next_actions": list(self.next_actions),
            "updated_at": self.updated_at,
            "last_event_hash": self.last_event_hash,
        }


class ProductionError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, exit_code: int | None = None):
        default_message, default_exit = REASON_DEFAULTS.get(code, (code, 1))
        self.code = code
        self.message = message or default_message
        self.exit_code = default_exit if exit_code is None else exit_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "error": {"code": self.code, "message": self.message},
        }
