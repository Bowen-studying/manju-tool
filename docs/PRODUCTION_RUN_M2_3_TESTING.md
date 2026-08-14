# ProductionRun M2.3 测试说明

日期：2026-08-13

## 本次交付

M2.3 将视觉 Provider 的外部交互收敛到 `VisualStageAdapter`：

- `ProductionService` 只调用 `submit_operation`、`observe_operation` 与 `publish_result`，不直接访问 Provider；
- 成功 observation 先把经过 Provider 合同校验的原始字节原子写入 stage 私有 pending 文件，再写入 receipt；
- receipt 绑定 operation、job、result SHA-256、MIME、大小、usage、实际金额、币种与费用状态，并由项目的临时 HMAC 密钥签名；
- 重启发生在 receipt 之后、`call_settled` 之前时，只复用本地 receipt/pending 字节，不会重新 reconcile 或下载；
- 发布按受控 MIME 映射使用固定文件名（JSON/PNG/JPEG/WebP/octet-stream），原子写入后再次校验 SHA-256；远程文件名不会影响路径；
- reserve 使用 Grant 的签名最大金额；最终费用必须为 Grant 币种。实际费用超预占或 Grant 时，已签名费用记录被保留，run 以 `BUDGET_EXCEEDED`（退出码 4）阻断；unknown cost 仍阻断，failed 的 final fee 仍计入。

## 已完成的离线验证（交付包自身）

交付包不携带开发机的 `.venv`。进入桌面交付包根目录后，将 `$Python` 指向已安装项目依赖的外部解释器，再执行：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$Python = 'C:\path\to\python.exe'
& $Python -m compileall -q manju
& $Python -m pytest -q
```

结果：`420 passed, 2 skipped`，且 `python -m compileall -q manju` 与 `import manju.production` 均成功。

M2.3 聚焦测试：

```powershell
& $Python -m pytest -q `
  tests/test_production_m2_3_binary_receipts.py `
  tests/test_production_m2_1_mock_visual.py `
  tests/test_production_m2_2_provider_contracts.py `
  tests/test_production_m2_contracts.py
```

结果：`35 passed`。覆盖二进制 PNG 原样发布、receipt 后重启复用、receipt 的路径/SHA/usage/金额/币种/费用状态篡改拒绝，以及超预算阻断且保留 receipt。

独立 `sol-medium` 只读复验：通过；其运行聚焦套件 `35 passed`、主 ProductionRun 回归 `40 passed`。交付包修订后，独立复验还核对了包内 `manju` 60 个与 `tests` 17 个 Python 文件和工作区逐一 SHA-256 一致，清单 94 项完整，且最终副本不含测试缓存。审计未修改文件、未读取凭据、未调用真实 Provider。

WSL 冒烟前需先在其独立 Python 环境安装项目依赖（至少包括 `requirements.txt` 和 pytest）。本次 WSL 环境仅有 Python 3.14，缺少 `langgraph` 和 pytest，因此未在该环境重复全量套件；Windows 的包内全量验证已完成。

## 桌面交付包校验

桌面包根目录的 `SHA256SUMS.txt` 覆盖其中所有其他文件。可在 PowerShell 执行：

```powershell
$root = '桌面包的完整路径'
Get-Content (Join-Path $root 'SHA256SUMS.txt') | ForEach-Object {
  $parts = $_ -split '  ', 2
  if ((Get-FileHash (Join-Path $root $parts[1]) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $parts[0]) {
    throw "SHA256 mismatch: $($parts[1])"
  }
}
```

除清单校验外，也应从包根目录执行本节的编译、导入和 pytest 命令；不能用开发工作区的测试结果替代交付包结果。

## 尚未执行的真实 Provider 验证

没有执行真实 Provider、真实凭据或付费调用。执行前需要用户明确提供：目标 Provider 与版本、隔离测试账号、最大可消耗预算/币种、允许模型、已批准 HTTPS endpoint 与 artifact origin allowlist、凭据注入方式，以及审批人。

获得授权后，在隔离账户中应验证：

1. 建立具体 Provider mapping，使用 HTTPS endpoint 和只包含测试 CDN 的 artifact allowlist；凭据仅通过受控运行环境注入，不写入项目或包。
2. 为单次 operation 设定小额 Grant，确认 submit 的 idempotency key 稳定；在 Provider 接收后、receipt 前后、settled 前后、publish 前后分别中断并重启。
3. 使用至少一个真实二进制 artifact，核验发布文件的 MIME 映射名、字节 SHA-256、authority 和 receipt HMAC。
4. 分别验证成功、Provider failed 且有 final fee、unknown cost、币种不匹配与实际金额超预算；确认没有自动重试付费 operation。
5. 完成后导出不含凭据的事件链、authority、receipt 和 SHA256SUMS，供独立复验。
