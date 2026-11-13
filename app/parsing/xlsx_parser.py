"""XLSX/XLSM/CSV：openpyxl / csv 轻量转 Markdown 表格。"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
from pathlib import Path
from typing import Any

from .base import JobCanceled, ParseContext, ParseResult, register
from .markdown import heading, md_table

MAX_ROWS = 2000
MAX_COLS = 200


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value).strip()


def _trim(rows: list[list[str]]) -> list[list[str]]:
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    if not rows:
        return rows
    width = 0
    for row in rows:
        for index in range(len(row) - 1, -1, -1):
            if row[index]:
                width = max(width, index + 1)
                break
    return [row[:width] for row in rows]


def _read_csv(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as fh:
        reader = csv.reader(fh)
        for index, row in enumerate(reader):
            if index >= MAX_ROWS:
                break
            rows.append([_cell_text(cell) for cell in row[:MAX_COLS]])
    return _trim(rows)


def _read_xlsx(path: Path, warnings: list[str]) -> list[tuple[str, list[list[str]]]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True)
    sheets: list[tuple[str, list[list[str]]]] = []
    try:
        for sheet in workbook.worksheets:
            total_rows = sheet.max_row or 0
            total_cols = sheet.max_column or 0
            if total_rows > MAX_ROWS or total_cols > MAX_COLS:
                warnings.append(
                    f"工作表 {sheet.title} 尺寸 {total_rows}x{total_cols}，已截断为 {MAX_ROWS}x{MAX_COLS}"
                )
            max_row = min(total_rows, MAX_ROWS)
            max_col = min(total_cols, MAX_COLS)
            rows: list[list[str]] = []
            for row_idx in range(1, max_row + 1):
                # 合并单元格仅在左上角保留取值，其余保持空白，避免 Markdown 表格重复内容
                row = [
                    _cell_text(sheet.cell(row=row_idx, column=col_idx).value)
                    for col_idx in range(1, max_col + 1)
                ]
                rows.append(row)
            sheets.append((sheet.title, _trim(rows)))
    finally:
        workbook.close()
    return sheets


@register
class XlsxParser:
    engine = "xlsx"
    label = "Excel/CSV 轻量解析"
    extensions = ("xlsx", "xlsm", "csv")

    async def parse(self, ctx: ParseContext) -> ParseResult:
        return await asyncio.to_thread(self._parse_sync, ctx)

    def _parse_sync(self, ctx: ParseContext) -> ParseResult:
        ctx.ensure_output()
        warnings: list[str] = []
        items: list[dict[str, Any]] = []
        blocks: list[str] = []
        suffix = Path(ctx.filename).suffix.lower()

        if suffix == ".csv":
            sheets = [(Path(ctx.filename).stem or "Sheet1", _read_csv(ctx.source_path))]
        else:
            sheets = _read_xlsx(ctx.source_path, warnings)

        total_rows = 0
        for sheet_index, (name, rows) in enumerate(sheets):
            if ctx.cancel_check and ctx.cancel_check():
                raise JobCanceled("任务已取消")
            if not rows:
                continue
            total_rows += len(rows)
            blocks.append(heading(f"{name}", 2))
            body = md_table(rows)
            blocks.append(body)
            items.append(
                {
                    "type": "title",
                    "text": name,
                    "text_level": 2,
                    "page_idx": sheet_index,
                }
            )
            items.append(
                {
                    "type": "table",
                    "table_body": body,
                    "page_idx": sheet_index,
                    "sheet": name,
                    "rows": len(rows),
                }
            )

        if not items:
            warnings.append("表格未解析出任何数据")

        ctx.log(f"[xlsx] 完成，工作表={len(sheets)}，数据行={total_rows}")
        return ParseResult(
            markdown="\n\n".join(block for block in blocks if block.strip()),
            content_list=items,
            pages=[],
            meta={
                "engine": "xlsx",
                "label": self.label,
                "extractor": "openpyxl" if suffix != ".csv" else "csv",
                "page_count": len(sheets),
                "sheet_count": len(sheets),
            },
            warnings=warnings,
        )
