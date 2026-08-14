# ProductionRun M2.1 复验与后续测试说明

日期：2026-08-11

## 当前结论

本包**未通过独立验收**，不能作为可接入真实付费 Visual Provider 的版本。按本轮约定，独立 `sol-medium` 验收已在三次修复后仍发现阻断项，因此停止继续修改。

已完成并通过的本地离线验证：

- `59 passed`：M2.1 mock visual、M2 contract 和 M1 ProductionRun 回归；
- Grant 在 `call_reserved`、`provider.submit()`、`provider.reconcile()` 前按服务端当前时间重新校验，过期时没有 Provider 或 call 事件副作用；
- 成功结算后可按冻结结果完成，不会因随后过期而丢失已结算结果；
- `get_status()`、`advance()` 与 `doctor()` 持续验证 storyboard 和 visual 的全部终态 authority；
- visual 固定在 run/stage 输出目录，authority、artifact 和已签名结算 operation 的 ID/job/result 三元组必须一致；
- 未读取真实 Provider 配置，未调用真实或付费 Provider。

## 独立验收阻断项

已签名 `result_fingerprint` 还没有可验证地绑定 `mock_image.json` 的完整字节内容。

攻击者若能改写非签名的产物与终态事件，可：

1. 保留已签名结算 operation 的 `operation_id`、`provider_job_id`、`result_fingerprint`；
2. 仅向 `mock_image.json` 增加任意字段或内容；
3. 更新 visual authority 中的 artifact SHA-256，更新 `stage_completed` 的 authority/artifact 哈希；
4. 重算该普通事件及其后普通事件的 hash chain；
5. 当前 `get_status()` 会错误返回 `completed`。

因此该版本不能宣称产物内容具有端到端篡改防护。

## 下一步修复方案

在新的独立验收额度下实施，且先冻结下列契约：

1. Provider reconcile 的已签名结果必须提供可验证的公开产物承诺值，例如 `artifact_content_sha256`；它应当是产物字节的 SHA-256，不能把该字段自身放入被哈希内容而形成循环。
2. `OperationRecord` / signed `call_settled`、`call_reconciled` 事件保存此承诺值；Visual authority 与顶层 `stage_completed` 仅能引用同一个值。
3. `VisualStageAdapter.inspect()` 使用严格 artifact schema（固定字段集合、固定 stage run、operation/job/result/内容哈希），拒绝额外字段和任何不匹配。
4. 服务层将 artifact 的实际 SHA-256 与已签名 operation 的内容承诺值逐字节对比；`get_status()`、`advance()`、`doctor()` 均应失败为 `STAGE_INTEGRITY_FAILED`。
5. 新增对抗回归：保留签名三元组、只改变 artifact 其他字节并重算所有后续普通 event hash，三个入口都必须拒绝。

真实 Provider 验证仍无法在本工作区完成：当前 M2.1 是离线 `MockVisualProvider` 边界，且未提供可授权的真实 Provider 凭据或执行器。修复通过独立验收后，才应在隔离账户、最小预算和人工审批下验证真实 Provider 的结果承诺值映射。

## 复现本地检查

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest tests/test_production_m2_1_mock_visual.py tests/test_production_m2_contracts.py tests/test_production_run.py -q
# 当前结果：59 passed
git diff --check
```

全量 `pytest -q` 在本机的 120 秒任务时限内没有返回结果；该超时不代表通过或失败。可在没有该时限的本地终端执行它，并记录总数、跳过项和耗时。
