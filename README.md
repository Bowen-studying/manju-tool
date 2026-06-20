# manju-tool 🎬

> AI 漫剧生成工具 — 把小说变成 AI 短视频的完整流水线。

从选文、AI 审核、文本清洗、同音改名，到视频素材组装，一条命令跑完。

## 功能

| 模块 | 说明 |
|------|------|
| `manju select` | 自动选文（doutenet 爬取 + 三标签过滤 + 违禁词检查） |
| `manju clean` | 文本清洗+违禁词审核 |
| `manju review` | AI 深度审核（剧情、人物、爆点、钩子） |
| `manju rename` | 同音改名（自动识别主角性别 + 常用字优先替换） |
| `manju pipeline` | 一键跑完完整流程 |

## 安装

```bash
pip install manju-tool
```

## 快速使用

```bash
# 选文
manju select

# 清洗+审核一篇小说
manju clean novel.txt
manju review novel.txt

# 一键全流程
manju pipeline
```

## 文件结构

```
manju-tool/
├── manju/
│   ├── __init__.py
│   ├── cli.py          # CLI 入口
│   ├── select.py       # 选文模块
│   ├── clean.py        # 清洗+违禁词
│   ├── review.py       # AI 审核
│   ├── rename.py       # 同音改名
│   └── data/           # 词库数据
├── tests/
├── pyproject.toml
└── README.md
```

## 开发

```bash
git clone https://github.com/Bowen-studying/manju-tool
cd manju-tool
pip install -e .
```

## 许可证

MIT
