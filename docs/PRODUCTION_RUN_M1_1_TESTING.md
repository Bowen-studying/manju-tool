# ProductionRun M1.1 测试说明

日期：2026-08-10

## 修复范围

- 恢复在 `run_created` 后中断的运行：恢复时先写入唯一的 `run_started` 事件，再调度阶段。
- 覆盖 7 个定义的崩溃窗口：contract 写入前、`run_created` 后、`stage_scheduled` 后、子 Agent 完成后、`stage_completed` 后、pause 请求后、`run_completed` 后。
- 对 failed manifest 同样校验 manifest 合同、checkpoint、trace 及其顶层 authority 引用；仅完整性验证通过的业务失败才使用 `STORYBOARD_FAILED`。
- `doctor` 保持原有 `status` 字段，并新增 `integrity_status` 与 `run_status`，分别表达数据完整性和业务运行结果。
- 新增 `tools/package_production_run_m1.ps1`，生成 UTF-8 无 BOM、使用 `/` 路径分隔符的 `SHA256SUMS.txt`。

## 独立验收

独立 `gpt-5.6-sol`、medium reasoning 共执行 2 轮：

1. 第 1 轮发现 failed manifest 在缺少 storyboard 时过早返回，且打包清单保留 Windows 反斜杠。
2. 修复后第 2 轮通过：专项 `40 passed`；篡改 failed manifest/checkpoint 返回 `STAGE_INTEGRITY_FAILED`；删除已记录 failed stage authority 后，`status` 与 `doctor` 均报告完整性失败；WSL `sha256sum -c SHA256SUMS.txt` 退出码为 0。

## 本地测试结果

```powershell
python -m compileall -q manju tests
python -m pytest tests/test_production_run.py -q
# 40 passed

python -m pytest -q
# 385 passed, 2 skipped

python -m manju.cli project --help
python -m manju.cli run --help
python -m manju.cli status --help
python -m manju.cli pause --help
python -m manju.cli doctor --help
git diff --check
```

两个 skip 为当前 Windows 环境没有创建 symlink 的权限；这不是功能失败。独立 WSL 复验执行完整套件为 `386 passed, 1 skipped`，该 skip 是 Windows junction 专属测试在 Linux 的平台条件跳过。

测试未读取 `.manju.env`，未调用真实 Provider 或付费 API。

## 交付包复验

```powershell
.\tools\package_production_run_m1.ps1 -Destination C:\path\to\manju-tool-production-run-m1.1-final
Get-FileHash -Algorithm SHA256 -LiteralPath .\SHA256SUMS.txt
```

在 Linux/WSL 中，可在交付包根目录直接执行：

```sh
sha256sum -c SHA256SUMS.txt
```
