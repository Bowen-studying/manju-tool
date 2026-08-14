# ProductionRun M2.5 运行时 Provider 接入测试说明

日期：2026-08-13

## 本次变更

M2.5 补齐了真实 Provider 的进程装配边界：

- 项目初始化支持 `--visual-provider-profile`、`--visual-request-file` 与 `--visual-operation-kind`。项目合同只保存 profile 名称和批准的公开 request；它不保存 endpoint、artifact allowlist、credential 名称或 credential 值。
- `manju run` 在进程启动时读取 `MANJU_PRODUCTION_VISUAL_PROFILES_JSON`，只为项目已签名的 profile 创建 `VisualProviderRegistry`。配置中的 API key 仅通过对应的环境变量读取。`status` 与 `doctor` 是只读路径，即使部署凭据尚未注入也可验证项目与账本。
- 目前支持 `async_http` profile，协议为 M2.2 的 `POST /operations` + `GET /operations/{job_id}`，因此仍受幂等 submit、持久 job、只读 reconcile、verified cost 的 M2.4 门禁保护。
- `manual_sync` profile 表示同步 Provider。自动 ProductionRun 会在任何 Provider 调用前以 `OPERATION_OUTCOME_UNKNOWN` 停止，不能把同步接口伪装成自动可恢复 Provider。
- profile 的 timeout 必须是有限的 `0 < seconds <= 60`；artifact 上限必须为 `1..100 MiB`。NaN、无穷、负数和超限配置会在网络调用前拒绝。

## 部署配置

以下示例仅展示变量名；请在受控进程环境中设置，避免把凭据写入项目、命令行历史或请求 JSON：

```powershell
$env:MANJU_PRODUCTION_VISUAL_PROFILES_JSON = @'
{
  "async-image": {
    "mode": "async_http",
    "base_url": "https://approved-provider.example",
    "api_key_env": "MANJU_ASYNC_IMAGE_KEY",
    "allowed_artifact_origins": ["https://approved-artifacts.example"],
    "timeout_seconds": 15,
    "max_artifact_bytes": 20971520
  }
}
'@
$env:MANJU_ASYNC_IMAGE_KEY = '<injected-secret>'
```

创建项目时，把公开 request 放在单独 JSON 文件，例如：

```json
{"prompt":"public test image","model":"approved-model","size":"1024x1024","n":1}
```

随后执行：

```powershell
manju project init --source .\source.txt --source-type script -o .\project `
  --visual-provider-profile async-image --visual-request-file .\public-request.json `
  --visual-operation-kind image_generation --visual-max-amount 1
manju run .\project\project.json
```

## 自动安全模式测试

开始前准备隔离账号、单 operation 小额预算、审批人、已批准 HTTPS endpoint 和 artifact allowlist。完成批准和 grant 后，验证：

1. 改写 profile、prompt、model 或 size 后，合同或 grant 拒绝，且 submit 为 0。
2. 在 submit 前后、上游 job 接受后、artifact 下载前后、receipt 前后重启；同一 idempotency key 始终对应一个上游收费 job。
3. 成功、失败已收费、unknown cost、币种不符和超预算的状态都符合 M2.4.1 规则；unknown 或超预算保持 blocked。
4. 导出恢复副本，验证 events、receipt、pending artifact 和 published artifact 的 SHA-256。

## 当前同步网关

当前同步生图网关可以配置为：

```json
{"sync-image":{"mode":"manual_sync"}}
```

但 `manju run` 会在网络调用前停止。它没有上游幂等键、持久 job 查询或账单字段，不能通过自动恢复验收。若要实际使用它，需要单独的人工作业流程：人工确认每个 POST、崩溃后只进行账单或供应商记录对账、不自动重发；在未取得可验证费用前保持 blocked。本 M2.5 自动 runner 不执行该流程。

## 离线验证

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$Python = 'C:\path\to\python.exe'
& $Python -m pytest -q tests/test_production_m2_5_runtime_profiles.py
& $Python -m pytest -q
```

本任务不读取已有凭据，也不发送健康探测或真实 Provider 请求。
