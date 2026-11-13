"""任务执行：调用解析器并落盘统一产物。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..models import ParseOptions
from ..parsing import ParseContext, get_parser
from ..parsing.normalize import write_result


async def execute_parse(
    job: dict[str, Any],
    source_path: Path,
    work_dir: Path,
    options: ParseOptions,
    log: Callable[[str], None],
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """执行一次解析，返回最终 meta（产物已写入 work_dir/output）。"""
    parser = get_parser(job["engine"])
    ctx = ParseContext(
        job_id=job["id"],
        filename=job["filename"],
        source_path=source_path,
        work_dir=work_dir,
        options=options,
        log=log,
        cancel_check=cancel_check,
    )
    started = time.monotonic()
    result = await parser.parse(ctx)
    duration_ms = int((time.monotonic() - started) * 1000)
    meta = write_result(
        work_dir,
        result,
        {
            "job_id": job["id"],
            "filename": job["filename"],
            "engine": job["engine"],
            "runner": options.runner,
            "duration_ms": duration_ms,
            "options": options.model_dump(),
        },
    )
    log(f"[done] 解析完成，用时 {duration_ms} ms，Markdown {len(result.markdown)} 字符")
    return meta
