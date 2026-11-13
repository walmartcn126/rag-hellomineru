"""统一产物：写 result.md / content_list.json / meta.json，图片路径重写。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .base import ParseResult
from .markdown import markdown_chars

IMAGE_MD_RE = re.compile(r"\]\((images/[^)\s]+)\)")
IMAGE_HTML_RE = re.compile(r'(<img[^>]+src=")(images/[^"]+)(")')


def rewrite_image_urls(markdown: str, job_id: str) -> str:
    """把 Markdown 里的相对图片路径改写成 API 绝对路径。"""
    prefix = f"/api/jobs/{job_id}/files/output/"

    def _md(match: re.Match[str]) -> str:
        return f"]({prefix}{quote(match.group(1))})"

    def _html(match: re.Match[str]) -> str:
        return f"{match.group(1)}{prefix}{quote(match.group(2))}{match.group(3)}"

    markdown = IMAGE_MD_RE.sub(_md, markdown)
    return IMAGE_HTML_RE.sub(_html, markdown)


def page_entry(page_idx: int, image: str, width: int, height: int) -> dict[str, Any]:
    return {"page_idx": page_idx, "image": image, "width": width, "height": height}


def stats_of(result: ParseResult) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in result.content_list:
        item_type = str(item.get("type", "text"))
        counts[item_type] = counts.get(item_type, 0) + 1
    return {
        "markdown_chars": markdown_chars(result.markdown),
        "content_items": len(result.content_list),
        "content_types": counts,
        "page_count": len(result.pages),
    }


def write_result(work_dir: Path, result: ParseResult, meta: dict[str, Any]) -> dict[str, Any]:
    """把解析结果落盘到 work_dir/output/，返回最终 meta。"""
    output = work_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    (output / "images").mkdir(parents=True, exist_ok=True)

    markdown = result.markdown or ""
    (output / "result.md").write_text(markdown, encoding="utf-8")
    (output / "content_list.json").write_text(
        json.dumps(result.content_list, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    warnings = list(dict.fromkeys([*(meta.pop("warnings", []) or []), *result.warnings]))
    final_meta: dict[str, Any] = {
        **result.meta,
        **meta,
        "warnings": warnings,
        "stats": stats_of(result),
        "pages": result.pages,
        "outputs": {
            "markdown": "output/result.md",
            "content_list": "output/content_list.json",
            "meta": "output/meta.json",
        },
    }
    (output / "meta.json").write_text(
        json.dumps(final_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return final_meta
