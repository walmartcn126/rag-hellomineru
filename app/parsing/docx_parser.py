"""DOCX：python-docx 轻量转 Markdown（标题/段落/列表/表格/图片）。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .base import JobCanceled, ParseContext, ParseResult, register
from .markdown import heading, md_table


def _heading_level(style_name: str) -> int:
    name = (style_name or "").lower()
    if name in ("title", "标题"):
        return 1
    match = re.match(r"^(heading|标题)\s*(\d+)", name)
    if match:
        return max(1, min(6, int(match.group(2))))
    return 0


@register
class DocxParser:
    engine = "docx"
    label = "DOCX 轻量解析"
    extensions = ("docx",)

    async def parse(self, ctx: ParseContext) -> ParseResult:
        return await asyncio.to_thread(self._parse_sync, ctx)

    def _parse_sync(self, ctx: ParseContext) -> ParseResult:
        ctx.ensure_output()
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(str(ctx.source_path))
        items: list[dict[str, Any]] = []
        blocks: list[str] = []
        warnings: list[str] = []
        image_seq = 0

        def save_image(blip, paragraph_title: str = "") -> str | None:
            nonlocal image_seq
            embed = blip.get(qn("r:embed"))
            if not embed:
                return None
            part = doc.part.related_parts.get(embed)
            if part is None:
                return None
            image_seq += 1
            ext = (part.partname.ext or "png").lstrip(".")
            name = f"docx_image_{image_seq}.{ext}"
            (ctx.images_dir / name).write_bytes(part.blob)
            items.append(
                {
                    "type": "image",
                    "img_path": f"images/{name}",
                    "page_idx": 0,
                    "caption": paragraph_title,
                }
            )
            return f"images/{name}"

        for child in doc.element.body.iterchildren():
            if ctx.cancel_check and ctx.cancel_check():
                raise JobCanceled("任务已取消")
            if child.tag == qn("w:p"):
                paragraph = Paragraph(child, doc)
                text = paragraph.text.strip()
                style_name = paragraph.style.name if paragraph.style is not None else ""
                level = _heading_level(style_name)
                if text:
                    if level:
                        items.append(
                            {"type": "title", "text": text, "text_level": level, "page_idx": 0}
                        )
                        blocks.append(heading(text, level))
                    elif style_name.lower().startswith("list"):
                        items.append({"type": "text", "text": text, "page_idx": 0, "style": "list"})
                        blocks.append(f"- {text}")
                    else:
                        items.append({"type": "text", "text": text, "page_idx": 0})
                        blocks.append(text)
                for run in paragraph.runs:
                    for blip in run._element.findall(".//" + qn("a:blip")):
                        image_path = save_image(blip, text)
                        if image_path:
                            blocks.append(f"![]({image_path})")
            elif child.tag == qn("w:tbl"):
                table = Table(child, doc)
                rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                body = md_table(rows)
                if body:
                    items.append({"type": "table", "table_body": body, "page_idx": 0})
                    blocks.append(body)

        if not blocks:
            warnings.append("DOCX 未解析出任何内容")

        ctx.log(f"[docx] 完成，内容块={len(items)}，图片={image_seq}")
        return ParseResult(
            markdown="\n\n".join(block for block in blocks if block.strip()),
            content_list=items,
            pages=[],
            meta={"engine": "docx", "label": self.label, "extractor": "python-docx", "page_count": 0},
            warnings=warnings,
        )
