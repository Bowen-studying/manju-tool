# ProductionRun M6.0：旧项目显式导入测试说明

## 范围

M6.0 只处理一个明确传入的旧 CLI `storyboard.json` 文件，并把它复制到一个全新的 ProductionRun 项目目录。导入过程：

- 只读读取单个普通文件；不扫描旧目录，不修改旧文件或旧目录；
- 有界读取并拒绝 symlink、junction、Windows reparse point；
- 验证 JSON、仓库兼容的 v1/v2 storyboard 结构、场景/镜头数量、文本、节点和嵌套深度上限；
- 保留输入 JSON 字节，不做 normalize、猜测或静默修复；
- 生成项目内 storyboard 副本、`legacy_import_manifest.json` 和 `legacy_import_authority.json`，并记录 SHA-256 与 `unverified` 来源状态；
- 通过 M6.0 不可变合同 `production-m6.0-v1` 创建已完成的 `storyboard` 起点，后续离线 voice_script/video_prompt 可直接继续；
- 导入本身不建立 Provider、不联网、不产生 approval、grant、call_reserved、call_submitted、call_settled 或 call_reconciled 事件。

## CLI

```powershell
manju project import-legacy C:\path\to\old\storyboard.json -o C:\path\to\new-project --json
```

可选的 `--voice-script` 和 `--video-prompts` 只写入后续阶段合同；导入命令本身仍然离线。输出 DTO 只含项目/运行状态、哈希和阶段信息，不含本机绝对路径或密钥。

应用层 API 为 `manju.production.import_legacy_storyboard()`。它支持为后续合同配置离线 voice、visual/video 阶段，但 M6.0 不替它们执行 Provider 调用。

## 安全与幂等边界

- 目标目录不存在或为空时才可发布；非空目标、无关 `project.json` 和篡改过的已导入项目均拒绝覆盖。
- 完整且未篡改的同源目标会被识别并返回相同快照，不新增事件。
- 构建在同级 staging 目录，完成事件链、artifact graph、合同、doctor 校验后才原子发布；发布前中断不会留下目标项目。
- 已发布项目的 storyboard、manifest、authority、事件链或合同发生变化时，status/doctor fail-closed。

## 针对性测试

```powershell
.\.venv\Scripts\pytest.exe -q tests/test_production_m6_0_import_legacy.py
.\.venv\Scripts\pytest.exe -q tests/test_production_m5_0_video_prompt.py
```

链接测试在当前 Windows 没有创建权限时会跳过；reparse-point 模拟测试仍应执行。M6.0 无需真实 Provider、API key 或网络凭据。

## M7 发布验收边界

M7 才进入固定评测集、真实 API 冒烟和跨平台发布验收，至少需要：真实视频/视觉 Provider profile、预算与审批策略、跨 Windows/Linux 的 link/reparse 实测、Provider 异步任务对账、重复提交与 outcome-unknown 恢复、最终媒体验收和发布包校验。M6.0 的 `unverified` 旧产物不能直接当作已授权或已发布媒体。
