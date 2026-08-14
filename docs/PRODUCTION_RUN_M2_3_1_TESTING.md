# ProductionRun M2.3.1 测试说明

日期：2026-08-13

## 修复范围

M2.3.1 修复已完成 visual 阶段在新建 `ProductionService` 后不能恢复验证的问题。

`VisualStageAdapter.inspect()` 需要项目临时 HMAC key 来验证 receipt。此前 signer 仅在 `advance()` 的 visual 推进分支配置；而 `get_status()`、`run_until_blocked()` 的首个状态读取、`advance()` 的预校验、`doctor()` 都可能在此之前检查 completed visual authority，导致新的 service 实例无法验证已经完成的项目。

修复将 signer 配置统一放入 `_validate_stage_authority()`：事件链包含 visual terminal 时，在首次 `visual_adapter.inspect()` 前按项目的 `hmac_key_id` 通过注入的 key provider 配置 signer。没有 visual terminal 的项目不请求该 key。

## 离线验证

从项目根目录，使用已有依赖的 Python：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$Python = 'C:\path\to\python.exe'
& $Python -m compileall -q manju
& $Python -m pytest -q `
  tests/test_production_m2_1_mock_visual.py `
  tests/test_production_m2_3_binary_receipts.py `
  tests/test_production_m2_2_provider_contracts.py `
  tests/test_production_m2_contracts.py
& $Python -m pytest -q
```

结果：聚焦 `36 passed`；全量 `421 passed, 2 skipped`。

新增回归会先完成一个带 HMAC receipt 的 visual run，然后为每个入口单独创建新 service：

- `get_status()` 返回 `completed`；
- `run_until_blocked()` 返回 `completed`；
- `advance()` 停在 `completed`；
- `doctor()` 返回 `passed`；
- 每条路径均断言 Provider `submit_counts == {}` 且 `reconciles == []`。

独立 `sol-medium` 只读验收：通过。其聚焦套件为 `36 passed`，并额外确认错误 HMAC key fail-closed、无 visual terminal 的项目不提前请求 signer；未读取凭据或调用真实 Provider。

## 真实产物的零付费复验边界

`manju-m2.3-real-paid-20260813` 导出可审计最终发布结果：其 SHA256 清单完整，PNG 字节 SHA、receipt 和 authority 绑定一致。但该导出不含 receipt 指向的私有 `.visual-pending-*.bin`，且不含 HMAC 验证 key。因此它不是可直接启动 service 的完整恢复副本，不能在不补造证据/不注入密钥的前提下完成零付费实测。

本次没有执行真实 Provider 或付费请求。后续零付费真实恢复复验应从原始完整项目副本进行，保留 pending artifact，并由运行环境受控注入同一 HMAC key；仅调用 `get_status()`、`run_until_blocked()`、`advance()` 与 `doctor()`，断言 Provider reconcile 计数为 0。

## 交付包验证

交付包不包含 `.venv`。进入包根目录后，将 `$Python` 指向项目依赖已安装的外部解释器，再执行本说明的命令。`SHA256SUMS.txt` 覆盖包内其余文件；测试从包根目录运行，不能以开发工作区的结果替代。
