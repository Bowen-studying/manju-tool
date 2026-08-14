# ProductionRun M2.4.1 真实 Provider 测试说明

日期：2026-08-13

## 本次修复

M2.4.1 收口 M2.4 的两条真实 Provider 安全边界：

- `succeeded` 和 `failed` 的 final fee 均要求 Provider 声明 `verified_cost=True`；否则在结算前进入 `OPERATION_OUTCOME_UNKNOWN`，不得把费用写入账本。
- 非 `mock` 的受签 `provider_profile` 必须由进程构建的 `VisualProviderRegistry` 解析为唯一 Provider 实例。项目、Approval、Grant 和 Operation 中只有 profile 名称，不包含 endpoint、allowlist 或凭据。
- 未提供 registry 的非 mock profile 在 submit 前以 `OPERATION_CONTRACT_INVALID` 停止；它不能回退到默认 mock 或任意注入的 Provider。
- mock fixture 的失败结果显式使用 `test_fixture` 成本来源；unknown 结果显式保持 unknown，因此离线测试不会模拟真实账单。
- `test_fixture` 仅属于内置、精确类型的 `MockVisualProvider`，且只能在未使用 registry 的 `mock` profile 中出现。任何 registry Provider、Mock 子类或已签收 receipt 伪报该来源，都会以 `OPERATION_CONTRACT_INVALID` 停止。

## 离线验证

在项目根目录，以已经安装依赖的 Python 执行：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$Python = 'C:\path\to\python.exe'
& $Python -m pytest -q `
  tests/test_production_m2_4_provider_safety.py `
  tests/test_production_m2_3_binary_receipts.py `
  tests/test_production_m2_2_provider_contracts.py `
  tests/test_production_m2_1_mock_visual.py `
  tests/test_production_m2_contracts.py
& $Python -m pytest -q
```

本版本本机分组验证结果：聚焦 `49 passed`；全量结果见本次复验报告。后两项 skip 属于平台相关用例。

独立 sol-medium 追加第 1 轮只读验收：通过。其确认生产 profile 无法在实时 reconcile 或缓存 receipt 路径使用 `test_fixture`，精确内置 mock 的离线路径仍可用；未读取凭据、未联网、未调用真实 Provider。

## 真实测试前提

仍需要真实测试，但只有在部署方提供合格异步 Provider 时才能开始。当前同步生图网关不具备 M2.4 合同，不适用于此轮测试。

部署方需要在进程配置中构建 `VisualProviderRegistry`，将一个已批准 profile 映射到一个 Provider 实例。该实例应以受控环境注入凭据，并同时满足：

1. `submit(operation_id, idempotency_key, request)` 对同一键只创建或返回同一收费 job。
2. submit 返回的 job ID 能在重启后查询。
3. `reconcile(job_id)` 只读取 job 与已存在 artifact，不创建新的收费任务。
4. 成功和失败的 final fee 来自 Provider response 或可验证 ledger，且实现声明 `verified_cost=True`。
5. endpoint 与 artifact origin allowlist 在部署配置中固定，不在 project.json、Approval、Grant 或 request descriptor 中出现。

## 执行顺序

1. 使用隔离账号、单 operation 小额预算和公开无敏感 request 初始化项目；确认审批页只显示 profile、operation kind 与 prompt/model/size/quality/n 等已批准公开字段。
2. 先以错误或缺失 profile 启动一次，确认 submit 为 0，且返回 `OPERATION_CONTRACT_INVALID`。
3. 以正确 registry profile 批准并签发 grant；在提交前后、job 接受后、artifact 下载前后、receipt 前后分别重启。每个窗口核对上游 job 数恒为 1；receipt 生成后 reconcile 为 0。
4. 分别验证成功、失败但已收费、unknown cost、币种不符和超预算。失败但已收费同样需要可验证 final fee；unknown 或超预算应保持 blocked。
5. 导出恢复副本，并校验 source、contract、events、state、authority、receipt、published artifact 和 pending artifact 的 SHA-256 清单。HMAC key 只由进程受控环境提供。

本任务无法代替你执行上述真实调用：当前没有已批准的异步 Provider profile、账单查询接口、隔离凭据或预算授权。请勿发送生成式健康探测；仅可使用不收费的 Provider status/job 查询接口检查部署连通性。
