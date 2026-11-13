"""PyMuPDF：原生数字版 PDF 的轻量解析（文本 + 表格 + 图片 + 页面缩略图）。"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

import fitz

from .base import JobCanceled, ParseContext, ParseResult, register
from .markdown import clean_text, heading, md_table
from .normalize import page_entry


def _base_font_size(doc: fitz.Document) -> float:
    counter: Counter[float] = Counter()
    for page_index in range(min(5, doc.page_count)):
        for block in doc[page_index].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if text:
                        counter[round(float(span.get("size", 10)) * 2) / 2] += len(text)
    if not counter:
        return 10.0
    return counter.most_common(1)[0][0]


def _heading_level(size: float, base_size: float, text: str) -> int:
    if len(text) > 140 or not text:
        return 0
    ratio = size / base_size if base_size else 1.0
    if ratio >= 1.5:
        return 2
    if ratio >= 1.22:
        return 3
    return 0


def _table_markdown(table: Any) -> str:
    try:
        to_markdown = getattr(table, "to_markdown", None)
        if callable(to_markdown):
            body = to_markdown()
            if body and body.strip():
                return body.strip()
    except Exception:
        pass
    try:
        rows = table.extract()
        return md_table(rows)
    except Exception:
        return ""


def _extract_images(
    doc: fitz.Document, page: fitz.Page, page_index: int, ctx: ParseContext
) -> list[tuple[float, float, str, list[float]]]:
    results: list[tuple[float, float, str, list[float]]] = []
    seen: set[int] = set()
    for order, image in enumerate(page.get_images(full=True)):
        xref = int(image[0])
        if xref in seen:
            continue
        seen.add(xref)
        try:
            info = doc.extract_image(xref)
        except Exception:
            continue
        data = info.get("image")
        width, height = info.get("width") or 0, info.get("height") or 0
        if not data or (width and height and width < 40 and height < 40):
            continue
        ext = info.get("ext") or "png"
        name = f"image_p{page_index + 1:04d}_{order + 1}.{ext}"
        (ctx.images_dir / name).write_bytes(data)
        rects = page.get_image_rects(xref)
        if rects:
            bbox = [round(v, 2) for v in rects[0]]
            results.append((rects[0].y0, rects[0].x0, f"images/{name}", bbox))
        else:
            results.append((1e6, order, f"images/{name}", [0, 0, 0, 0]))
    return results


def render_pdf_thumbnails(ctx: ParseContext, dpi: int = 90, max_pages: int = 200) -> list[dict[str, Any]]:
    """为 PDF 渲染页面缩略图（供预览/版式视图）。"""
    pages: list[dict[str, Any]] = []
    doc = fitz.open(ctx.source_path)
    try:
        for page_index in range(min(doc.page_count, max_pages)):
            page = doc[page_index]
            pix = page.get_pixmap(dpi=dpi)
            name = f"page_{page_index + 1:04d}.png"
            pix.save(str(ctx.images_dir / name))
            pages.append(page_entry(page_index, f"images/{name}", pix.width, pix.height))
    finally:
        doc.close()
    return pages


@register
class PyMuPDFParser:
    engine = "pymupdf"
    label = "PyMuPDF 轻量解析"
    extensions = ("pdf",)

    async def parse(self, ctx: ParseContext) -> ParseResult:
        return await asyncio.to_thread(self._parse_sync, ctx)

    def _parse_sync(self, ctx: ParseContext) -> ParseResult:
        ctx.ensure_output()
        options = ctx.options.pymupdf
        doc = fitz.open(ctx.source_path)
        warnings: list[str] = []
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        markdown_blocks: list[str] = []
        text_chars = 0

        try:
            base_size = _base_font_size(doc)
            for page_index in range(doc.page_count):
                if ctx.cancel_check and ctx.cancel_check():
                    raise JobCanceled("任务已取消")
                page = doc[page_index]
                pix = page.get_pixmap(dpi=options.thumbnail_dpi)
                thumb_name = f"page_{page_index + 1:04d}.png"
                pix.save(str(ctx.images_dir / thumb_name))
                pages.append(page_entry(page_index, f"images/{thumb_name}", pix.width, pix.height))

                entries: list[dict[str, Any]] = []
                table_rects: list[fitz.Rect] = []
                if options.extract_tables:
                    try:
                        finder = page.find_tables()
                        for table in getattr(finder, "tables", []):
                            rect = fitz.Rect(table.bbox)
                            body = _table_markdown(table)
                            if not body:
                                continue
                            table_rects.append(rect)
                            entries.append(
                                {
                                    "y": rect.y0,
                                    "x": rect.x0,
                                    "kind": "table",
                                    "body": body,
                                    "bbox": [round(v, 2) for v in rect],
                                }
                            )
                    except Exception as exc:  # pragma: no cover - 表格识别失败降级
                        warnings.append(f"第 {page_index + 1} 页表格识别失败: {exc}")

                for block in page.get_text("dict").get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    rect = fitz.Rect(block["bbox"])
                    if any(rect.intersects(other) for other in table_rects):
                        continue
                    lines: list[str] = []
                    max_size = 0.0
                    for line in block.get("lines", []):
                        line_text = ""
                        for span in line.get("spans", []):
                            line_text += span.get("text", "")
                            max_size = max(max_size, float(span.get("size", 0) or 0))
                        if line_text.strip():
                            lines.append(line_text.strip())
                    text = clean_text("\n".join(lines))
                    if not text:
                        continue
                    text_chars += len(text)
                    level = _heading_level(max_size, base_size, text)
                    entries.append(
                        {
                            "y": rect.y0,
                            "x": rect.x0,
                            "kind": "title" if level else "text",
                            "level": level,
                            "text": text,
                            "bbox": [round(v, 2) for v in rect],
                        }
                    )

                if options.extract_images:
                    for y, x, img_path, bbox in _extract_images(doc, page, page_index, ctx):
                        entries.append({"y": y, "x": x, "kind": "image", "text": img_path, "bbox": bbox})

                entries.sort(key=lambda entry: (round(entry["y"], 1), round(entry["x"], 1)))
                for entry in entries:
                    if entry["kind"] == "table":
                        items.append(
                            {
                                "type": "table",
                                "table_body": entry["body"],
                                "page_idx": page_index,
                                "bbox": entry["bbox"],
                            }
                        )
                        markdown_blocks.append(entry["body"])
                    elif entry["kind"] == "title":
                        items.append(
                            {
                                "type": "title",
                                "text": entry["text"],
                                "text_level": entry["level"],
                                "page_idx": page_index,
                                "bbox": entry["bbox"],
                            }
                        )
                        markdown_blocks.append(heading(entry["text"], entry["level"]))
                    elif entry["kind"] == "image":
                        items.append(
                            {
                                "type": "image",
                                "img_path": entry["text"],
                                "page_idx": page_index,
                                "bbox": entry["bbox"],
                            }
                        )
                        markdown_blocks.append(f"![]({entry['text']})")
                    else:
                        items.append(
                            {
                                "type": "text",
                                "text": entry["text"],
                                "page_idx": page_index,
                                "bbox": entry["bbox"],
                            }
                        )
                        markdown_blocks.append(entry["text"])
        finally:
            doc.close()

        if text_chars == 0:
            warnings.append("未提取到文本层，文档可能是扫描件，建议改用 MinerU（ocr）解析")

        ctx.log(
            f"[pymupdf] 完成，页数={len(pages)}，文本字符={text_chars}，内容块={len(items)}"
        )
        return ParseResult(
            markdown="\n\n".join(block for block in markdown_blocks if block.strip()),
            content_list=items,
            pages=pages,
            meta={
                "engine": "pymupdf",
                "label": self.label,
                "extractor": "PyMuPDF",
                "page_count": len(pages),
                "text_chars": text_chars,
            },
            warnings=warnings,
        )
