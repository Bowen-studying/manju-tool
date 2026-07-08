<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/version-0.4.0-orange" alt="Version">
</p>

# manju

从文字到 AI 漫剧，一条命令。

两种方式拿到剧本，然后自动生成分镜、配音脚本、视频提示词。小说改编也行，从零创作也行。还能直接生图和生视频。

## 两行跑起来

```bash
pip install git+https://github.com/Bowen-studying/manju-tool.git
```

配置：

```bash
# LLM（剧本/分镜/配音，必填）
export LLM_API_KEY="your-key"
export LLM_API_BASE="https://your-api.example.com/v1"
export LLM_MODEL="your-model-name"

# 生图和生视频（可选，需要时才配）
export MANJU_IMAGE_API_KEY="your-key"
export MANJU_IMAGE_API_BASE="https://your-api.example.com/v1"
export MANJU_VIDEO_API_KEY="your-key"
export MANJU_VIDEO_API_BASE="https://your-api.example.com/v1"

# 配音（可选，不配则使用免费方案）
export MANJU_VOICE_API_KEY="your-key"
export MANJU_VOICE_API_BASE="https://your-api.example.com/v1"
```

或者写入 `~/.manju.env`，以后不用每次 export。

然后：

```bash
# 从小说改
manju adapt my_novel.txt

# 从零创作
manju create

# 一条命令跑完全程（剧本+分镜+配音+视频提示词）
manju pipeline --novel my_novel.txt

# 生图
manju image "极光下的雪山小屋，暖黄灯光从窗户透出"

# 生视频
manju generate "雪夜中一匹白马缓缓走过森林，电影质感"

# 文字转语音
manju speak "欢迎收听今天的节目"
```

## 它做什么

| 步骤 | 命令 | 输入 | 输出 |
|---|---|---|---|
| 剧本 | `adapt` / `create` | 小说 / 你的想法 | 结构化剧本 |
| 分镜 | `storyboard` | 剧本 | xlsx 分镜表 |
| 配音脚本 | `voice` | 分镜 | pdf 配音脚本 |
| 配音音频 | `voice --speak` | 分镜 | mp3 音频文件 |
| 视频提示词 | `video` | 分镜 | pdf 中英双版视频提示词 |
| 生图 | `image` | 文字描述 | png 图片 |
| 生视频 | `generate` | 文字/文字+图片 | mp4 视频 |
| 文字转语音 | `speak` | 文字 | mp3 音频 |
| 全部 | `pipeline` | 任意起点 | 以上全部 + 使用指南 pdf |

每一步可以单独用。`pipeline` 末尾自动生成使用指南，告诉你怎么把输出用到后续制作里。

## 生图

直接从文字出图，支持图生图保持风格统一。

```bash
# 文生图
manju image "一位古风女子站在樱花树下，衣袂飘飘，柔光"

# 图生图（给一张参考图，保持风格一致）
manju image "同一角色，转身回眸" -i "https://example.com/ref.png"

# 指定尺寸
manju image "..." --size 1024x768

# 指定文件名
manju image "..." -n "scene_01"
```

接入任意兼容 OpenAI Images API 的生图服务。

## 生视频

不经过分镜流程，直接从文字或者文字+图片出 AI 视频。

```bash
# 文字生视频
manju generate "a warrior riding through a snowy forest, cinematic"

# 图生视频（参考图片 + 文字描述）
manju generate "人物缓缓抬头，眼神从迷茫变为坚定" -i "https://example.com/ref.jpg"

# 控制时长和尺寸
manju generate "..." --frames 241 --fps 24 --size 1024x576
```

参数：`--frames`（默认 121≈5 秒），`--fps`（默认 24），`--size`（默认 768x512）。

接入任意兼容的视频生成 API 即可使用。

## 配音

从分镜提取对白，推断情绪，生成配音脚本。加 `--speak` 直接出音频。

```bash
# 只生成配音脚本（PDF）
manju voice storyboard.json

# 生成脚本 + 音频文件
manju voice storyboard.json --speak
```

情绪自动推断：LLM 分析上下文区分"这个'！'是愤怒还是冷笑"。每种情绪自动映射到语速、声调、音量参数。

```bash
# 单独文字转语音（不经过分镜）
manju speak "欢迎大家来到今天的节目" -v xiaoxiao

# 指定情绪参数
manju speak "快跑！" --speed 1.6 --pitch 8 --volume 9 -v yunjian

# 温柔叙述
manju speak "晚安..." --speed 0.7 --pitch 3
```

零配置即可使用。也可在 `~/.manju.env` 中接入自选语音 API。

## 分镜怎么做的

分镜不是简单切镜头。每个镜头包含：景别、构图及情感意图、运镜方式、画面描述、对白、音效、中英文生图和视频提示词。

六个要点：
- **五要素**：主体(年龄/发型/眼型/服装) + 场景(光线/地点/氛围) + 细节(动作/表情/特效) + 风格 + 质量 — 缺一不可
- **角色锚定**：每个角色一份视觉锚定(外貌+服装+体型+标志特征)，全片所有镜头复用
- **表情外放**：用 `[情绪词] + [五官动作分解] + [表演修饰] + [氛围加持]` 公式写表情
- **场景母版**：每个场景定义一份母版(环境+光影+色彩+记忆点)，同场景镜头从这里派生
- **构图带情感**：对角线右下=压抑被困，左上=希望自由；对称=庄严或诡异；框架=窥视感
- **色彩有意图**：每镜头标注暖/冷/中性调以及想传达的情绪

## 配音不只是选标签

LLM 整批分析所有对白，能分辨"这个'！'是愤怒还是冷笑"。

| 情绪 | 语速 | 声调 | 音量 | 场景 |
|---|---|---|---|---|
| 日常 | 1.0 | 5 | 5 | 对话 |
| 愤怒 | 1.5 | 8 | 8 | 爆发 |
| 悲伤 | 0.5 | 3 | 3 | 哽咽 |
| 兴奋 | 1.6 | 9 | 9 | 高能 |
| 恐惧 | 1.7 | 7 | 4 | 战栗 |
| 温柔 | 0.7 | 4 | 4 | 宠溺 |
| 冷漠 | 0.8 | 2 | 5 | 讽刺/反语 |
| 威胁 | 0.8 | 2 | 5 | 警告 |
| 焦急 | 1.4 | 6 | 7 | 急迫 |
| 内心独白 | 0.8 | 5 | 2 | 心声 |

## 输出格式

| 步骤 | 格式 | 说明 |
|---|---|---|
| storyboard | `.xlsx` | 分镜表 |
| voice | `.pdf` | 配音脚本 |
| video | `.pdf` | 视频提示词 |
| image | `.png` | 生成的图片 |
| generate | `.mp4` | 生成的视频 |
| pipeline | `.pdf` | 使用指南 |

## 配置

所有 API 配置项，写入 `~/.manju.env` 或设为环境变量：

```bash
# LLM（剧本/分镜/配音，必填其一）
LLM_API_KEY=sk-...
LLM_API_BASE=https://your-api.example.com/v1
LLM_MODEL=your-model-name

# 生图（可选）
MANJU_IMAGE_API_KEY=sk-...
MANJU_IMAGE_API_BASE=https://your-api.example.com/v1
MANJU_IMAGE_MODEL=your-model-name       # 可选

# 生视频（可选）
MANJU_VIDEO_API_KEY=sk-...
MANJU_VIDEO_API_BASE=https://your-api.example.com/v1
MANJU_VIDEO_MODEL=your-model-name       # 可选
MANJU_VIDEO_POLL_BASE=https://...       # 可选，查询生成进度的地址

# 配音（可选，不配则使用免费方案）
MANJU_VOICE_API_KEY=sk-...
MANJU_VOICE_API_BASE=https://your-api.example.com/v1
MANJU_VOICE_MODEL=your-model-name       # 可选
```

## 里面长什么样

```
manju/
├── cli.py              # 入口，8 个命令
├── pipeline/
│   ├── adapt.py        # 小说 → 剧本
│   ├── create.py       # 想法 → 剧本
│   ├── storyboard.py   # 剧本 → 分镜（支持 --image-api 逐镜生图）
│   ├── voice.py        # 分镜 → 配音参数
│   ├── video.py        # 分镜 → 视频提示词（中英双版）
│   ├── generate_image.py   # 文字/图片 → AI图片
│   ├── generate_video.py   # 文字/图片 → AI视频
│   └── generate_voice.py   # 文字 → 语音
└── utils/
    ├── ai.py           # LLM 调用
    ├── formats.py      # xlsx/docx/pdf 读写
    ├── http.py         # HTTP 工具
    └── use_guide.py    # 使用指南生成
```

依赖：Python 3.10+, click, openpyxl, python-docx, weasyprint。LLM 接入任意 OpenAI 兼容 API。

## License

MIT
