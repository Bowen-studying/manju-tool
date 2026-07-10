# manju-tool 0.6.0 审查入口

本文档用于让收到整个 `manju-tool` 文件夹的人快速理解、运行和审查本地修改。

## 推荐阅读顺序

1. `README.md`：安装、配置和常用命令。
2. `docs/IMPLEMENTATION_0.6.0.md`：每个问题如何修复、具体改了哪些文件。
3. `docs/API_COMPATIBILITY.md`：LLM、图片、语音和视频 API 契约。
4. `docs/KNOWN_LIMITATIONS.md`：实际使用仍需注意的问题和能力边界。
5. `tests/`：可执行的行为证据。

## 安装与验证

```powershell
cd C:\path\to\manju-tool
python -m pip install -e .
python -m compileall -q manju tests
python -m unittest discover -s tests -v
python -m manju.cli --help
```

如果只想检查包能否构建：

```powershell
python -m pip wheel . --no-deps --no-build-isolation -w dist
```

## 重点审查文件

| 文件 | 审查重点 |
|---|---|
| `manju/utils/runtime.py` | API 地址拼接、安全文件名、内容指纹、原子 JSON |
| `manju/utils/ai.py` | LLM 配置、重试、HTTP 错误、响应提取 |
| `manju/pipeline/storyboard_schema.py` | v1/v2 兼容、规范化、结构校验 |
| `manju/pipeline/storyboard_stages.py` | 长文本分块、阶段指纹、续跑、JSON 修复 |
| `manju/pipeline/storyboard.py` | 分镜主入口、导出、生图状态回写 |
| `manju/pipeline/generate_image.py` | URL/base64/本地图片、分组参考图、缓存 |
| `manju/pipeline/generate_voice.py` | TTS 端点、角色音色、缓存、批量结果 |
| `manju/pipeline/generate_video.py` | 同步/异步响应、轮询、恢复文件、缓存 |
| `manju/cli.py` | 退出码、pipeline 依赖参数、素材状态闭环 |
| `manju/utils/formats.py` | v2 导出、HTML 转义、PDF 外部资源阻断 |
| `manju/utils/reportlab_pdf.py` | Windows 无 GTK/Pango 时的纯 Python PDF 回退 |

## 最终验证基线

- Python 3.10：核心测试通过；缺少的可选导出组件会按预期跳过或明确报错。
- Python 3.12 + 完整导出依赖：42 项测试全部通过，其中包含上传内容与输出规范检查。
- 实际生成并校验了 Excel、Word、PDF 和 PDF+DOCX 使用指南。
- 成功构建 `manju_tool-0.6.0-py3-none-any.whl`；隔离安装后 `manju --help` 和版本读取通过。
- `git diff --check` 通过；发布前仍需重新核对暂存区和待提交文件。
- 第三方计费 API 未实际调用；API 协议边界使用 mock 验证。

## 版本控制检查

提交或发布前运行：

```powershell
git status --short --branch
git diff --check
git diff --stat
git diff -- manju/pipeline/storyboard_stages.py
```

## 输出目录说明

- `storyboard.json`：v2 项目主状态。
- `storyboard/stages/run_<fingerprint>/`：可续跑阶段文件。
- `images/*.png.manju.json`、`audio/*.mp3.manju.json`、`videos/*.mp4.manju.json`：内容指纹缓存元数据。
- `video_recovery_<fingerprint>.json`：视频超时后的任务恢复信息。

这些 `.manju.json` 文件用于判断内容是否变化，继续制作时应与对应素材一并保留。
