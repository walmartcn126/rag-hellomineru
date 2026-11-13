"""生成验证用样例文档：文本 PDF、表格 PDF、扫描件 PDF、DOCX、PPTX、XLSX、CSV。"""

from __future__ import annotations

import csv
from pathlib import Path

import fitz
from openpyxl import Workbook
from openpyxl.styles import Font
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.util import Inches
from docx import Document

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "samples"

CJK_FONT = "china-s"


def _text_page(doc: fitz.Document, title: str, paragraphs: list[str], page_no: int) -> None:
    page = doc.new_page(width=595, height=842)
    y = 70
    page.insert_text((60, y), title, fontname=CJK_FONT, fontsize=22)
    y += 40
    page.insert_text((60, y), f"第 {page_no} 页 · HelloMinerU 解析验证样例", fontname=CJK_FONT, fontsize=10)
    y += 30
    for heading, body in zip(("1. 背景", "2. 方法", "3. 结论"), paragraphs):
        page.insert_text((60, y), heading, fontname=CJK_FONT, fontsize=15)
        y += 24
        rect = fitz.Rect(60, y, 535, y + 58)
        page.insert_textbox(rect, body, fontname=CJK_FONT, fontsize=11)
        y += 74
    page.insert_text((60, 800), f"Page {page_no}", fontname="helv", fontsize=9)


def make_text_pdf(path: Path) -> None:
    doc = fitz.open()
    _text_page(
        doc,
        "HelloMinerU 原生数字文档",
        [
            "本文件由程序生成，用于验证解析器对原生数字版 PDF 的文本抽取能力。",
            "段落中包含中文、English words、数字 12345 以及标点符号，用来检查阅读顺序与文本还原。",
            "如果解析结果与原文一致，说明文本层抽取正常，无需使用 OCR。",
        ],
        1,
    )
    _text_page(
        doc,
        "第二章 公式与代码",
        [
            "行内公式示例：E = mc^2，以及一个独立公式段落 y = ax + b。",
            "代码片段示例：print('hello mineru')，用于检查特殊字符是否被破坏。",
            "本页用于验证多页文档的页码、段落顺序与标题层级识别。",
        ],
        2,
    )
    doc.save(path)
    doc.close()


def make_table_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), "表格解析验证", fontname=CJK_FONT, fontsize=22)
    page.insert_text((60, 100), "下表包含 4 列 5 行，用于验证表格结构还原。", fontname=CJK_FONT, fontsize=11)

    rows = [
        ["序号", "名称", "数量", "备注"],
        ["1", "PDF 文档", "12", "原生数字版"],
        ["2", "扫描件", "5", "需要 OCR"],
        ["3", "DOCX", "8", "Office 文档"],
        ["4", "XLSX", "3", "表格数据"],
    ]
    x0, y0, col_w, row_h = 60, 130, 118, 34
    for row_idx, row in enumerate(rows):
        for col_idx, cell in enumerate(row):
            rect = fitz.Rect(
                x0 + col_idx * col_w,
                y0 + row_idx * row_h,
                x0 + (col_idx + 1) * col_w,
                y0 + (row_idx + 1) * row_h,
            )
            page.draw_rect(rect, color=(0.2, 0.2, 0.2), width=0.8)
            page.insert_textbox(
                rect + (4, 8, -4, -4), cell, fontname=CJK_FONT, fontsize=11
            )
    page.insert_text((60, y0 + len(rows) * row_h + 40), "表格下方的段落：用于验证文本与表格的阅读顺序。", fontname=CJK_FONT, fontsize=11)
    doc.save(path)
    doc.close()


def make_scanned_pdf(path: Path, image_path: Path) -> None:
    source = fitz.open()
    _text_page(
        source,
        "扫描件模拟文档",
        [
            "本页先渲染为图片，再嵌入新的 PDF，因此没有文本层，只能通过 OCR 识别。",
            "OCR 结果应当包含“扫描件模拟文档”标题以及这一段中文文本。",
            "如果 MinerU 的 method=auto 能自动切换到 ocr，说明自动判定逻辑有效。",
        ],
        1,
    )
    pix = source[0].get_pixmap(dpi=150)
    pix.save(str(image_path))
    source.close()

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(0, 0, 595, 842), filename=str(image_path))
    doc.save(path)
    doc.close()


def make_docx(path: Path, image_path: Path) -> None:
    document = Document()
    document.add_heading("HelloMinerU DOCX 样例", level=0)
    document.add_heading("1. 概述", level=1)
    document.add_paragraph("这是一个用于验证 DOCX 轻量解析的示例文档，包含标题、段落、列表、表格与图片。")
    document.add_heading("2. 列表", level=2)
    document.add_paragraph("第一项：文本解析", style="List Bullet")
    document.add_paragraph("第二项：表格解析", style="List Bullet")
    document.add_paragraph("第三项：图片解析", style="List Bullet")
    document.add_heading("3. 表格", level=2)
    table = document.add_table(rows=3, cols=3)
    data = [["解析器", "速度", "适用场景"], ["PyMuPDF", "快", "原生数字版"], ["MinerU", "慢", "扫描件/复杂版式"]]
    for row_idx, row in enumerate(data):
        for col_idx, value in enumerate(row):
            table.cell(row_idx, col_idx).text = value
    document.add_heading("4. 图片", level=2)
    document.add_picture(str(image_path), width=Inches(4))
    document.save(path)


def make_pptx(path: Path, image_path: Path) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "HelloMinerU PPTX 样例"
    slide.placeholders[1].text = "第一页：验证标题与正文抽取\n支持多行文本\n以及列表样式"

    slide2 = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide2.shapes.title.text = "第二页：表格与图片"
    rows, cols = 3, 2
    table = slide2.shapes.add_table(rows, cols, Inches(0.6), Inches(1.6), Inches(6), Inches(1.6)).table
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "结果"
    table.cell(1, 0).text = "文本抽取"
    table.cell(1, 1).text = "正常"
    table.cell(2, 0).text = "表格抽取"
    table.cell(2, 1).text = "正常"
    slide2.shapes.add_picture(str(image_path), Inches(0.6), Inches(3.6), width=Inches(4))
    presentation.save(path)


def make_xlsx(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "解析能力对比"
    sheet["A1"] = "解析器"
    sheet["B1"] = "文本"
    sheet["C1"] = "表格"
    sheet["D1"] = "公式"
    for cell in ("A1", "B1", "C1", "D1"):
        sheet[cell].font = Font(bold=True)
    rows = [
        ("PyMuPDF", "是", "是", "否"),
        ("MinerU", "是", "是", "是"),
        ("python-docx", "是", "是", "否"),
    ]
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, value in enumerate(row, start=1):
            sheet.cell(row=row_idx, column=col_idx, value=value)
    sheet.merge_cells("A6:D6")
    sheet["A6"] = "合并单元格示例：说明与备注"

    second = workbook.create_sheet("样例数据")
    second.append(["序号", "数值"])
    for index in range(1, 6):
        second.append([index, index * 100])
    workbook.save(path)


def make_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["编号", "文档类型", "是否原生", "推荐引擎"])
        writer.writerow([1, "PDF", "是", "pymupdf"])
        writer.writerow([2, "扫描件", "否", "mineru"])
        writer.writerow([3, "DOCX", "是", "docx"])


def make_image(path: Path) -> None:
    image = Image.new("RGB", (640, 320), color=(248, 250, 252))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 620, 300), outline=(37, 99, 235), width=3)
    draw.rectangle((60, 220, 160, 280), fill=(59, 130, 246))
    draw.rectangle((200, 160, 300, 280), fill=(96, 165, 250))
    draw.rectangle((340, 100, 440, 280), fill=(147, 197, 253))
    draw.rectangle((480, 60, 580, 280), fill=(191, 219, 254))
    draw.text((210, 40), "Figure 1: parsing benchmark", fill=(15, 23, 42))
    image.save(path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = OUT_DIR / "sample_figure.png"
    make_image(image_path)
    make_text_pdf(OUT_DIR / "sample_text.pdf")
    make_table_pdf(OUT_DIR / "sample_table.pdf")
    make_scanned_pdf(OUT_DIR / "sample_scan.pdf", OUT_DIR / "sample_scan_page.png")
    make_docx(OUT_DIR / "sample.docx", image_path)
    make_pptx(OUT_DIR / "sample.pptx", image_path)
    make_xlsx(OUT_DIR / "sample.xlsx")
    make_csv(OUT_DIR / "sample.csv")
    for file in sorted(OUT_DIR.iterdir()):
        print(f"{file.name}\t{file.stat().st_size} bytes")


if __name__ == "__main__":
    main()
