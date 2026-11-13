"""元信息接口：健康检查、解析器与参数元数据、文档识别。"""

from __future__ import annotations

import importlib.metadata

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from .. import storage
from ..config import get_settings
from ..models import PDF_EXTS, IMAGE_EXTS, OFFICE_EXTS
from ..parsing.detect import UnsupportedFileError, detect_document

router = APIRouter(prefix="/api", tags=["meta"])

ALL_EXTS = [*PDF_EXTS, *IMAGE_EXTS, *OFFICE_EXTS]

PARSERS = [
    {
        "id": "auto",
        "label": "自动识别（推荐）",
        "description": "按文件类型与 PDF 文本层自动选择引擎：原生数字版走 PyMuPDF，扫描件走 MinerU OCR",
        "extensions": ALL_EXTS,
    },
    {
        "id": "mineru",
        "label": "MinerU",
        "description": "MinerU 深度解析，输出 Markdown + content_list + layout，可配置全部参数",
        "extensions": [*PDF_EXTS, *IMAGE_EXTS],
    },
    {
        "id": "pymupdf",
        "label": "PyMuPDF 轻量解析",
        "description": "面向原生数字版 PDF：直接抽取文本/表格/图片，速度快、不占 GPU",
        "extensions": list(PDF_EXTS),
    },
    {
        "id": "docx",
        "label": "DOCX 轻量解析",
        "description": "python-docx 转换为 Markdown（标题/段落/列表/表格/图片）",
        "extensions": ["docx"],
    },
    {
        "id": "pptx",
        "label": "PPTX 轻量解析",
        "description": "python-pptx 按幻灯片转换为 Markdown",
        "extensions": ["pptx"],
    },
    {
        "id": "xlsx",
        "label": "Excel/CSV 轻量解析",
        "description": "openpyxl / csv 转换为 Markdown 表格",
        "extensions": ["xlsx", "xlsm", "csv"],
    },
]

OPTION_GROUPS = [
    {
        "id": "common",
        "label": "通用",
        "fields": [
            {
                "name": "engine",
                "label": "解析引擎",
                "type": "select",
                "default": "auto",
                "choices": [
                    {"value": "auto", "label": "自动识别（推荐）"},
                    {"value": "pymupdf", "label": "PyMuPDF 轻量解析"},
                    {"value": "mineru", "label": "MinerU"},
                    {"value": "docx", "label": "DOCX"},
                    {"value": "pptx", "label": "PPTX"},
                    {"value": "xlsx", "label": "Excel/CSV"},
                ],
            },
            {
                "name": "runner",
                "label": "MinerU 执行方式",
                "type": "select",
                "default": "subprocess",
                "choices": [
                    {"value": "subprocess", "label": "子进程 CLI（隔离，可实时日志）"},
                    {"value": "inprocess", "label": "进程内 API（模型常驻，更快）"},
                ],
                "help": "仅 engine=mineru 生效；进程内模式修改 device/vram/model_source 需重启",
            },
            {
                "name": "timeout_sec",
                "label": "超时（秒，留空使用默认）",
                "type": "number",
                "default": None,
                "min": 1,
                "max": 86400,
            },
        ],
    },
    {
        "id": "mineru",
        "label": "MinerU 参数",
        "fields": [
            {
                "name": "mineru.backend",
                "label": "backend",
                "type": "select",
                "default": "pipeline",
                "choices": [
                    {"value": "pipeline", "label": "pipeline（通用，CPU 可用）"},
                    {"value": "vlm-auto-engine", "label": "vlm-auto-engine（本地算力，高精度）"},
                    {"value": "hybrid-auto-engine", "label": "hybrid-auto-engine（本地算力，下一代）"},
                    {"value": "vlm-http-client", "label": "vlm-http-client（远程 VLM 服务）"},
                    {"value": "hybrid-http-client", "label": "hybrid-http-client（远程 VLM 服务）"},
                ],
                "help": "pipeline / hybrid-* 支持 method 与 lang；vlm-* 忽略 method",
            },
            {
                "name": "mineru.method",
                "label": "method",
                "type": "select",
                "default": "auto",
                "choices": [
                    {"value": "auto", "label": "auto（自动判定）"},
                    {"value": "txt", "label": "txt（文本抽取，快速）"},
                    {"value": "ocr", "label": "ocr（OCR，扫描件）"},
                ],
            },
            {
                "name": "mineru.lang",
                "label": "lang",
                "type": "select",
                "default": "ch",
                "choices": [
                    {"value": "ch", "label": "ch（中英）"},
                    {"value": "ch_server", "label": "ch_server"},
                    {"value": "ch_lite", "label": "ch_lite"},
                    {"value": "en", "label": "en"},
                    {"value": "korean", "label": "korean"},
                    {"value": "japan", "label": "japan"},
                    {"value": "chinese_cht", "label": "chinese_cht"},
                    {"value": "latin", "label": "latin"},
                    {"value": "arabic", "label": "arabic"},
                    {"value": "cyrillic", "label": "cyrillic"},
                    {"value": "devanagari", "label": "devanagari"},
                ],
            },
            {
                "name": "mineru.server_url",
                "label": "server_url",
                "type": "text",
                "default": "",
                "help": "仅 backend 为 *-http-client 时使用，例如 http://127.0.0.1:30000",
            },
            {
                "name": "mineru.start_page_id",
                "label": "起始页（从 0 开始）",
                "type": "number",
                "default": 0,
                "min": 0,
            },
            {
                "name": "mineru.end_page_id",
                "label": "结束页（含，留空到末尾）",
                "type": "number",
                "default": None,
                "min": 0,
            },
            {
                "name": "mineru.formula_enable",
                "label": "解析公式",
                "type": "boolean",
                "default": True,
            },
            {
                "name": "mineru.table_enable",
                "label": "解析表格",
                "type": "boolean",
                "default": True,
            },
            {
                "name": "mineru.device",
                "label": "device",
                "type": "text",
                "default": "",
                "help": "仅 pipeline；留空自动探测，例如 cpu / cuda:0",
            },
            {
                "name": "mineru.virtual_vram",
                "label": "vram（MB）",
                "type": "number",
                "default": None,
                "min": 1,
                "help": "仅 pipeline 生效",
            },
            {
                "name": "mineru.model_source",
                "label": "模型来源",
                "type": "select",
                "default": "huggingface",
                "choices": [
                    {"value": "huggingface", "label": "huggingface"},
                    {"value": "modelscope", "label": "modelscope（国内推荐）"},
                    {"value": "local", "label": "local"},
                ],
            },
        ],
    },
    {
        "id": "pymupdf",
        "label": "PyMuPDF 参数",
        "fields": [
            {"name": "pymupdf.extract_tables", "label": "抽取表格", "type": "boolean", "default": True},
            {"name": "pymupdf.extract_images", "label": "抽取图片", "type": "boolean", "default": True},
            {
                "name": "pymupdf.thumbnail_dpi",
                "label": "页面缩略图 DPI",
                "type": "number",
                "default": 110,
                "min": 40,
                "max": 300,
            },
        ],
    },
]


def _mineru_version() -> str:
    try:
        return importlib.metadata.version("mineru")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@router.get("/health")
async def health(request: Request) -> dict:
    db = request.app.state.db
    settings = get_settings()
    from .. import repository

    counts = await repository.job_status_counts(db)
    workers = await repository.list_workers(db)
    online = sum(1 for worker in workers if worker["status"] == "online")
    cuda = False
    device = settings.mineru_device or "cpu"
    try:
        import torch

        cuda = bool(torch.cuda.is_available())
        if cuda and not settings.mineru_device:
            device = "cuda"
    except Exception:
        pass
    return {
        "app": "HelloMinerU",
        "role": settings.role,
        "mineru_version": _mineru_version(),
        "device": device,
        "cuda": cuda,
        "queue": counts,
        "workers_online": online,
        "workers_total": len(workers),
        "worker_token_file": str(settings.data_path / "worker_token.txt"),
        "data_dir": str(settings.data_path),
    }


@router.get("/parsers")
async def parsers() -> dict:
    return {"parsers": PARSERS, "option_groups": OPTION_GROUPS}


@router.post("/detect")
async def detect(request: Request, file: UploadFile = File(...)) -> dict:
    settings = get_settings()
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    try:
        temp_path, filename, size = await storage.save_temp_upload(
            settings, file, settings.max_upload_mb
        )
    except storage.UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    try:
        detection = detect_document(temp_path, filename)
    finally:
        temp_path.unlink(missing_ok=True)
    detection["size"] = size
    return detection
