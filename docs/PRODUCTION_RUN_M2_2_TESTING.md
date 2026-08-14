# ProductionRun M2.2 Provider Transport 测试说明

日期：2026-08-11

## 范围

M2.2 建立真实 Provider 接入前的通用安全传输层。它不配置任何外部服务，也不从项目文件读取 endpoint、凭据或 artifact 下载地址。

新增内容：

- `VisualProvider` 协议：`submit(operation_id, idempotency_key)` 与 `reconcile(provider_job_id)`；
- `ProviderObservation`：统一 outcome、job ID、artifact 字节承诺、媒体类型、实际金额、币种和 usage；
- `HttpJsonVisualProvider`：用于已批准 HTTPS endpoint 的通用 JSON/HTTP transport；
- 每次 submit 传递稳定的 `operation_id` 幂等键；
- 已签名的 settled operation 保存 Provider usage、实际金额、币种和费用状态；失败结果保留 final 费用，未知费用以 `cost_status=unknown` 显式记录；
- artifact URL 仅可来自显式 allowlist，下载不转发 Provider API 凭据，重定向拒绝，大小、MIME、长度和 SHA-256 全部校验；
- HTTP 仅能在测试显式 `allow_insecure_http=True` 时使用。

M2.1.1 的 mock visual artifact、审批、Grant、事件链和内容承诺合同保持不变。

## 本地测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest tests/test_production_m2_2_provider_contracts.py tests/test_production_m2_1_mock_visual.py tests/test_production_m2_contracts.py tests/test_production_run.py -q
# 当前结果：67 passed
```

`test_production_m2_2_provider_contracts.py` 启动标准库本地 HTTP fixture，不访问互联网，不调用真实 Provider。覆盖：

- JSON submit、稳定幂等键和 job ID；
- 成功 observation 的 artifact 字节、媒体类型、长度、SHA-256、usage 和金额；
- artifact allowlist；
- 哈希不一致；
- 缺失幂等键；
- 失败结果的 job ID、费用、币种和 usage 绑定；
- unknown 结果的显式未知费用语义与伪造费用拒绝；
- API Authorization 仅发送给 Provider API，不发送给 artifact 下载；
- 生产默认拒绝 HTTP，测试环境须显式开启。

## 外部验证边界

尚未加入某个服务商的具体 request/response 映射，也未进行付费调用。实施真实 Provider adapter 前需要用户提供：目标服务商、隔离账户、最小预算、允许模型、凭据注入方式和人工审批人。

真实 adapter 还需要把 provider 返回的二进制 artifact 接入阶段发布逻辑，并在 submit 成功、事件落盘前崩溃、状态未知、下载中断和发布中断时完成端到端恢复验证。

## 独立验收

独立 `sol-medium` 第二轮只读验收通过，聚焦套件为 `67 passed`。独立对抗验证了错误 job、失败费用、unknown 费用、错误 MIME/大小、artifact 重定向、API token 转发和 artifact SHA-256。未调用真实 Provider。
