"""Worker 协议接口：注册、心跳、领取任务、上报进度/结果、失败上报。"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from .. import repository, storage
from ..config import get_settings
from ..coordinator import queue as coordinator_queue
from ..models import FailIn, HeartbeatIn, ProgressIn, WorkerRegisterIn

router = APIRouter(prefix="/api", tags=["workers"])


async def require_worker_token(x_hmu_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if x_hmu_token != settings.resolved_worker_token():
        raise HTTPException(status_code=401, detail="worker token 无效")


@router.get("/workers")
async def list_workers(request: Request) -> dict:
    settings = get_settings()
    db = request.app.state.db
    workers = await repository.list_workers(db)
    counts = await repository.job_status_counts(db)
    return {
        "workers": workers,
        "queue": counts,
        "lease_sec": settings.lease_sec,
        "heartbeat_sec": settings.heartbeat_sec,
    }


@router.post("/workers/register", dependencies=[Depends(require_worker_token)])
async def register_worker(request: Request, payload: WorkerRegisterIn) -> dict:
    db = request.app.state.db
    worker_id = await repository.register_worker(
        db,
        payload.name,
        payload.hostname,
        payload.capabilities.model_dump(),
        payload.version,
    )
    return {"worker_id": worker_id, "lease_sec": get_settings().lease_sec}


@router.post("/workers/{worker_id}/heartbeat", dependencies=[Depends(require_worker_token)])
async def worker_heartbeat(request: Request, worker_id: str, payload: HeartbeatIn) -> dict:
    db = request.app.state.db
    worker = await repository.get_worker(db, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker 未注册")
    await repository.heartbeat_worker(db, worker_id, payload.current_job_id)
    current = None
    if payload.current_job_id:
        job = await repository.get_job(db, payload.current_job_id)
        if job is not None:
            current = {"id": job["id"], "status": job["status"]}
    return {"current_job": current, "lease_sec": get_settings().lease_sec}


@router.post(
    "/workers/{worker_id}/claim",
    dependencies=[Depends(require_worker_token)],
    responses={204: {"description": "暂无任务"}},
)
async def claim_job(request: Request, worker_id: str):
    settings = get_settings()
    db = request.app.state.db
    worker = await repository.get_worker(db, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker 未注册")
    job = await repository.claim_job(
        db, worker_id, worker["capabilities"], settings.lease_sec, settings.max_attempts
    )
    if job is None:
        return Response(status_code=204)
    return {
        "id": job["id"],
        "document_id": job["document_id"],
        "filename": job["filename"],
        "ext": job["ext"],
        "engine": job["engine"],
        "runner": job["runner"],
        "resolved_options": job["resolved_options"],
        "attempts": job["attempts"],
        "created_at": job["created_at"],
    }


@router.post("/worker/tasks/{job_id}/progress", dependencies=[Depends(require_worker_token)])
async def task_progress(request: Request, job_id: str, payload: ProgressIn) -> dict:
    settings = get_settings()
    db = request.app.state.db
    job = await repository.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    await coordinator_queue.append_progress(
        settings, db, job_id, payload.stage, payload.progress, payload.logs
    )
    await repository.touch_lease(db, job_id, settings.lease_sec)
    return {"ok": True}


@router.get("/worker/tasks/{job_id}/file", dependencies=[Depends(require_worker_token)])
async def task_file(request: Request, job_id: str) -> FileResponse:
    settings = get_settings()
    db = request.app.state.db
    job = await repository.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    document = await repository.get_document(db, int(job["document_id"]))
    if document is None:
        raise HTTPException(status_code=404, detail="源文档不存在")
    path = storage.upload_path(settings, document["sha256"], document["ext"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="源文件已丢失")
    return FileResponse(path, filename=job["filename"], media_type="application/octet-stream")


@router.post("/worker/tasks/{job_id}/result", dependencies=[Depends(require_worker_token)])
async def task_result(request: Request, job_id: str, bundle: UploadFile = File(...)) -> dict:
    settings = get_settings()
    db = request.app.state.db
    job = await repository.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    target = settings.data_path / "tmp" / f"{job_id}_{uuid.uuid4().hex}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    limit = settings.max_bundle_mb * 1024 * 1024
    size = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await bundle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail="结果包超过大小限制")
                handle.write(chunk)
        await coordinator_queue.finalize_bundle(settings, db, job_id, target)
    finally:
        target.unlink(missing_ok=True)
    return {"ok": True, "job_id": job_id}


@router.post("/worker/tasks/{job_id}/fail", dependencies=[Depends(require_worker_token)])
async def task_fail(request: Request, job_id: str, payload: FailIn) -> dict:
    settings = get_settings()
    db = request.app.state.db
    job = await repository.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    await coordinator_queue.mark_failed(settings, db, job_id, payload.error, payload.logs)
    return {"ok": True}
