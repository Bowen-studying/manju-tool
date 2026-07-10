# 0.6.0 详细实施报告

## 目标

本次升级修复真实使用审计中发现的 API、恢复、缓存、配音、导出和 CLI 问题，并完成 v2 分镜资产闭环。

## 主要修改

### API 与网络

- API 根地址统一补全，修复 LLM 缺少 `/chat/completions` 和 TTS 重复 `/v1`。
- HTTPError 先于 URLError 处理，保留状态码和响应正文。
- LLM、图片、语音、视频增加 429/5xx/网络错误退避重试。
- 图片响应支持 URL 和 `b64_json`；本地图片使用 multipart `/images/edits`。
- 视频支持同步 URL、嵌套 URL、多种任务 ID/状态和正确恢复地址。

### 分镜与长文本

- v2 数据结构分离 `visual/audio/prompts/assets/status`。
- 长文本按约 4 万字符切块摘要，不再向每场重复发送整篇原文。
- 每个输入指纹对应独立 `stages/run_<fingerprint>`，避免新旧阶段混杂。
- 摘要、规划和逐场结果均可续跑；损坏 JSON 会自动尝试修复。
- 小说改编按 5 万字符分块并合并人物/场景，不再截掉结尾。
- 内容量统计同时考虑中文、英文、数字和 JSON。

### 素材与缓存

- 文件名统一跨平台清洗，处理 Windows 保留名和非法字符。
- 图片、音频、视频使用内容指纹元数据；提示词、台词、模型或参数变化会重新生成。
- 批量图片按场景建立参考图，降低跨场背景和服装污染。
- 图片、配音、逐镜视频路径及状态回写 `storyboard.json`。
- 视频超时为每个任务保存独立恢复文件。

### 配音

- 每个角色稳定分配不同 Edge/API 音色。
- 情绪分类每 40 句分块，模型漏答的句子逐条使用关键词回退，不再静默全部变“平静”。
- `edge-tts` 加入正式依赖，默认免费后端安装后即可使用。

### CLI 与 pipeline

- 单次图片/视频/配音失败返回非零退出码。
- 批量任务只有全部成功才返回成功。
- pipeline 支持 `--storyboard-json`、`--resume` 和显式 `--render-videos`。
- `--speak`/`--render-videos` 的依赖选项会提前校验。
- 使用生成函数返回的真实路径，修复标题清洗后找不到剧本。
- 默认运行目录带时间戳；独立剧本输出存在时生成版本化文件，不覆盖旧结果。
- Excel/PDF/使用指南缺失会明确失败，不再假装全流程完成。

### 导出安全

- PDF 中标题、台词、提示词全部 HTML 转义。
- HTML PDF 后端关闭外部和本地 URL 资源读取。
- Windows 缺少相关系统动态库时自动使用跨平台后端生成 PDF。
- 使用指南同时生成 PDF 和 DOCX，并返回可验证结果。

## 文件变更索引

- 新增 `manju/utils/runtime.py`：共用安全与状态基础设施。
- 新增 `manju/pipeline/storyboard_schema.py`：v2 schema 与兼容层。
- 新增 `manju/pipeline/storyboard_stages.py`：分块、续跑、多阶段生成。
- 重构 `generate_image.py`、`generate_video.py`、`generate_voice.py`。
- 更新 `storyboard.py`、`voice.py`、`video.py`、`adapt.py`、`create.py`、`cli.py`。
- 更新 `formats.py`、`use_guide.py`、`config.py`、`ai.py`。
- 新增 `manju/utils/reportlab_pdf.py`，在 Windows 缺少额外系统动态库时仍能导出中文 PDF。
- 新增 `requirements.txt`；删除与项目实际依赖严重不一致的旧 `uv.lock`。
- 新增 `tests/test_runtime_and_apis.py`、`tests/test_pipeline_integration.py`、`tests/test_github_compliance.py` 并扩展原回归测试。

## 行为兼容

- 旧 v1 分镜仍可被 voice/video/Excel/Word 读取。
- 原命令名称保留。
- 会产生费用的逐镜视频默认关闭，必须显式使用 `--render-videos`。
- pipeline 的范围是“生成制作素材”；最终剪辑、字幕设计和混音继续由剪辑软件完成，README 已明确说明。

## 验证命令

```powershell
python -m compileall -q manju tests
python -m unittest discover -s tests -v
python -m manju.cli --help
python -m pip wheel . --no-deps --no-build-isolation -w dist
git diff --check
```

最终验收结果：

- Python 3.12 完整导出环境：42 项测试全部通过，其中包含上传内容与输出规范检查。
- 实际生成了 `storyboard.xlsx`、分镜 Word、配音/视频 PDF、`使用指南.pdf` 和 `使用指南.docx`。
- 模拟兼容 PDF 后端缺少系统动态库，确认跨平台后端仍能成功输出 PDF。
- CLI 根命令、`storyboard --help`、`pipeline --help` 均通过。
- 成功构建 0.6.0 wheel；包元数据包含 `click`、`openpyxl`、`python-docx`、`reportlab`、`edge-tts`，隔离安装后版本读取和 `manju --help` 均通过。

未由本次代码强行掩盖的供应商差异、模型随机性、成本、素材一致性、最终剪辑范围和依赖锁定问题，详见 `docs/KNOWN_LIMITATIONS.md`。

## 发布说明

本报告随 0.6.0 源码一并发布，接收方应以当前文件内容和测试结果为准。
