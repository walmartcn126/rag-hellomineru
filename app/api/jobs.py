"""任务与结果接口：提交、查询、日志、结果、文件、取消、删除。"""

from __future__ import annotations

import json
import mimetypes
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError

from .. import repository, storage
from ..config import get_settings
from ..coordinator import queue as coordinator_queue
from ..models import SUPPORTED_EXTS, ParseOptions
from ..parsing.detect import UnsupportedFileError, detect_document
from ..parsing.normalize import rewrite_image_urls

router = APIRouter(prefix="/api", tags=["jobs"])


async def _require_job(request: Request, job_id: str) -> dict:
    job = await repository.get_job(request.app.state.db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.post("/parse", status_code=202)
async def create_parse(
    request: Request,
    file: UploadFile = File(...),
    options: str = Form("{}"),
) -> dict:
    settings = get_settings()
    db = request.app.state.db
    try:
        parse_options = ParseOptions.model_validate_json(options or "{}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    ext = Path(file.filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: .{ext or '未知'}，支持: {', '.join(SUPPORTED_EXTS)}",
        )
    try:
        upload = await storage.save_upload(settings, file, settings.max_upload_mb)
    except storage.UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        detection = detect_document(upload["path"], upload["filename"])
        document_id = await repository.upsert_document(
            db,
            upload["sha256"],
            upload["filename"],
            upload["ext"],
            upload["size"],
            detection.get("page_count"),
        )
        document = await repository.get_document(db, document_id)
        assert document is not None
        job = await coordinator_queue.create_job(
            settings,
            db,
            document=document,
            filename=upload["filename"],
            ext=upload["ext"],
            options=parse_options,
            detection=detection,
        )
    except UnsupportedFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "job_id": job["id"],
        "status": job["status"],
        "engine": job["engine"],
        "runner": job["runner"],
        "resolved_options": job["resolved_options"],
        "detection": detection,
        "warnings": job["warnings"],
    }


@router.get("/jobs")
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    engine: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    jobs = await repository.list_jobs(request.app.state.db, status, engine, limit, offset)
    for job in jobs:
        job.pop("options_json", None)
        job.pop("resolved_options_json", None)
    return {"jobs": jobs}


@router.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: str) -> dict:
    db = request.app.state.db
    job = await _require_job(request, job_id)
    annotations = await repository.list_job_annotations(db, job_id)
    items = await repository.list_annotation_items(db, job_id)
    queue_position = None
    if job["status"] == "queued":
        rows = await db.fetchall(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND created_at < ?",
            (job["created_at"],),
        )
        queue_position = int(rows[0]["n"]) + 1 if rows else None
    return {
        "job": job,
        "annotations": annotations,
        "annotation_items": items,
        "queue_position": queue_position,
    }


@router.get("/jobs/{job_id}/log")
async def job_log(
    request: Request,
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    await _require_job(request, job_id)
    return storage.read_log(get_settings(), job_id, offset, limit)


@router.get("/jobs/{job_id}/result")
async def job_result(
    request: Request,
    job_id: str,
    annotator: str | None = Query(default=None),
) -> dict:
    settings = get_settings()
    db = request.app.state.db
    job = await _require_job(request, job_id)
    output_dir = storage.job_output_dir(settings, job_id)
    markdown_path = output_dir / "result.md"
    if not markdown_path.exists():
        raise HTTPException(
            status_code=409,
            detail={"message": "结果尚未生成", "status": job["status"], "error": job.get("error")},
        )
    markdown = rewrite_image_urls(markdown_path.read_text(encoding="utf-8", errors="replace"), job_id)
    content_list = storage.read_json(output_dir / "content_list.json", []) or []
    meta = storage.read_json(output_dir / "meta.json", {}) or job.get("meta") or {}

    # meta 以 DB 版本为准（finalize_job 更新过，可能含 artifacts/pages 等增强字段）；
    # 若 DB 中没有 meta_json 或为空，再回退到磁盘 meta.json。
    db_meta = job.get("meta") or {}
    if db_meta:
        # 合并：磁盘 meta 先读（含 engine/duration/stats），DB meta 覆盖（含 artifacts/pages）
        merged_meta = {**meta, **db_meta}
        meta = merged_meta

    pages = []
    for page in meta.get("pages") or []:
        image = page.get("image")
        pages.append(
            {
                **page,
                "url": f"/api/jobs/{job_id}/files/output/{quote(image)}" if image else None,
            }
        )
    # artifacts 永远以磁盘扫描结果为准 — 磁盘才是真正的数据源；
    # 磁盘为空时再回退到 meta 里记录的（如果有的话），给调用方返回至少一个非空数组。
    artifacts = storage.list_artifacts(settings, job_id)
    if not artifacts:
        artifacts = meta.get("artifacts") or []
    annotation = await repository.get_annotation(db, job_id, annotator)
    items = await repository.list_annotation_items(db, job_id)

    return {
        "job": job,
        "markdown": markdown,
        "content_list": content_list,
        "pages": pages,
        "artifacts": artifacts,
        "meta": meta,
        "annotation": annotation,
        "annotation_items": items,
    }


@router.get("/jobs/{job_id}/files/{file_path:path}")
async def job_file(request: Request, job_id: str, file_path: str) -> FileResponse:
    settings = get_settings()
    await _require_job(request, job_id)
    root = storage.job_dir(settings, job_id)
    try:
        target = storage.safe_child(root, file_path)
    except storage.UnsafePath as exc:
        raise HTTPException(status_code=400, detail="非法文件路径") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> dict:
    settings = get_settings()
    await _require_job(request, job_id)
    job = await coordinator_queue.cancel_job(settings, request.app.state.db, job_id)
    return {"job": job}


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: str) -> dict:
    settings = get_settings()
    db = request.app.state.db
    job = await _require_job(request, job_id)
    if job["status"] == "running":
        raise HTTPException(status_code=409, detail="任务运行中，请先取消")
    await repository.delete_job(db, job_id)
    shutil.rmtree(storage.job_dir(settings, job_id), ignore_errors=True)
    return {"deleted": job_id}
