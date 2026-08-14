# ProductionRun M2.0.1 测试说明

日期：2026-08-11

## 修复范围

M2.0.1 修复 `outcome_unknown` 的顶层纵向控制流：

- `ProductionScheduler` 将 `blocked` 作为终止状态，绝不继续调度或完成 run；
- `ProductionService.run_until_blocked()` 原样返回 `blocked` snapshot；
- `manju run --json` 因而输出稳定 DTO 并以退出码 4 退出；
- `call_reconciled` 只可精确匹配此前 `settled/outcome_unknown` 的同一 operation；服务层会在写事件前校验，避免错误请求污染事件链；
- reconciliation 后仅当没有其他未知 operation 时恢复 `running`。

本里程碑仍只使用离线事件链与 mock，不读取 `.manju.env`，不调用真实 Provider 或付费 API。

## 自动化覆盖

- blocked scheduler 与 service 停止语义；
- CLI `run --json` 的退出码 4 与不推进断言；
- 双 operation 中一个 unknown 后拒绝另一 operation 的提交；
- unknown operation 的 signed reconciliation 恢复 running；
- service 在 append 前拒绝错误 operation 的 reconciliation；
- M1/M1.1 与 M2.0 的既有回归。

## 本地验证

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m compileall -q manju tests
.\.venv\Scripts\python.exe -m pytest -q
# 396 passed, 2 skipped

.\.venv\Scripts\python.exe -m manju.cli run --help
.\.venv\Scripts\python.exe -m manju.cli approvals --help
.\.venv\Scripts\python.exe -m manju.cli approve --help
.\.venv\Scripts\python.exe -m manju.cli reject --help
.\.venv\Scripts\python.exe -m manju.cli issue-grant --help
git diff --check
```

两个 skip 为 Windows 平台 symlink 权限条件；不是功能失败。

## 独立验收

独立 `gpt-5.6-sol`、medium reasoning 于 M2.0.1 第 1 轮只读通过：

- Production/M2.0.1 专项与 M1 回归：`51 passed`；
- 全量离线：`396 passed, 2 skipped`；
- 复核 blocked 终止、CLI exit 4、reconciliation 的 exact-binding 与恢复条件；
- 未编辑工作区，未调用 Provider。

## 打包复验

```powershell
.\tools\package_production_run_m2_0_1.ps1 -Destination C:\path\to\manju-tool-production-run-m2.0.1-20260811-final
```

在包根目录执行 `sha256sum -c SHA256SUMS.txt`（WSL/Linux），或逐项比较 `SHA256SUMS.txt`（Windows）。
