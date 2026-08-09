<div align="center">

# 🎬 manju-tool

### 把小说或一个故事想法，整理成可继续制作的 AI 漫剧素材

剧本 · 分镜表 · 配音脚本 · 图片 · 视频提示词 · 视频片段

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/Version-0.6.0-F59E0B)](#)
[![License](https://img.shields.io/badge/License-MIT-22C55E)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-268%20passed-14B8A6)](#验证与文档)

**适合小说作者、短视频创作者、编剧和 AI 内容制作团队。**

</div>

---

## 🌟 它能帮你做什么？

你可以从一篇小说、一个现成剧本，甚至一句故事想法开始。工具会把内容逐步整理成后续制作需要的文件。

```mermaid
flowchart LR
    A[小说或故事想法] --> B[结构化剧本]
    B --> C[逐场分镜]
    C --> D[配音脚本]
    C --> E[图片与视频提示词]
    D --> F[音频素材]
    E --> G[图片与视频片段]
```

| 你提供 | 工具可以生成 |
|---|---|
| 小说文本 | 人物、场景、对白和结构化剧本 |
| 一个故事想法 | 完整剧本和场景安排 |
| 剧本 | 分镜表、画面描述、对白、音效和提示词 |
| 分镜 | 配音脚本、音频、视频提示词和可选素材 |

> [!TIP]
> 第一次使用时，推荐从 `manju pipeline --novel "你的小说.txt"` 开始。它会按顺序完成剧本整理、分镜、配音脚本和视频提示词。

> [!NOTE]
> `pipeline` 的定位是“制作素材流水线”。最终成片中的字幕样式、转场、混音、调色和时间线剪辑，继续在你熟悉的后期环境中完成。

---

## 🚀 零基础快速开始

整个过程分为四步：

1. 安装 Python
2. 下载并安装本工具
3. 填写 API 配置
4. 运行第一条命令

### 第一步：准备 Python

需要 Python 3.10 或更高版本。

打开终端后输入：

```bash
python --version
```

看到类似 `Python 3.10.11`、`Python 3.11.x` 或 `Python 3.12.x`，即可继续。

<details>
<summary><strong>Windows 提示：终端在哪里？</strong></summary>

1. 打开解压后的 `manju-tool` 文件夹。
2. 点击文件夹顶部的地址栏。
3. 输入 `powershell`，按回车。
4. 新窗口会自动定位到当前文件夹。

如果系统还没有 Python，可从 Python 官方网站安装。安装界面中勾选将 Python 加入 PATH 的选项，后续命令会更顺畅。

</details>

### 第二步：下载并安装

#### 方法 A：下载 ZIP，适合第一次接触代码仓库的用户

1. 点击 GitHub 页面右上区域的 **Code**。
2. 选择 **Download ZIP**。
3. 解压下载文件。
4. 在解压后的 `manju-tool` 文件夹中打开终端。
5. 运行：

```bash
python -m pip install -e .
```

#### 方法 B：使用 Git

```bash
git clone https://github.com/Bowen-studying/manju-tool.git
cd manju-tool
python -m pip install -e .
```

安装完成后检查命令：

```bash
manju --help
```

如果终端找不到 `manju`，可以使用同等写法：

```bash
python -m manju.cli --help
```

### 第三步：填写 API 配置

API 可以理解为“让本工具调用 AI 能力的连接信息”。最少需要一组文字模型配置，用于整理剧本、生成分镜和分析对白。

在个人主目录创建名为 `.manju.env` 的文本文件：

- Windows：`C:\Users\你的用户名\.manju.env`
- macOS / Linux：`~/.manju.env`

Windows 可以直接运行：

```powershell
notepad $HOME\.manju.env
```

将下面内容复制进去，再把示例值替换成你自己的信息：

```env
# 文字模型：基础流程必填
LLM_API_KEY=your-key
LLM_API_BASE=https://your-api.example.com/v1
LLM_MODEL=your-model-name

# 图片生成：使用 --image-api 时填写
MANJU_IMAGE_API_KEY=your-key
MANJU_IMAGE_API_BASE=https://your-api.example.com/v1
MANJU_IMAGE_MODEL=your-model-name
MANJU_IMAGE_REFERENCE_MODE=single
MANJU_IMAGE_MAX_PARALLEL=4
MANJU_IMAGE_TIMEOUT_SECONDS=300
MANJU_IMAGE_DOWNLOAD_TIMEOUT_SECONDS=120
MANJU_IMAGE_ASPECT_MODE=cover

# 图像 Agent 的独立视觉复核；未配置时会暂停并转人工检查
MANJU_VISION_API_KEY=your-key
MANJU_VISION_API_BASE=https://your-vision-api.example.com/v1
MANJU_VISION_MODEL=your-vision-model

# 视频生成：使用 --render-videos 或 generate 时填写
MANJU_VIDEO_API_KEY=your-key
MANJU_VIDEO_API_BASE=https://your-api.example.com/v1
MANJU_VIDEO_MODEL=your-model-name

# 自选语音服务：可选
MANJU_VOICE_API_KEY=your-key
MANJU_VOICE_API_BASE=https://your-api.example.com/v1
MANJU_VOICE_MODEL=your-model-name
```

所有地址均可填写 API 根地址，工具会自动补全常见请求路径。也可以直接填写完整接口地址。

> [!IMPORTANT]
> 小说、提示词和参考素材会发送给配置的第三方服务。处理未公开内容前，请先确认服务商的数据保留与隐私政策。

> [!WARNING]
> 文字、图片、语音和视频服务可能分别计费。逐镜视频生成默认关闭，只有显式加入 `--render-videos` 才会调用视频生成服务。

### 第四步：完成第一次运行

准备一个 UTF-8 编码的小说文本，例如 `我的小说.txt`，放在当前文件夹中，然后运行：

```bash
manju pipeline --novel "我的小说.txt"
```

运行结束后，终端会显示输出位置。默认输出通常位于当前文件夹中的：

```text
manju-output/
└── 日期_时间/
    ├── storyboard/
    │   ├── storyboard.json
    │   ├── storyboard.md
    │   └── storyboard.xlsx
    ├── voice_scripts.pdf
    ├── video_prompts.pdf
    └── 使用指南.pdf
```

如果运行中断，再次执行同一条命令即可从已完成阶段继续：

```bash
manju pipeline --novel "我的小说.txt" --resume
```

---

## 🧭 按你的起点选择命令

### 我有一篇小说

```bash
manju pipeline --novel "小说.txt"
```

适合希望一次得到剧本、分镜、配音脚本和视频提示词的用户。

### 我已经有结构化剧本

```bash
manju pipeline --script "剧本.json"
```

工具会从分镜阶段继续。

### 我已经有分镜

```bash
manju pipeline --storyboard-json "storyboard.json"
```

工具会直接生成配音脚本和视频提示词。

### 我只有一个故事想法

```bash
manju pipeline
```

终端会逐项询问剧名、类型、主角和核心冲突。填写完成后自动进入后续流程。

### 我只想完成其中一步

| 目标 | 命令示例 |
|---|---|
| 小说整理成剧本 | `manju adapt "小说.txt"` |
| 从想法创作剧本 | `manju create` |
| 剧本生成分镜 | `manju storyboard "剧本.json"` |
| 分镜生成配音脚本 | `manju voice "storyboard.json"` |
| 分镜生成视频提示词 | `manju video "storyboard.json"` |
| 一段文字生成图片 | `manju image "画面描述"` |
| 一段文字生成视频 | `manju generate "动作和画面描述"` |
| 一段文字生成语音 | `manju speak "需要朗读的文字"` |

---

## 🎨 生成图片和视频素材

### 生成单张图片

```bash
manju image "雨夜古城，少女撑伞回望，电影感光影"
```

使用本地参考图：

```bash
manju image "保持人物特征，改为转身回眸" -i "reference.png"
```

指定尺寸和文件名：

```bash
manju image "雪山木屋，窗内暖光" --size 1024x768 -n "scene_01"
```

### 批量生成图片

创建 `prompts.txt`，每行写一个画面描述：

```text
古城城门，清晨薄雾
同一角色走入长街
同一角色停在茶楼门前
```

运行：

```bash
manju image --batch "prompts.txt"
```

### 图像主管 Agent（审批后生图）

图像 Agent v4 的正常阶段路由由代码状态机决定，不再调用文字模型来选择下一工具。工作流状态
以 `stages/visual_agent/runs/<run_id>/events.jsonl` 哈希事件链为恢复权威；`state.json`、
`visual_agent_run.json`、`visual_review.json` 和 `cost_plan.json` 只是可重建投影。`run_id` 是与
调用参数无关的 UUID，调用合同哈希只用于判断能否续跑，参数不兼容时会创建新 run，旧事件链不覆盖。

图像 Agent 先根据 storyboard v2 规划风格板、人物身份、三视图、必要的表情姿势、场景母版和关键道具。第一次运行只生成计划与审批文件，不会调用生图 API：

```bash
manju image-agent "storyboard.json" -o "visual_output"
```

打开 `visual_output/approvals/current.json`，按其中的 `decision_path` 找到本次运行的审批文件，把 `decision` 改为 `approve`。v4 模板已经预填本次 `reviewed_item_ids` 和图片指纹，这些绑定字段应保持原值。基础资产候选锁定阶段还需要在 `selections` 中选择候选 ID。审批文件按 `run_id` 隔离，旧运行不会被新运行误用。之后显式授权已审批范围内的付费调用并续跑：

```bash
manju image-agent "storyboard.json" -o "visual_output" --resume --image-api
```

如果主管因明确的语义不确定性进入 `needs_review`，默认续跑不会越过人工门。确认后可审计地恢复：

```bash
manju image-agent "storyboard.json" -o "visual_output" --resume --image-api \
  --resume-needs-review --resume-reviewer "导演姓名" \
  --resume-note "已核对当前分镜、锁定资产和账本，确认可继续。"
```

该选项只允许恢复主管主动停止；预算、审批拒绝或技术错误仍需解决原始原因。

已有完整图片产物只需重新执行视觉审核时，使用零付费 vision-only 模式：

```bash
manju image-agent "storyboard.json" -o "visual_output" --recheck-vision --no-image-api
```

该模式会审核全部场景组后统一汇总。发现 blocking 时状态为 `needs_review`，并写出
`visual_repair_plan.json`；它不会创建零预算的伪 regenerate 审批，也不会调用生图 API。
确认 repair plan 后，先创建精确到 blocking 镜头的新成本审批：

```bash
manju image-agent "storyboard.json" -o "visual_output" \
  --repair-vision-blockers --no-image-api
```

人工填写当前审批文件后，再显式授权并续跑同一个 repair run：

```bash
manju image-agent "storyboard.json" -o "visual_output" \
  --repair-vision-blockers --image-api
```

修复模式只重生 repair plan 中的镜头，每个场景组使用新 grant；继承账本中的旧 grant
仅作为历史记录，不能授权新调用。`--recheck-vision` 与 `--repair-vision-blockers` 不能同时使用。
修复后的视觉复审若仍有 blocking，不提供普通人工覆盖入口，而以 `vision_repair_blocked`
结束；系统将旧计划归档到 `repair_history`，并按当前真实 blocking 镜头生成新的 proposed plan。
下一轮 `--repair-vision-blockers` 会创建新的 run 和成本审批，必须使用新的 grant。定向修订会把
上一版镜头作为主编辑参考，锁定资产只作为辅助参考，避免重生成时破坏未涉及区域。
相邻镜头成片会作为时序连续性辅助参考，用于保持人物、道具、场景和动作状态；不会取代当前
失败镜头的主编辑参考。`counters.model_calls` 和 `counters.tool_steps` 是跨 run 累计审计值，
每个新 recheck/repair 使用独立的 `run_budget_usage` 执行预算门禁。视觉复核若因预算或技术原因
未完成，repair plan 标为 `verification_incomplete` 且不可审批，不会输出空计划或沿用旧 blocker。

既有完成结果若只缺少修订 provenance，可纯本地回填，不重新复审或生图：

```bash
manju image-agent "storyboard.json" -o "visual_output" \
  --reconcile-metadata --no-image-api
```

该模式只根据当前 state、图片 sidecar 和 `assets/reference_boards` 中的 targeted revision
manifest 回填 `previous_shot_reference_*`、`revision_reference_board` 和 `temporal_context_*`。
它不调用主管、视觉或图像 API，不修改质量门禁、累计调用计数或付费账本，并且不能与
`--recheck-vision`、`--repair-vision-blockers` 或 `--image-api` 同时使用。

既有运行升级、付费产物收口，或删除兼容投影后的纯本地重建，使用：

```bash
manju image-agent "storyboard.json" -o "visual_output" \
  --reconcile-paid-artifacts --no-image-api
```

该命令优先从当前 run 的事件链恢复，并以付费账本核对授权和用量；模型、视觉和生图调用均为
零。事件链校验失败会直接停止，不会退回读取投影或猜测状态。

每项基础资产默认生成 3 个候选，视觉模型只排序，最终锁定由人决定。共享同一组锁定参考的候选、同场景组镜头和定向重生默认最多 4 路并行；资产依赖、审批、锁版和复核仍按顺序执行。每个付费任务会在调用前写入 run 专属账本并扣减审批额度，单张完成即提交，恢复时接管已写盘文件；失败后的新调用必须重新审批。每个镜头只引用实际出镜人物、该场景母版和该镜关键道具；单参考供应商的本地参考板会完整保留这些引用，不再静默截断。可用 `MANJU_IMAGE_MAX_PARALLEL` 或 `--image-parallelism` 在 1–16 之间调整。

`--size auto` 会根据 storyboard 画幅，从 `MANJU_IMAGE_SUPPORTED_SIZES` 中选择最接近的供应商请求尺寸。供应商若仍返回其他比例，默认 `cover` 居中裁切以避免填充带；也可选择 `contain_blur` 或 `strict`。原始尺寸、最终尺寸和处理方式写入图片旁的 `.manju.json`。基础资产锁定后按场景组逐批审批；所有审批必须包含审核人、完整审核项和当前图片指纹，`auto`/`ok` 等占位内容无效。每组最多自动修正一次。没有视觉 API 时必须经过人工语义确认，最终 `visual_agent_run.json` 会明确记录 `completed_with_manual_override` 和 `quality_gate`，不会伪装成视觉模型验收。退出码 `3` 表示等待审批，`2` 表示需要人工质量判断。等待或复核期间 pipeline 不会进入配音和视频。

### 直接生成视频片段

```bash
manju generate "雪夜森林，一匹白马缓慢走过，镜头平稳跟随"
```

使用参考图：

```bash
manju generate "人物缓慢抬头，眼神逐渐坚定" -i "reference.jpg"
```

### 在完整流程中生成素材

加入图片、配音音频和逐镜视频：

```bash
manju pipeline --novel "小说.txt" --image-api --speak --render-videos
```

其中逐镜视频通常耗时更长，也可能产生较高费用。可以先运行基础流程，确认分镜后再生成素材。

---

## 🎙️ 配音怎么使用？

只生成配音脚本：

```bash
manju voice "storyboard.json"
```

同时生成音频：

```bash
manju voice "storyboard.json" --speak
```

单独朗读一段文字：

```bash
manju speak "欢迎来到今天的故事"
```

调整语速、声调和音量：

```bash
manju speak "快跑！" --speed 1.4 --pitch 7 --volume 8
```

分镜配音会结合上下文推断情绪，并为不同角色稳定分配音色。无对白镜头也会保留在配音表中，便于和分镜逐行核对。

---

## 📦 你会得到哪些文件？

| 文件 | 打开方式 | 用途 |
|---|---|---|
| `*_script.json` | 文本编辑器 | 结构化剧本，供后续命令读取 |
| `storyboard.xlsx` | 表格软件 | 查看和修改逐镜内容 |
| `storyboard.md` | 文本编辑器 | 快速浏览分镜 |
| `storyboard.json` | 文本编辑器 | 项目的主要状态文件 |
| `voice_scripts.pdf` | PDF 阅读器 | 配音顺序、角色、台词和情绪参数 |
| `video_prompts.pdf` | PDF 阅读器 | 每个镜头的中英文视频提示词 |
| `audio/` | 音频播放器 | 生成的配音文件 |
| `images/` | 图片查看器 | 生成的镜头图片 |
| `videos/` | 视频播放器 | 生成的视频片段 |
| `使用指南.pdf` | PDF 阅读器 | 本次输出的后续制作流程 |

`storyboard.json`、阶段目录和 `.manju.json` 元数据共同支持续跑与缓存。继续制作期间，建议保留它们。

---

## 🧩 常用选项

| 选项 | 含义 |
|---|---|
| `-o 路径` | 指定输出文件夹 |
| `--resume` | 复用已完成阶段和未变化素材 |
| `--max-scenes 数量` | 指定目标场景数 |
| `--engine workflow` | 使用冻结的 LangGraph v6 固定流程，便于对照 |
| `--engine agent` | 使用主管 Agent 自主选择分镜工具，并保存 SQLite 检查点与行动轨迹 |
| `--image-engine agent` | 使用审批驱动的图像主管 Agent；默认仍为 `legacy` |
| `--agent-max-steps 数量` | 主管 Agent 工具步骤预算，默认 40 |
| `--agent-max-calls auto\|数量` | 主管 Agent 模型调用预算；默认 `auto`，按场景数、分块数和修订闭环计算（通常 20–36），可显式填写更高正整数 |
| `--agent-max-revisions 数量` | 每场定向修订上限，默认 2 |
| `--image-api` | 在分镜阶段调用图片生成服务 |
| `--speak` | 生成配音音频 |
| `--render-videos` | 按镜头生成视频片段 |
| `-i 图片路径` | 给图片或视频生成提供参考图 |
| `--batch 文件` | 从文本文件批量读取内容 |

查看任意命令的完整选项：

```bash
manju pipeline --help
manju storyboard --help
manju image --help
```

LangGraph 当前是本地试验引擎，默认仍使用原有 `legacy` 流程。低成本试跑示例：

```bash
manju storyboard "sample_story.txt" --engine agent --max-scenes 1 -o demo_output
```

Agent 模式不是固定的“规划→生成→审核”流程。主管模型通过严格 JSON 动作协议选择分析、规划、生成、组合审计或定向修订工具；未知参数会成为可恢复协议错误。Python 强制执行工具白名单、证据有效性、预算和完成条件。原文会形成带稳定 beat ID 的 Source Model，场景和镜头通过 `source_beat_ids`、`visible_character_ids` 与 `temporal_relations` 建立语义关联。模型提出的 blocking 问题必须同时引用有效原文证据和当前 storyboard JSON 路径，否则降为 advisory，不触发自动修订。它会额外生成 `review.json`、`agent_run.json`、`agent_trace.jsonl` 和 `stages/agent/checkpoints.sqlite`。当状态为 `needs_review` 时，分镜文件仍会保存，但 CLI 返回退出码 2，pipeline 会在所有媒体调用前停止。固定 v6 对照流程使用 `--engine workflow`，检查点位于 `stages/workflow/`。相同输入和参数再次使用 `--resume` 时，会从本地检查点恢复。

---

## 🩺 常见问题

<details>
<summary><strong>终端提示找不到 manju</strong></summary>

先确认安装命令执行成功：

```bash
python -m pip install -e .
```

随后可以用模块方式运行：

```bash
python -m manju.cli --help
python -m manju.cli pipeline --novel "小说.txt"
```

</details>

<details>
<summary><strong>提示缺少 LLM API 配置</strong></summary>

检查个人主目录中的 `.manju.env` 是否包含以下三项，并确认文件名开头带有英文句点：

```env
LLM_API_KEY=your-key
LLM_API_BASE=https://your-api.example.com/v1
LLM_MODEL=your-model-name
```

保存文件后重新运行命令。

</details>

<details>
<summary><strong>生成到一半中断</strong></summary>

使用相同输入再次运行，并保留 `--resume`：

```bash
manju pipeline --novel "小说.txt" --resume
```

已经完成的规划、场景和未变化素材会被复用。

</details>

<details>
<summary><strong>图片或视频接口报错</strong></summary>

依次检查：

1. API Key 是否有效。
2. API Base 是否为正确的根地址或完整接口地址。
3. 模型名是否已填写并可用。
4. 账户是否有可用额度。
5. 当前服务是否支持参考图、任务轮询或对应尺寸。

建议先用一条短提示词测试连接，再运行批量任务。

</details>

<details>
<summary><strong>输出文件在哪里？</strong></summary>

每次运行都会在终端中打印完整输出路径。基础流程默认写入当前目录下的 `manju-output`，并按日期和时间分开保存。

也可以自行指定：

```bash
manju pipeline --novel "小说.txt" -o "D:\我的项目\第一集"
```

</details>

---

## 💬 使用前常见疑问

### 这是完全免费的工具吗？

本项目代码采用 MIT 许可证。你接入的文字、图片、语音或视频服务可能收费，具体以服务商规则为准。

### 可以直接生成最终成片吗？

它负责把故事整理成制作素材，并可生成图片、音频和视频片段。最终时间线、字幕、转场、混音和调色仍需要后期整理。

### 长篇小说会被截断吗？

长内容会分块处理并合并结果，结尾也会纳入分析。分镜阶段会保存中间产物，便于恢复。

### 修改提示词后会重新生成吗？

会。图片、音频和视频使用内容指纹判断变化。内容与参数保持一致时复用缓存，发生变化时重新生成。

### 旧版分镜还能继续使用吗？

常用旧版分镜可以继续进入配音、视频提示词和导出流程。新项目会使用 2.0 数据结构保存画面、声音、提示词、素材和状态。

---

## 📚 验证与文档

本版本已通过 42 项自动测试，覆盖：

- 长文本处理和结尾保留
- 多阶段分镜与断点续跑
- 新旧分镜兼容
- 图片、语音、视频请求和缓存
- 同步及异步视频任务恢复
- Excel、Word、PDF 和使用指南导出
- 上传内容、隐私声明和输出规范检查

本地验证：

```bash
uv sync --extra planner --extra test
uv run pytest -q
uv run python -m compileall -q manju tests
```

混合规划与离线渲染的验证依赖 `planner`，测试运行器由 `test` extra 提供；请同时安装两者，避免手动安装未声明的 pytest 版本。

兼容旧环境时仍可运行：

```bash
python -m compileall -q manju tests
python -m unittest discover -s tests -v
```

进一步了解：

- [`docs/REVIEW_GUIDE.md`](docs/REVIEW_GUIDE.md)：版本审查与验证入口
- [`docs/IMPLEMENTATION_0.6.0.md`](docs/IMPLEMENTATION_0.6.0.md)：0.6.0 修改说明
- [`docs/API_COMPATIBILITY.md`](docs/API_COMPATIBILITY.md)：API 配置与响应格式
- [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md)：实际使用边界和风险

---

## 📄 License

MIT

<div align="center">

如果这个项目对你的创作有帮助，欢迎点亮 ⭐，也欢迎提交问题和改进建议。

</div>
