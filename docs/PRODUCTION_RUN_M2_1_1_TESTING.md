# ProductionRun M2.1.1 测试说明

日期：2026-08-11

## 结论

M2.1.1 已通过第一轮独立 `sol-medium` 只读验收。该里程碑仍是离线 mock visual 边界，不包含真实或付费 Provider 调用。

## 本次修复

已签名的 `call_settled` / `call_reconciled` 中 `result_fingerprint` 现在等于最终 `mock_image.json` 文件全部字节的 SHA-256（`sha256:<hex>`）。

公开 artifact 使用严格 schema：

- `schema_version`
- `operation_id`
- `provider_job_id`

artifact 不存储自身指纹，避免哈希循环。authority 保存 artifact 哈希及 operation 三元组；服务层将该三元组与唯一已签名成功 operation 交叉校验。`atomic_write_json` 固定写入 UTF-8 + LF，确保签名承诺跨平台稳定。

## 本地测试结果

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest tests/test_production_m2_1_mock_visual.py tests/test_production_m2_contracts.py tests/test_production_run.py -q
# 60 passed

.\.venv\Scripts\python.exe -m pytest -q
# 405 passed, 2 skipped

.\.venv\Scripts\python.exe -m manju.cli run --help
.\.venv\Scripts\python.exe -m manju.cli approvals --help
git diff --check
```

两个跳过项受 Windows symlink 权限条件影响。

## 对抗覆盖

- 修改 artifact 的任意现有字段或加入额外字段；
- 保留已签名 operation 三元组并同步重算 authority、`stage_completed` 和后续普通 hash chain；
- 替换为另一份自洽 artifact / authority；
- storyboard 与 visual 全终态链篡改；
- Grant 过期时 reserve、submit、reconcile 前阻断；
- failed、outcome_unknown、signed reconciliation 与 provider 接受后的崩溃恢复。

上述 visual 篡改会使 `get_status()`、`advance()` 报 `STAGE_INTEGRITY_FAILED`，`doctor()` 返回同一完整性失败。

## 独立验收

独立 `sol-medium` 运行聚焦套件得到 `60 passed`，并确认：

- 签名 `result_fingerprint` 与实际 artifact 字节完全一致；
- artifact、authority 和 operation 均执行严格字段集合校验；
- 不改敏感签名、仅改 artifact 字节并重算普通事件链仍会被拒绝；
- 未修改工作区，未调用真实 Provider。

## 未完成的外部测试

真实 Provider 的结果承诺值映射需要后续真实执行器、隔离账户、最小预算、人工审批和用户提供的授权凭据。本工作区当前没有这些条件，因此未执行该测试。
