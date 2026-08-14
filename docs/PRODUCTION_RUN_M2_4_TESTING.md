# ProductionRun M2.4 正式 Provider 测试说明

日期：2026-08-13

## 本次变更

M2.4 为真实付费 Provider 建立可自动恢复的最低安全合同：

- Provider 必须声明并满足：幂等 `submit`、异步可查询 job、只读 `reconcile`；否则在任何 submit 前以 `OPERATION_OUTCOME_UNKNOWN` 拒绝。
- 已批准的公开请求描述会原样传给 `provider.submit()`，并将 SHA-256 纳入 operation input fingerprint、Approval 和 Grant。
- 允许的公开描述字段仅为 `prompt`、`model`、`size`、`quality`、`n`、`response_format`；凭据、endpoint、任意额外控制字段不会进入 Approval 合同。
- `provider_profile` 与 `operation_kind` 从项目 visual 设置冻结到 run contract、Approval、Grant 和 Operation。
- 非 test fixture 的 final 费用要求 Provider 声明 `verified_cost=True`，且费用来源是 `provider_response` 或 `provider_ledger`；无可验证费用必须使用 `outcome_unknown`/`cost_status=unknown`，run 会 blocked，不会写名义常量冒充账单。

## 本地验证

在项目根目录，使用依赖已安装的 Python：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$Python = 'C:\path\to\python.exe'
& $Python -m compileall -q manju
& $Python -m pytest -q `
  tests/test_production_m2_4_provider_safety.py `
  tests/test_production_m2_3_binary_receipts.py `
  tests/test_production_m2_2_provider_contracts.py `
  tests/test_production_m2_1_mock_visual.py `
  tests/test_production_m2_contracts.py
& $Python -m pytest -q
```

结果：聚焦 `42 passed`；全量 `427 passed, 2 skipped`。

独立 `sol-medium` 第二轮只读验收：通过。其确认 descriptor 经 Approval/Grant/input binding 后原样传给 Provider，profile/kind 受项目合同冻结，Approval 直接构造也会拒绝凭据字段，`verified_cost` 在执行路径中生效；未读取凭据、未调用真实 Provider。

## 需要你执行的真实测试

需要测试，但仅适用于符合 M2.4 能力合同的真实 Provider。当前把同步图片生成放在 `reconcile()` 的网关不符合合同，M2.4 会拒绝其自动执行；请勿用它进行第二次付费测试。

开始前需准备：隔离账号、单 operation 的小额预算、审批人、已批准 HTTPS endpoint、artifact origin allowlist，以及不写入项目的凭据注入方式。Provider 实现必须能证明：

1. `submit(operation_id, idempotency_key, request)` 在上游只创建或返回同一个 job；同一幂等键重放不得创建第二个收费任务。
2. `submit()` 返回的 job ID 在进程重启后仍可查询。
3. `reconcile(job_id)` 只读取 job、下载已完成 artifact，不创建新图或新收费任务。
4. 成功/失败最终费用来自 provider 响应或可验证账单查询，并标注 `provider_response` 或 `provider_ledger`。

执行顺序：

1. 为一个公开、无敏感内容的 request 创建项目，写入 provider profile、operation kind、prompt/model/size/quality/n；核对审批展示值。
2. 批准后故意尝试替换 prompt、model 或 size，确认 Grant/input binding 拒绝。
3. 对同一 operation 在 submit 前后、job 已接受后、artifact 下载前后、receipt 前后分别中断重启；每个窗口确认上游 job 数量为 1，receipt 后 `reconcile` 为 0。
4. 验证成功、Provider failed with final fee、unknown cost、币种不符、实际费用超过预算。unknown 或超额必须 blocked，不得继续发布。
5. 导出完整恢复副本：source、contract、events、state、authority、receipt、published artifact 和 `.visual-pending-*.bin` 都进入 `SHA256SUMS.txt`；HMAC key 仅从受控环境注入。

本项目当前无法替你完成这一步，因为没有被批准且满足上述能力合同的真实 Provider mapping、账单查询接口和隔离凭据。请勿发送额外生成式“健康探测”请求；健康检查必须使用无付费的 Provider status/job 查询接口。
