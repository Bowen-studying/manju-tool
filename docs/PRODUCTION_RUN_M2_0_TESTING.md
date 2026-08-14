# ProductionRun M2.0 测试说明

日期：2026-08-11

## 变更范围

- 冻结顶层 `ApprovalRequest`、`Grant` 和 `OperationRecord` DTO，供后续前端只经由 `ProductionService` 接入。
- 敏感顶层事件使用 HMAC-SHA256 签名，并保留原有事件 hash chain；密钥仅从进程 key provider 或 `MANJU_PRODUCTION_HMAC_KEY` 获取，不写入项目。
- 增加 `awaiting_approval`（退出码 3）、审批查询/批准/拒绝/grant 命令和乐观并发 `expected_last_event_hash`。
- 固定 `call_reserved -> call_submitted -> call_settled`，并为 `outcome_unknown` 增加阻塞与 `call_reconciled` 对账恢复。
- `VisualStageAdapter` 只接受公开发布的 approval artifact，拒绝 visual 子系统的私有 state/ledger。
- 本里程碑不包含真实视觉 Provider 执行器，也未调用付费 API。

## 本地验证结果

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q
# 393 passed, 2 skipped in 149.71s

.\.venv\Scripts\python.exe -m manju.cli approvals --help
.\.venv\Scripts\python.exe -m manju.cli approve --help
.\.venv\Scripts\python.exe -m manju.cli reject --help
.\.venv\Scripts\python.exe -m manju.cli issue-grant --help
git diff --check
```

两个 skip 是平台条件测试；不是功能失败。专项负向测试覆盖 HMAC 篡改/缺失、无 key 写入、grant 的精确绑定与过期、操作阶段跳跃、私有 visual state、以及 `outcome_unknown` 后阻止其他操作。

## 独立验收状态

本任务按要求使用独立 `gpt-5.6-sol`、medium reasoning，最多三轮：

1. 第 1 轮发现 grant 绑定、operation 授权、过期和 HMAC envelope 缺口，已修复。
2. 第 2 轮发现 grant 活跃性/签名/有效期、operation 初态/kind 和 unknown 对账缺口，已修复。
3. 第 3 轮发现 `outcome_unknown` 后仍可提交/结算已预留操作。该报告返回前已加入 `blocked` guard，随后加入同类双 operation 回归并完成上述本地全量测试。

三轮独立复验额度已用完，因此本交付不声称独立验收通过。最后一项修复只有本地回归验证；下一轮应由新的独立验收授权复核该条 blocked guard 与 `call_reconciled` 恢复路径。

## 交付包复验

```powershell
.\tools\package_production_run_m2.ps1 -Destination C:\path\to\manju-tool-production-run-m2.0-20260811-final
Get-FileHash -Algorithm SHA256 -LiteralPath C:\path\to\manju-tool-production-run-m2.0-20260811-final\SHA256SUMS.txt
```

在 Linux/WSL 的交付包根目录执行：

```sh
sha256sum -c SHA256SUMS.txt
```
