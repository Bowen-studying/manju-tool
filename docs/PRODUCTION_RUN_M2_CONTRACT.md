# ProductionRun M2.0 合同冻结

M2.0 只冻结顶层审批、授权与外部操作合同。它不调用真实图像 Provider，也不读取 visual agent 的私有 `state.json`、`approval_grants` 或 `paid_ledger`。

## 边界与版本

- 所有 DTO 的 `schema_version` 为 `1`；事件仍使用 ProductionRun event version `1`。
- 前端、CLI、REST、WebSocket 和 worker 只能通过 `ProductionService` 的查询/命令 DTO 交互。
- `VisualStageAdapter.map_published_approval` 只接受 `{ "published_approval": ApprovalRequest }`；任意私有 state 字段一律拒绝。
- `expected_last_event_hash` 是所有状态变更命令的乐观并发前置条件。

## 受签名的顶层事件

`approval_requested`、`approval_approved`、`approval_rejected`、`grant_issued`、`grant_revoked`、`call_reserved`、`call_submitted`、`call_settled`、`call_reconciled` 都是敏感事件。

事件在写入时用 `key_id` 指向进程提供的 HMAC-SHA256 key。HMAC 覆盖事件的规范 JSON（排除 `event_hash` 与 `hmac`），随后 hash chain 覆盖包含 HMAC 的完整事件。密钥不会写入项目或交付包。

没有密钥时，普通 hash-chain 仍可只读校验；但不能写入敏感事件。持有 key provider 的服务读取到缺 key、缺签名或签名篡改时，分别报 `HMAC_KEY_UNAVAILABLE` 或 `SENSITIVE_EVENT_SIGNATURE_INVALID`。

## DTO

`ApprovalRequest` 绑定 `project_id/run_id/stage/stage_run_id`、精确 artifact version、每个不可变 `operation_id/input_fingerprint`、Provider profile、调用数和金额上限、时效及固定的 approve/reject 决策集合。M2.0 仅允许 `stage=visual`、`kind=paid_visual_batch`，且 `maximum_paid_calls` 必须等于 intent 数量。

`Grant` 再次绑定上述身份、artifact version、operation IDs、预算和有效期，并具有独立 `key_id/signature`。它必须由已批准的同一 request 签发；跨项目、run、stage、fingerprint 或 intent 复用均无效。

`OperationRecord` 有稳定的 `operation_id/grant_id/input_fingerprint/provider_profile`。只允许：

| 事件 | 状态迁移 |
| --- | --- |
| `call_reserved` | new → reserved |
| `call_submitted` | reserved → submitted（必须已有 provider_job_id） |
| `call_settled` | submitted → settled |
| `call_reconciled` | settled/outcome_unknown → settled/succeeded 或 settled/failed |

`outcome_unknown` 不会自动重发：它以退出码 4 阻塞，必须先向 Provider 对账。

## 审批状态和退出码

`approval_requested` 将顶层 snapshot 设置为 `awaiting_approval`，reason 为 `PAID_VISUAL_BATCH_APPROVAL_REQUIRED`，`run` 退出码为 3，并返回 `approve/reject`（或已同意后的 `issue_grant`）动作。拒绝或撤销进入 `needs_review`（退出码 2）。审批、拒绝、授权均只写入签名合同；M2.0 不执行付费调用。

CLI 提供 `approvals`、`approve`、`reject`、`issue-grant`，其中 grant 的签发时间由服务端记录。敏感 CLI 命令要求仅在进程环境中提供 `MANJU_PRODUCTION_HMAC_KEY`；该值绝不落盘。

## 恢复规则

恢复提交过但未结算的操作时先按稳定 `operation_id`/`provider_job_id` 对账。系统不会因为进程重启或重放而盲目再次提交。真实 Provider 执行器将在后续里程碑以此合同接入；M2.0 测试全部使用离线对象和 mock。
