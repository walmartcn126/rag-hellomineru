"""人工标注与评分：五维整体评分、元素级标注、统计与导出。"""

from __future__ import annotations

import csv
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import repository, storage
from ..config import get_settings
from ..models import AnnotationIn, AnnotationItemIn

router = APIRouter(prefix="/api", tags=["annotations"])

SCORE_KEYS = ("text_accuracy", "reading_order", "table", "formula", "image")


async def _require_job(request: Request, job_id: str) -> dict:
    job = await repository.get_job(request.app.state.db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


async def _refresh_snapshots(settings, db, job_id: str) -> None:
    job = await repository.get_job(db, job_id)
    if job is None:
        return
    annotations = await repository.list_job_annotations(db, job_id)
    items = await repository.list_annotation_items(db, job_id)
    latest = annotations[0] if annotations else None
    repository.write_job_snapshot(settings, job, latest)
    for annotation in annotations:
        repository.write_annotation_snapshot(
            settings, job_id, annotation, [item for item in items]
        )


@router.get("/jobs/{job_id}/annotation")
async def get_annotation(
    request: Request,
    job_id: str,
    annotator: str | None = Query(default=None),
) -> dict:
    await _require_job(request, job_id)
    db = request.app.state.db
    annotation = await repository.get_annotation(db, job_id, annotator)
    items = await repository.list_annotation_items(db, job_id)
    return {
        "annotation": annotation,
        "items": items,
        "annotations": await repository.list_job_annotations(db, job_id),
    }


@router.put("/jobs/{job_id}/annotation")
async def put_annotation(request: Request, job_id: str, payload: AnnotationIn) -> dict:
    settings = get_settings()
    db = request.app.state.db
    await _require_job(request, job_id)
    scores = payload.scores.model_dump()
    annotation = await repository.upsert_annotation(
        db,
        job_id,
        payload.annotator.strip(),
        scores,
        payload.scores.total,
        payload.comment,
        payload.status,
    )
    await _refresh_snapshots(settings, db, job_id)
    return {"annotation": annotation}


@router.get("/jobs/{job_id}/annotation/items")
async def list_items(request: Request, job_id: str) -> dict:
    await _require_job(request, job_id)
    return {"items": await repository.list_annotation_items(request.app.state.db, job_id)}


@router.post("/jobs/{job_id}/annotation/items")
async def add_item(
    request: Request,
    job_id: str,
    payload: AnnotationItemIn,
    annotator: str | None = Query(default=None),
) -> dict:
    settings = get_settings()
    db = request.app.state.db
    await _require_job(request, job_id)
    annotation = await repository.get_annotation(db, job_id, annotator)
    item = await repository.add_annotation_item(
        db,
        job_id,
        annotation["id"] if annotation else None,
        payload.page_idx,
        payload.block_ref,
        payload.label,
        payload.comment,
    )
    await _refresh_snapshots(settings, db, job_id)
    return {"item": item}


@router.delete("/jobs/{job_id}/annotation/items/{item_id}")
async def delete_item(request: Request, job_id: str, item_id: str) -> dict:
    settings = get_settings()
    db = request.app.state.db
    await _require_job(request, job_id)
    if not await repository.delete_annotation_item(db, job_id, item_id):
        raise HTTPException(status_code=404, detail="标注不存在")
    await _refresh_snapshots(settings, db, job_id)
    return {"deleted": item_id}


@router.get("/annotations")
async def list_annotations(
    request: Request,
    engine: str | None = Query(default=None),
    annotator: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    rows = await repository.list_annotations(request.app.state.db, engine, annotator, limit)
    return {"annotations": rows}


@router.get("/stats/scores")
async def score_stats(
    request: Request,
    group_by: str = Query(default="engine,backend,method"),
) -> dict:
    columns = [column.strip() for column in group_by.split(",") if column.strip()]
    rows = await repository.score_stats(request.app.state.db, columns)
    return {"group_by": columns, "rows": rows}


@router.get("/export/annotations", response_model=None)
async def export_annotations(
    request: Request,
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    download: bool = Query(default=True),
) -> FileResponse | dict:
    settings = get_settings()
    rows = await repository.annotation_export_rows(request.app.state.db)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = settings.exports_dir / f"annotations_{stamp}.{format}"

    if format == "csv":
        columns = [
            "annotator", "job_id", "filename", "engine", "runner", "backend", "method", "lang",
            "formula_enable", "table_enable", "start_page_id", "end_page_id",
            *SCORE_KEYS, "total", "comment", "annotation_status",
            "sha256", "page_count", "duration_ms", "created_at", "updated_at",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    else:
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if not download:
        return {"path": str(path), "rows": len(rows)}
    return FileResponse(path, filename=path.name)
