"""文档识别：扩展名嗅探 + PDF 原生数字版判定 + 引擎路由。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

from ..models import ENGINE_EXTS, IMAGE_EXTS, OFFICE_EXTS, PDF_EXTS, SUPPORTED_EXTS

TEXT_PAGE_THRESHOLD = 30
SAMPLE_LIMIT = 5


class UnsupportedFileError(ValueError):
    pass


def _pdf_sample_indices(page_count: int) -> list[int]:
    if page_count <= SAMPLE_LIMIT:
        return list(range(page_count))
    indices = {0, page_count // 4, page_count // 2, (page_count * 3) // 4, page_count - 1}
    return sorted(idx for idx in indices if 0 <= idx < page_count)


def detect_document(path: Path, filename: str) -> dict[str, Any]:
    ext = Path(filename).suffix.lower().lstrip(".")
    size = path.stat().st_size if path.exists() else 0
    result: dict[str, Any] = {
        "filename": filename,
        "ext": ext,
        "size": size,
        "kind": "unknown",
        "page_count": None,
        "pdf": None,
        "recommended_engine": None,
        "recommended_mineru_method": None,
        "reasons": [],
    }

    if ext not in SUPPORTED_EXTS:
        raise UnsupportedFileError(f"不支持的文件类型: .{ext or '未知'}")

    if ext in PDF_EXTS:
        info = analyze_pdf(path)
        result.update(
            {
                "kind": "pdf",
                "page_count": info["page_count"],
                "pdf": info,
                "recommended_engine": info["recommended_engine"],
                "recommended_mineru_method": info["recommended_mineru_method"],
                "reasons": info["reasons"],
            }
        )
        return result

    if ext in IMAGE_EXTS:
        width = height = None
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
        except Exception:  # pragma: no cover - 图片损坏时仍交给解析器报错
            pass
        result.update(
            {
                "kind": "image",
                "page_count": 1,
                "image": {"width": width, "height": height},
                "recommended_engine": "mineru",
                "recommended_mineru_method": "ocr",
                "reasons": ["图片文件无可提取文本层，建议使用 MinerU OCR"],
            }
        )
        return result

    if ext in OFFICE_EXTS:
        engine = {
            "docx": "docx",
            "pptx": "pptx",
            "xlsx": "xlsx",
            "xlsm": "xlsx",
            "csv": "xlsx",
        }.get(ext, "docx")
        result.update(
            {
                "kind": "office",
                "recommended_engine": engine,
                "reasons": [f"Office 文档（.{ext}），使用轻量解析器直接转换 Markdown"],
            }
        )
        return result

    raise UnsupportedFileError(f"不支持的文件类型: .{ext}")


def analyze_pdf(path: Path) -> dict[str, Any]:
    doc = fitz.open(path)
    try:
        page_count = doc.page_count
        indices = _pdf_sample_indices(page_count)
        chars: list[int] = []
        for idx in indices:
            page = doc[idx]
            chars.append(len(page.get_text("text").strip()))
        sampled = len(chars) or 1
        avg_chars = round(sum(chars) / sampled, 1)
        text_pages = sum(1 for value in chars if value >= TEXT_PAGE_THRESHOLD)
        text_ratio = round(text_pages / sampled, 3)
        page_has_text = text_ratio >= 0.5
        has_text_layer = avg_chars >= 100 and text_ratio >= 0.5
    finally:
        doc.close()

    if has_text_layer:
        engine, method = "pymupdf", None
        reasons = [f"抽样 {len(indices)} 页平均可提取 {avg_chars} 字符/页，判定为原生数字版 PDF，建议使用轻量解析"]
    elif avg_chars >= 40:
        engine, method = "mineru", "auto"
        reasons = [f"抽样页平均 {avg_chars} 字符/页（混合型），建议 MinerU 自动判定"]
    else:
        engine, method = "mineru", "ocr"
        reasons = [f"抽样页平均仅 {avg_chars} 字符/页，判定为扫描件，建议 MinerU 使用 ocr"]

    return {
        "page_count": page_count,
        "sampled_pages": len(indices),
        "sample_indices": indices,
        "avg_chars_per_page": avg_chars,
        "text_ratio": text_ratio,
        "page_has_text": page_has_text,
        "has_text_layer": has_text_layer,
        "recommended_engine": engine,
        "recommended_mineru_method": method,
        "reasons": reasons,
    }


def resolve_engine(ext: str, requested: str, detection: dict[str, Any]) -> str:
    if requested and requested != "auto":
        allowed = ENGINE_EXTS.get(requested)
        if allowed and ext not in allowed:
            raise UnsupportedFileError(
                f"引擎 {requested} 不支持 .{ext} 文件，可选引擎：{[e for e, exts in ENGINE_EXTS.items() if ext in exts]}"
            )
        return requested
    return detection["recommended_engine"] or "mineru"
