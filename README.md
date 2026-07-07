<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/version-0.2.0-orange" alt="Version">
</p>

# manju

从文字到 AI 漫剧，一条命令。

两种方式拿到剧本，然后自动生成分镜、配音脚本和视频提示词。小说改编也行，从零创作也行。

## 两行跑起来

```bash
pip install git+https://github.com/Bowen-studying/manju-tool.git
```

配置 API key：

```bash
export DEEPSEEK_API_KEY="your-key-here"
```

然后：

```bash
# 从小说改
manju adapt my_novel.txt

# 从零创作
manju create

# 一条命令跑完全程
manju pipeline --novel my_novel.txt
```

## 它做什么

| 步骤 | 命令 | 输入 | 输出 |
|---|---|---|---|
| 剧本 | `adapt` / `create` | 小说 / 你的想法 | 结构化剧本 |
| 分镜 | `storyboard` | 剧本 | xlsx 分镜表 |
| 配音 | `voice` | 分镜 | pdf 配音脚本 |
| 视频提示词 | `video` | 分镜 | pdf 中英双版视频提示词 |
| 全部 | `pipeline` | 任意起点 | 以上全部 + 使用指南 pdf |

每一步可以单独用。`pipeline` 末尾自动生成使用指南，告诉你怎么把输出用到后续制作里。

## 两种方式拿到剧本

### 从小说改：`adapt`

有小说 txt，LLM 读出角色、划分场景、标记对白，输出结构化剧本。

```bash
manju adapt my_novel.txt -g "古风宫斗"
```

### 从零创作：`create`

只有一个想法。交互模式一步步问：类型 → 梗概 → 主角 → 冲突。问完自动写剧本。

```bash
manju create
# 跟着提示走

# 也可以命令行一把梭
manju create --title "末世咖啡店" --genre "末日" \
  --premise "丧尸末日中，一家咖啡店的香气成了最后的安全区" \
  --protagonist "林小满, 25岁, 咖啡师, 脸上有一道疤痕" \
  --conflict "想保住咖啡店却被武装势力盯上"
```

## 直接生视频

不经过分镜/配音流程，`generate` 从文字或文字+图片出 AI 视频。

```bash
# 文字描述
manju generate "a warrior riding a horse through a snowy forest, cinematic"

# 参考图片 + 文字
manju generate "人物缓缓抬头，眼神从迷茫变为坚定" -i "https://example.com/ref.jpg"

# 控制时长和尺寸
manju generate "...description..." --frames 241 --fps 24 --size 1024x576
```

参数：`--frames`（8n+1，≤441），`--fps`（1-60），`--size`（64的倍数）。

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
| pipeline | `.pdf` | 使用指南 |

## 里面长什么样

```
manju/
├── cli.py              # Click 入口，7 个命令
├── pipeline/
│   ├── adapt.py        # 小说 → 剧本
│   ├── create.py       # 想法 → 剧本
│   ├── storyboard.py   # 剧本 → 分镜（支持 --image-api 逐镜生图）
│   ├── voice.py        # 分镜 → 配音参数
│   ├── video.py        # 分镜 → 视频提示词（中英双版）
│   └── generate_video.py  # 文字/图片 → AI视频
└── utils/
    ├── ai.py           # LLM 调用封装
    ├── formats.py      # xlsx/docx/pdf 多格式读写
    ├── http.py         # HTTP 工具
    └── use_guide.py    # 使用指南生成
```

依赖：Python 3.10+, click, openpyxl, python-docx, weasyprint。LLM 适配 DeepSeek / GLM。

## License

MIT
