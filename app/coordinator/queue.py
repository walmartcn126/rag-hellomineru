"""协调节点：任务创建、参数解析、结果终结与失败处理。"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import repository, storage
from ..db import Database
from ..models import ParseOptions
from ..parsing.detect import resolve_engine


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_options(
    options: ParseOptions, detection: dict[str, Any]
) -> tuple[str, ParseOptions, list[str]]:
    """根据识别结果确定最终引擎，并补全 MinerU method。"""
    engine = resolve_engine(detection["ext"], options.engine, detection)
    warnings: list[str] = []
    if engine == "mineru" and options.engine == "auto":
        method = detection.get("recommended_mineru_method")
        if method and method != options.mineru.method:
            options.mineru.method = method
            warnings.append(f"自动识别：MinerU method 已设为 {method}")
    if engine == "mineru" and options.mineru.backend.endswith("-client") and not options.mineru.server_url:
        warnings.append("backend 为 http-client 但未填写 server_url，解析预计失败")
    if engine == "mineru" and options.runner == "inprocess":
        warnings.append("进程内模式下 device/vram/model_source 仅在模型首次加载时生效")
    return engine, options, warnings


async def create_job(
    settings,
    db: Database,
    *,
    document: dict[str, Any],
    filename: str,
    ext: str,
    options: ParseOptions,
    detection: dict[str, Any],
) -> dict[str, Any]:
    engine, resolved, warnings = resolve_options(options, detection)
    existing_warnings = list(detection.get("reasons") or [])
    merged_warnings = list(dict.fromkeys([*warnings, *existing_warnings]))
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "document_id": int(document["id"]),
        "filename": filename,
        "ext": ext,
        "engine": engine,
        "runner": resolved.runner if engine == "mineru" else "direct",
        "options": options.model_dump(),
        "resolved_options": resolved.model_dump(),
        "status": "queued",
        "stage": "queued",
        "created_at": utcnow(),
        "warnings": merged_warnings,
        "meta": {
            "detection": detection,
            "page_count": detection.get("page_count"),
        },
    }
    await repository.insert_job(db, job)
    storage.ensure_job_dir(settings, job_id)
    storage.append_log(
        settings,
        job_id,
        [
            f"[job] {job_id} 创建于 {job['created_at']}",
            f"[job] 文件={filename} 引擎={engine} runner={job['runner']}",
            f"[job] 用户参数={json.dumps(options.model_dump(), ensure_ascii=False)}",
            f"[job] 生效参数={json.dumps(resolved.model_dump(), ensure_ascii=False)}",
            *[f"[job] 提示: {warning}" for warning in merged_warnings],
        ],
    )
    stored = await repository.get_job(db, job_id)
    assert stored is not None
    repository.write_job_snapshot(settings, stored)
    return stored


def _duration_ms(started_at: str | None, meta: dict[str, Any]) -> int:
    if meta.get("duration_ms"):
        return int(meta["duration_ms"])
    if not started_at:
        return 0
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return 0
    return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)


async def finalize_job(settings, db: Database, job_id: str, work_dir: Path) -> None:
    job = await repository.get_job(db, job_id)
    if job is None:
        return
    if job["status"] == "canceled":
        _cleanup(work_dir)
        return

    root = storage.ensure_job_dir(settings, job_id)
    output_src = work_dir / "output"
    raw_src = work_dir / "raw"
    meta_path = output_src / "meta.json"
    if not meta_path.exists():
        raise RuntimeError("解析结果缺少 meta.json")

    storage.copy_tree(output_src, root / "output")
    if raw_src.exists():
        storage.copy_tree(raw_src, root / "raw")

    meta = storage.read_json(meta_path, {}) or {}
    duration = _duration_ms(job.get("started_at"), meta)
    warnings = list(dict.fromkeys([*(job.get("warnings") or []), *(meta.get("warnings") or [])]))
    meta["warnings"] = warnings
    meta["artifacts"] = storage.list_artifacts(settings, job_id)

    await repository.update_job(
        db,
        job_id,
        status="succeeded",
        stage="done",
        progress=1.0,
        finished_at=utcnow(),
        duration_ms=duration,
        error=None,
        warnings_json=warnings,
        meta_json=meta,
        result_markdown_path="output/result.md",
        lease_expires_at=None,
    )
    storage.append_log(settings, job_id, [f"[job] 解析成功，用时 {duration} ms"])
    refreshed = await repository.get_job(db, job_id)
    if refreshed:
        annotation = await repository.get_annotation(db, job_id, None)
        repository.write_job_snapshot(settings, refreshed, annotation)
    _cleanup(work_dir)


async def finalize_bundle(settings, db: Database, job_id: str, bundle_path: Path) -> None:
    incoming = settings.data_path / "tmp" / f"unzip_{job_id}"
    if incoming.exists():
        shutil.rmtree(incoming, ignore_errors=True)
    try:
        storage.extract_bundle(bundle_path, incoming)
        await finalize_job(settings, db, job_id, incoming)
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
        bundle_path.unlink(missing_ok=True)


async def mark_failed(settings, db: Database, job_id: str, error: str, logs: list[str] | None = None) -> None:
    job = await repository.get_job(db, job_id)
    if job is None:
        return
    if logs:
        storage.append_log(settings, job_id, logs)
    storage.append_log(settings, job_id, [f"[error] {error}"])
    if job["status"] == "canceled":
        repository.write_job_snapshot(settings, job)
        return
    duration = _duration_ms(job.get("started_at"), {})
    await repository.update_job(
        db,
        job_id,
        status="failed",
        stage="failed",
        finished_at=utcnow(),
        duration_ms=duration,
        error=error,
        lease_expires_at=None,
    )
    refreshed = await repository.get_job(db, job_id)
    if refreshed:
        repository.write_job_snapshot(settings, refreshed)


async def cancel_job(settings, db: Database, job_id: str) -> dict[str, Any] | None:
    job = await repository.get_job(db, job_id)
    if job is None:
        return None
    if job["status"] in ("succeeded", "failed", "canceled"):
        return job
    await repository.update_job(
        db,
        job_id,
        status="canceled",
        stage="canceled",
        finished_at=utcnow(),
        lease_expires_at=None,
    )
    storage.append_log(settings, job_id, ["[job] 用户取消任务"])
    refreshed = await repository.get_job(db, job_id)
    if refreshed:
        repository.write_job_snapshot(settings, refreshed)
    return refreshed


async def append_progress(
    settings,
    db: Database,
    job_id: str,
    stage: str | None,
    progress: float | None,
    logs: list[str] | None,
) -> None:
    if logs:
        storage.append_log(settings, job_id, logs)
    fields: dict[str, Any] = {}
    if stage:
        fields["stage"] = stage
    if progress is not None:
        fields["progress"] = max(0.0, min(1.0, progress))
    if fields:
        await repository.update_job(db, job_id, **fields)


def _cleanup(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
