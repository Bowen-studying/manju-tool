# ProductionRun M2.7：同步 Provider 的人工执行与合同费率结算

日期：2026-08-14。M2.7 支持**人工、一次性**执行同步生图接口，不改变 M2.5 对自动 runner 的拒绝规则。

M2.7 有两种互斥结算模式：`provider_evidence` 需要可验证的 Provider 账单证据；`contractual_tariff` 依据审批前冻结、Grant 签名绑定的合同费率完成结算。合同费率验证的是预先约定价格，**不是**真实上游成本、Provider 实际扣费或账单金额。

## 安全模型

1. `manju prepare-manual` 只在已审批、已签发 grant、且部署 profile 声明为 `manual_sync` 时产生签名执行包。包只包含已审批的公开 request，不包含 endpoint、凭据名或凭据值。
2. `manju-provider-worker` 在受控机器使用 `--state-dir` 原子建立本机 claim。相同 claim token 第二次执行会失败；写入 `dispatch_started` 后，任何异常都必须人工查账，绝不自动重发。
3. worker 默认仅运行 fixture；真实请求必须同时给出 `--execute-http-once --confirm EXECUTE-ONCE`。API key 仅由 worker 进程环境使用。
4. `manju import-manual-result` 导入结果后，操作固定为 `outcome_unknown`、项目 `blocked`；即使图片存在也不能发布。
5. `manju reconcile-manual` 仅适用于 `provider_evidence`，需要真实账单证据文件、SHA-256、审核人、Provider reference、实际金额和币种。缺文件、哈希不符、币种不符或超预算都会拒绝或保持阻断。
6. `manju settle-manual-contractual-tariff` 仅适用于 `contractual_tariff`。该命令没有金额、币种、账单文件或 Provider reference 参数，只能采用 Grant 内签名费率表的 `amount`、`currency`、`tariff_id` 和哈希。

跨机器复制执行包会绕过“本机” claim 互斥，因此只能在受控流程中由指定操作员处理；该风险不能由没有上游幂等键的同步 API 自动消除。

## 离线验证

不设置任何真实 Provider 凭据，执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m2_6_manual_sync.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_m2_7_contractual_tariff.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_run.py tests/test_production_m2_contracts.py tests/test_production_m2_1_mock_visual.py tests/test_production_m2_2_provider_contracts.py tests/test_production_m2_3_binary_receipts.py tests/test_production_m2_4_provider_safety.py tests/test_production_m2_5_runtime_profiles.py tests/test_production_m2_6_manual_sync.py
```

这些测试只运行 fixture，不启动 HTTP server，也不读取 `.manju.env` 或调用真实 Provider。

## 受控真实测试（由操作员执行）

前提：隔离账号、单次小预算、已审批 prompt、受控 worker 主机、账单可下载，以及部署侧设置：

```powershell
$env:MANJU_PRODUCTION_HMAC_KEY = '<operator HMAC key>'
$env:MANJU_PRODUCTION_VISUAL_PROFILES_JSON = '{"sync-image":{"mode":"manual_sync","base_url":"https://approved.example/v1","api_key_env":"SYNC_IMAGE_KEY","timeout_seconds":60,"max_artifact_bytes":20971520}}'
$env:SYNC_IMAGE_KEY = '<worker-only provider key>'
```

上述值只应保留在受控进程环境中，不应出现在 project、执行包或结果包内。

1. 正常推进项目，批准并签发 grant。读取 `status --json` 的 `last_event_hash`。
2. 生成执行包：

```powershell
manju prepare-manual .\project.json --expected-last-event-hash <hash> --json
```

3. 在受控 worker 主机、空的 state directory 中执行一次：

```powershell
manju-provider-worker .\manual-<id>.json --state-dir D:\manju-worker-state --output-dir D:\manju-result --execute-http-once --confirm EXECUTE-ONCE
```

若此处崩溃或超时：应保留 claim，先检查 Provider 账户/账单，并以人工证据决定后续处置；系统不会自动重试。

4. 将 result package 带回项目机，导入：

```powershell
manju import-manual-result .\project.json --result-file D:\manju-result\manual_result.json --package-dir D:\manju-result --expected-last-event-hash <hash>
```

5. `provider_evidence` 模式：下载或导出 Provider 账单证据文件至 package directory，并在人工核对金额后结算：

```powershell
manju reconcile-manual .\project.json --operation-id <operation-id> --actual-amount <minor-units> --currency USD --provider-reference <invoice-or-ledger-id> --reviewer <name> --evidence-file D:\manju-result\invoice.json --package-dir D:\manju-result --expected-last-event-hash <hash>
manju run .\project.json --json
```

金额使用项目的最小货币单位，与既有 `maximum_amount` 一致。

## 合同费率模式

项目初始化时显式冻结合同费率。`tariff_id` 应引用已批准的价格版本；金额使用项目的最小货币单位，且不能超过项目预算：

```powershell
manju project init --source .\source.txt --source-type script --output-dir .\project --visual-provider-profile sync-image --visual-request-file .\request.json --visual-max-amount 5 --visual-settlement-mode contractual_tariff --visual-contractual-tariff-id image-contract-2026-08 --visual-contractual-tariff-amount 3
```

按人工执行步骤完成 `prepare-manual`、worker 和 `import-manual-result` 后，使用 Grant 冻结的价格结算：

```powershell
manju settle-manual-contractual-tariff .\project.json --operation-id <operation-id> --expected-last-event-hash <hash>
manju run .\project.json --json
```

最终事件会记录 `cost_source=contractual_tariff` 与 `cost_disclosure=pre_agreed_price_not_upstream_actual_cost`。其含义是合同约定结算，不代表上游 Provider 的真实成本。
