"""MinerU 引擎：子进程 CLI 与进程内 Python API 双模式。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..models import IMAGE_EXTS
from .base import JobCanceled, ParseContext, ParseResult, register
from .markdown import clean_text
from .normalize import page_entry
from .pymupdf_parser import render_pdf_thumbnails


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.kill()
    except Exception:
        pass


def build_command(ctx: ParseContext, raw_dir: Path) -> list[str]:
    options = ctx.options.mineru
    executable = shutil.which("mineru")
    command = [executable] if executable else [sys.executable, "-m", "mineru.cli.client"]
    command += [
        "-p", str(ctx.source_path),
        "-o", str(raw_dir),
        "-b", options.backend,
        "-m", options.method,
        "-l", options.lang,
        "-s", str(options.start_page_id),
        "-f", str(options.formula_enable).lower(),
        "-t", str(options.table_enable).lower(),
        "--source", options.model_source,
    ]
    if options.end_page_id is not None:
        command += ["-e", str(options.end_page_id)]
    if options.device:
        command += ["-d", options.device]
    if options.virtual_vram:
        command += ["--vram", str(options.virtual_vram)]
    if options.server_url:
        command += ["-u", options.server_url]
    return command


@register
class MineruParser:
    engine = "mineru"
    label = "MinerU"
    extensions = ("pdf",) + IMAGE_EXTS

    async def parse(self, ctx: ParseContext) -> ParseResult:
        raw_dir = ctx.work_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        ctx.ensure_output()
        started = time.monotonic()
        warnings: list[str] = []
        options = ctx.options.mineru
        ctx.log(
            f"[mineru] runner={ctx.options.runner} backend={options.backend} "
            f"method={options.method} lang={options.lang}"
        )

        if ctx.options.runner == "inprocess":
            await self._run_inprocess(ctx, raw_dir, warnings)
        else:
            await self._run_subprocess(ctx, raw_dir)

        duration_ms = int((time.monotonic() - started) * 1000)
        result = self._collect(ctx, raw_dir, warnings)
        result.meta.update(
            {
                "engine": "mineru",
                "label": self.label,
                "runner": ctx.options.runner,
                "backend": options.backend,
                "method": options.method,
                "lang": options.lang,
                "model_source": options.model_source,
                "duration_ms": duration_ms,
            }
        )
        return result

    async def _run_subprocess(self, ctx: ParseContext, raw_dir: Path) -> None:
        command = build_command(ctx, raw_dir)
        ctx.log("[mineru] $ " + " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ctx.work_dir),
        )

        async def pump() -> None:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    ctx.log(text)

        pump_task = asyncio.create_task(pump())
        timeout = ctx.options.timeout_sec or get_settings().job_timeout_sec
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while process.returncode is None:
                if ctx.cancel_check and ctx.cancel_check():
                    await asyncio.to_thread(_kill_process, process)
                    raise JobCanceled("任务已取消")
                if deadline and time.monotonic() > deadline:
                    await asyncio.to_thread(_kill_process, process)
                    raise RuntimeError(f"MinerU 子进程超时（{timeout}s）")
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
        finally:
            await pump_task
        if process.returncode != 0:
            raise RuntimeError(f"MinerU 子进程退出码 {process.returncode}，请查看运行日志")

    async def _run_inprocess(
        self, ctx: ParseContext, raw_dir: Path, warnings: list[str]
    ) -> None:
        from mineru.cli.common import do_parse
        from mineru.utils.enum_class import MakeMode

        options = ctx.options.mineru
        if options.device:
            current = os.environ.get("MINERU_DEVICE_MODE")
            if current and current != options.device:
                warnings.append(
                    f"进程内模式 device 已固定为 {current}，本次请求 {options.device} 需重启服务或改用子进程模式"
                )
            else:
                os.environ["MINERU_DEVICE_MODE"] = options.device
        if options.virtual_vram:
            current_vram = os.environ.get("MINERU_VIRTUAL_VRAM_SIZE")
            if current_vram and current_vram != str(options.virtual_vram):
                warnings.append(
                    f"进程内模式 vram 已固定为 {current_vram}，本次请求 {options.virtual_vram} 未生效"
                )
            else:
                os.environ["MINERU_VIRTUAL_VRAM_SIZE"] = str(options.virtual_vram)

        def _run() -> None:
            from loguru import logger

            sink_id = logger.add(
                lambda message: ctx.log(str(message).rstrip()),
                level="INFO",
                format="{time:HH:mm:ss} | {level} | {message}",
            )
            try:
                do_parse(
                    output_dir=str(raw_dir),
                    pdf_file_names=[ctx.stem],
                    pdf_bytes_list=[ctx.source_path.read_bytes()],
                    p_lang_list=[options.lang],
                    backend=options.backend,
                    parse_method=options.method,
                    formula_enable=options.formula_enable,
                    table_enable=options.table_enable,
                    server_url=options.server_url,
                    start_page_id=options.start_page_id,
                    end_page_id=options.end_page_id,
                    f_make_md_mode=MakeMode.MM_MD,
                )
            finally:
                logger.remove(sink_id)

        ctx.log("[mineru] 进程内模式执行 do_parse ...")
        await asyncio.to_thread(_run)
        if ctx.cancel_check and ctx.cancel_check():
            raise JobCanceled("任务已取消（进程内模式推理不可中断，结果已丢弃）")

    def _collect(self, ctx: ParseContext, raw_dir: Path, warnings: list[str]) -> ParseResult:
        markdown_files = sorted(
            raw_dir.rglob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        if not markdown_files:
            raise RuntimeError("MinerU 未生成 Markdown 输出，请查看运行日志")
        markdown_file = markdown_files[0]
        markdown = clean_text(markdown_file.read_text(encoding="utf-8", errors="replace"))

        content_list: list[dict[str, Any]] = []
        content_file = markdown_file.with_name(f"{markdown_file.stem}_content_list.json")
        if content_file.exists():
            try:
                loaded = json.loads(content_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    content_list = loaded
            except json.JSONDecodeError:
                warnings.append("content_list.json 解析失败")
        if not content_list and markdown:
            content_list = [{"type": "text", "page_idx": 0, "text": markdown}]

        images_dir = markdown_file.parent / "images"
        if images_dir.exists():
            for image in images_dir.iterdir():
                if image.is_file():
                    shutil.copy2(image, ctx.images_dir / image.name)

        pages: list[dict[str, Any]] = []
        if Path(ctx.filename).suffix.lower() == ".pdf":
            try:
                pages = render_pdf_thumbnails(ctx, dpi=90)
            except Exception as exc:  # pragma: no cover - 预览降级
                warnings.append(f"页面缩略图生成失败: {exc}")
        else:
            try:
                from PIL import Image

                with Image.open(ctx.source_path) as image:
                    name = "page_0001.png"
                    image.convert("RGB").save(ctx.images_dir / name)
                    pages.append(page_entry(0, f"images/{name}", image.width, image.height))
            except Exception:
                pass

        page_count = len(pages) or (max((int(item.get("page_idx", 0)) for item in content_list), default=0) + 1)
        ctx.log(f"[mineru] 产物收集完成，markdown={len(markdown)} 字符，page_count={page_count}")
        return ParseResult(
            markdown=markdown,
            content_list=content_list,
            pages=pages,
            meta={
                "extractor": "MinerU",
                "page_count": page_count,
                "raw_markdown": str(markdown_file.relative_to(ctx.work_dir)),
            },
            warnings=warnings,
        )
