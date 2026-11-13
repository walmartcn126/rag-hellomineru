"""Markdown 拼装与文本处理工具。"""

from __future__ import annotations

import re
from typing import Any, Iterable


def escape_md(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


def heading(text: str, level: int) -> str:
    level = max(1, min(6, level))
    return f"{'#' * level} {clean_text(text)}"


def clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def md_table(rows: Iterable[Iterable[Any]]) -> str:
    matrix: list[list[str]] = []
    for row in rows:
        cells = [clean_text(str(cell)) if cell is not None else "" for cell in row]
        matrix.append(cells)
    if not matrix:
        return ""
    width = max(len(row) for row in matrix)
    matrix = [row + [""] * (width - len(row)) for row in matrix]
    lines = ["| " + " | ".join(escape_md(cell) for cell in matrix[0]) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in matrix[1:]:
        lines.append("| " + " | ".join(escape_md(cell) for cell in row) + " |")
    return "\n".join(lines)


def block_join(blocks: list[str]) -> str:
    return "\n\n".join(block for block in blocks if block and block.strip())


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def markdown_chars(markdown: str) -> int:
    return len(markdown.strip())
