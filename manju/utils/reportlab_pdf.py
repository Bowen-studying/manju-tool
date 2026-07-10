"""Pure-Python PDF exporters used when WeasyPrint is unavailable.

ReportLab's built-in STSong-Light CID font keeps Chinese output portable and
avoids the GTK/Pango runtime dependency that WeasyPrint has on Windows.
"""

from __future__ import annotations

import os
from html import escape


def _components():
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise ImportError("reportlab not installed") from exc

    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ManjuBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=8.5,
        leading=12,
        wordWrap="CJK",
    )
    heading = ParagraphStyle(
        "ManjuHeading",
        parent=body,
        fontSize=16,
        leading=22,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    section = ParagraphStyle(
        "ManjuSection",
        parent=body,
        fontSize=12,
        leading=17,
        spaceBefore=10,
        spaceAfter=5,
    )
    return {
        "A4": A4,
        "PageBreak": PageBreak,
        "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate,
        "Spacer": Spacer,
        "Table": Table,
        "TableStyle": TableStyle,
        "colors": colors,
        "cm": cm,
        "body": body,
        "heading": heading,
        "section": section,
    }


def _text(value) -> str:
    return escape(str(value or "")).replace("\n", "<br/>")


def _paragraph(c, value, style="body"):
    return c["Paragraph"](_text(value), c[style])


def _table(c, headers, rows, widths):
    content = [[_paragraph(c, value) for value in headers]]
    content.extend([[_paragraph(c, value) for value in row] for row in rows])
    table = c["Table"](content, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(c["TableStyle"]([
        ("BACKGROUND", (0, 0), (-1, 0), c["colors"].HexColor("#2F5496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), c["colors"].white),
        ("GRID", (0, 0), (-1, -1), 0.35, c["colors"].HexColor("#BBBBBB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _document(c, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return c["SimpleDocTemplate"](
        path,
        pagesize=c["A4"],
        leftMargin=1.2 * c["cm"],
        rightMargin=1.2 * c["cm"],
        topMargin=1.2 * c["cm"],
        bottomMargin=1.2 * c["cm"],
        title="manju-tool export",
        author="manju-tool",
    )


def write_data_pdf(data: dict, path: str, title: str = ""):
    """Write voice/video prompt data without native system libraries."""
    c = _components()
    story = [_paragraph(c, title or data.get("title", ""), "heading")]

    if "lines" in data:
        lines = data.get("lines", [])
        story.append(_paragraph(c, f"配音脚本，共 {len(lines)} 句对白", "section"))
        rows = [
            [
                line.get("shot_id", ""), line.get("character", ""), line.get("text", ""),
                line.get("emotion", ""), line.get("speed", ""), line.get("pitch", ""),
                line.get("volume", ""),
            ]
            for line in lines
        ]
        story.append(_table(
            c,
            ["镜头", "角色", "台词", "情绪", "语速", "声调", "音量"],
            rows,
            [36, 48, 190, 48, 42, 42, 42],
        ))
    elif "shots" in data:
        shots = data.get("shots", [])
        story.append(_paragraph(c, f"视频提示词，共 {len(shots)} 个镜头", "section"))
        rows = [
            [
                shot.get("shot_id", ""), shot.get("shot_type", ""), shot.get("duration", ""),
                f'{shot.get("camera_movement_original", "")} → {shot.get("camera_movement_en", "")}',
                shot.get("dialogue", ""),
                "\n\n".join(filter(None, [
                    str(shot.get("video_prompt_cn", "")),
                    str(shot.get("video_prompt_en", "")),
                ])),
            ]
            for shot in shots
        ]
        story.append(_table(
            c,
            ["镜头", "景别", "时长", "运镜", "对白", "中文提示词 / English Prompt"],
            rows,
            [36, 42, 36, 80, 90, 164],
        ))

    _document(c, path).build(story)


def write_guide_pdf(path: str, files: dict, title: str = ""):
    """Write the generated-asset usage guide as a portable PDF."""
    c = _components()
    story = [
        _paragraph(c, title or "使用指南", "heading"),
        _paragraph(c, "以下为生成的全部交付物，请按顺序使用。"),
        _paragraph(c, "文件清单", "section"),
    ]
    descriptions = {
        "storyboard_xlsx": ("分镜表", "检查画面、对白和提示词"),
        "voice_pdf": ("配音脚本", "按角色与情绪生成或核对配音"),
        "video_pdf": ("视频提示词", "逐镜生成视频素材"),
    }
    rows = [
        [value, descriptions[key][0], descriptions[key][1]]
        for key, value in files.items() if key in descriptions
    ]
    story.append(_table(c, ["文件", "用途", "下一步"], rows, [155, 90, 203]))

    steps = [
        ("第一步：确认分镜", "检查镜头顺序、人物一致性、对白、景别与时长。"),
        ("第二步：生成图片", "按镜头生成静态图片；内容变化后缓存会自动失效。"),
        ("第三步：生成视频", "使用参考图和视频提示词逐镜生成视频素材。"),
        ("第四步：配音", "按角色固定音色、情绪、语速和音量生成音频。"),
        ("第五步：剪辑合成", "在剪辑软件中对齐视频、配音、字幕和音效后导出成片。"),
    ]
    for heading, body in steps:
        story.append(_paragraph(c, heading, "section"))
        story.append(_paragraph(c, body))

    _document(c, path).build(story)
