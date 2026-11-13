"""PPTX：python-pptx 轻量转 Markdown（按幻灯片输出文本/表格/图片）。"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import JobCanceled, ParseContext, ParseResult, register
from .markdown import heading, md_table


@register
class PptxParser:
    engine = "pptx"
    label = "PPTX 轻量解析"
    extensions = ("pptx",)

    async def parse(self, ctx: ParseContext) -> ParseResult:
        return await asyncio.to_thread(self._parse_sync, ctx)

    def _parse_sync(self, ctx: ParseContext) -> ParseResult:
        ctx.ensure_output()
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(str(ctx.source_path))
        items: list[dict[str, Any]] = []
        blocks: list[str] = []
        warnings: list[str] = []
        image_seq = 0

        for slide_index, slide in enumerate(prs.slides):
            if ctx.cancel_check and ctx.cancel_check():
                raise JobCanceled("任务已取消")
            slide_no = slide_index + 1
            blocks.append(heading(f"Slide {slide_no}", 2))
            title_shape = slide.shapes.title
            title_id = title_shape.shape_id if title_shape is not None else None
            title_text = (title_shape.text or "").strip() if title_shape is not None else ""
            if title_text:
                items.append(
                    {"type": "title", "text": title_text, "text_level": 3, "page_idx": slide_index}
                )
                blocks.append(heading(title_text, 3))

            for shape in slide.shapes:
                if shape.shape_id == title_id:
                    continue
                if getattr(shape, "has_table", False):
                    rows = [
                        [cell.text.strip() for cell in row.cells] for row in shape.table.rows
                    ]
                    body = md_table(rows)
                    if body:
                        items.append(
                            {"type": "table", "table_body": body, "page_idx": slide_index}
                        )
                        blocks.append(body)
                    continue
                if getattr(shape, "has_text_frame", False):
                    text = (shape.text_frame.text or "").strip()
                    if text:
                        items.append({"type": "text", "text": text, "page_idx": slide_index})
                        blocks.append(text)
                    continue
                is_picture = shape.shape_type == MSO_SHAPE_TYPE.PICTURE or hasattr(shape, "image")
                if is_picture:
                    try:
                        image = shape.image
                    except Exception:
                        continue
                    image_seq += 1
                    ext = (image.ext or "png").lstrip(".")
                    name = f"pptx_image_{image_seq}.{ext}"
                    (ctx.images_dir / name).write_bytes(image.blob)
                    items.append(
                        {"type": "image", "img_path": f"images/{name}", "page_idx": slide_index}
                    )
                    blocks.append(f"![]({f'images/{name}'})")

        if not blocks:
            warnings.append("PPTX 未解析出任何内容")

        ctx.log(f"[pptx] 完成，幻灯片={len(prs.slides)}，内容块={len(items)}，图片={image_seq}")
        return ParseResult(
            markdown="\n\n".join(block for block in blocks if block.strip()),
            content_list=items,
            pages=[],
            meta={
                "engine": "pptx",
                "label": self.label,
                "extractor": "python-pptx",
                "page_count": len(prs.slides),
            },
            warnings=warnings,
        )
