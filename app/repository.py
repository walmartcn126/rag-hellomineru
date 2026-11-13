"""数据访问层：documents / jobs / workers / annotations。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .storage import job_dir, read_json, write_json


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_before(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


# ---------------------------------------------------------------- documents

async def upsert_document(
    db: Database, sha256: str, filename: str, ext: str, size: int, page_count: int | None
) -> int:
    existing = await db.fetchone("SELECT id FROM documents WHERE sha256 = ?", (sha256,))
    if existing:
        await db.execute(
            "UPDATE documents SET page_count = COALESCE(?, page_count) WHERE id = ?",
            (page_count, existing["id"]),
        )
        return int(existing["id"])
    return await db.execute(
        "INSERT INTO documents (sha256, filename, ext, size, page_count, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sha256, filename, ext, size, page_count, utcnow()),
    )


async def get_document(db: Database, document_id: int) -> dict[str, Any] | None:
    return await db.fetchone("SELECT * FROM documents WHERE id = ?", (document_id,))


async def get_document_by_sha(db: Database, sha256: str) -> dict[str, Any] | None:
    return await db.fetchone("SELECT * FROM documents WHERE sha256 = ?", (sha256,))


async def list_documents(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return await db.fetchall(
        """
        SELECT d.*,
               COUNT(j.id) AS run_count,
               SUM(CASE WHEN j.status = 'succeeded' THEN 1 ELSE 0 END) AS success_count,
               (SELECT ROUND(AVG(a.total), 2) FROM annotations a
                  JOIN jobs jj ON jj.id = a.job_id WHERE jj.document_id = d.id) AS avg_score
        FROM documents d
        LEFT JOIN jobs j ON j.document_id = d.id
        GROUP BY d.id
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


async def list_document_runs(db: Database, document_id: int) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        """
        SELECT j.*, a.total AS score_total, a.status AS annotation_status
        FROM jobs j
        LEFT JOIN annotations a ON a.job_id = j.id
        WHERE j.document_id = ?
        ORDER BY j.created_at DESC
        """,
        (document_id,),
    )
    return [_job_row_to_dict(row) for row in rows]


# --------------------------------------------------------------------- jobs

JOB_FIELDS = {
    "status", "stage", "progress", "started_at", "finished_at", "duration_ms",
    "worker_id", "attempts", "lease_expires_at", "error", "warnings_json",
    "result_markdown_path", "meta_json",
}


async def insert_job(db: Database, job: dict[str, Any]) -> None:
    await db.execute(
        """
        INSERT INTO jobs (id, document_id, filename, ext, engine, runner, options_json,
                          resolved_options_json, status, stage, created_at, attempts, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            job["id"],
            job["document_id"],
            job["filename"],
            job["ext"],
            job["engine"],
            job["runner"],
            _dumps(job["options"]),
            _dumps(job["resolved_options"]),
            job.get("status", "queued"),
            job.get("stage", "queued"),
            job["created_at"],
            _dumps(job.get("meta", {})),
        ),
    )


async def get_job(db: Database, job_id: str) -> dict[str, Any] | None:
    row = await db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return _job_row_to_dict(row) if row else None


def _job_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["options"] = _loads(data.pop("options_json", None), {})
    data["resolved_options"] = _loads(data.pop("resolved_options_json", None), {})
    data["warnings"] = _loads(data.pop("warnings_json", None), [])
    data["meta"] = _loads(data.pop("meta_json", None), {})
    return data


async def update_job(db: Database, job_id: str, **fields: Any) -> None:
    payload = {key: value for key, value in fields.items() if key in JOB_FIELDS}
    if not payload:
        return
    for key in ("warnings_json", "meta_json"):
        if key in payload and not isinstance(payload[key], str):
            payload[key] = _dumps(payload[key])
    assignments = ", ".join(f"{key} = ?" for key in payload)
    await db.execute(
        f"UPDATE jobs SET {assignments} WHERE id = ?",
        (*payload.values(), job_id),
    )


async def list_jobs(
    db: Database,
    status: str | None = None,
    engine: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("j.status = ?")
        params.append(status)
    if engine:
        clauses.append("j.engine = ?")
        params.append(engine)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.fetchall(
        f"""
        SELECT j.*, d.sha256, d.page_count,
               (SELECT ROUND(AVG(a.total), 2) FROM annotations a WHERE a.job_id = j.id) AS score_avg,
               (SELECT COUNT(*) FROM annotations a WHERE a.job_id = j.id) AS score_count,
               (SELECT a.status FROM annotations a WHERE a.job_id = j.id ORDER BY a.updated_at DESC LIMIT 1) AS annotation_status
        FROM jobs j
        LEFT JOIN documents d ON d.id = j.document_id
        {where}
        ORDER BY j.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return [_job_row_to_dict(row) | {
        "sha256": row.get("sha256"),
        "page_count": row.get("page_count"),
        "score_avg": row.get("score_avg"),
        "score_count": row.get("score_count"),
        "annotation_status": row.get("annotation_status"),
    } for row in rows]


async def claim_job(
    db: Database,
    worker_id: str,
    capabilities: dict[str, Any],
    lease_sec: int,
    max_attempts: int,
) -> dict[str, Any] | None:
    engines = set(capabilities.get("engines") or [])
    backends = set(capabilities.get("backends") or [])
    async with db.immediate() as conn:
        cursor = await conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 30"
        )
        candidates = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        chosen: dict[str, Any] | None = None
        for row in candidates:
            if row["engine"] not in engines:
                continue
            resolved = _loads(row["resolved_options_json"], {})
            backend = (resolved.get("mineru") or {}).get("backend")
            if row["engine"] == "mineru" and backend and backend not in backends:
                continue
            chosen = row
            break
        if chosen is None:
            return None
        if int(chosen["attempts"]) >= max_attempts:
            await conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                ("超过最大重试次数", utcnow(), chosen["id"]),
            )
            await conn.commit()
            return None
        lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_sec)).isoformat(timespec="seconds")
        await conn.execute(
            """
            UPDATE jobs SET status='running', stage='claimed', worker_id=?, started_at=?,
                            lease_expires_at=?, attempts = attempts + 1
            WHERE id=? AND status='queued'
            """,
            (worker_id, utcnow(), lease, chosen["id"]),
        )
        await conn.commit()
    job = await get_job(db, chosen["id"])
    return job


async def touch_lease(db: Database, job_id: str, lease_sec: int) -> None:
    lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_sec)).isoformat(timespec="seconds")
    await db.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (lease, job_id))


async def release_expired_jobs(db: Database, max_attempts: int) -> list[str]:
    now = utcnow()
    expired = await db.fetchall(
        "SELECT id, attempts FROM jobs WHERE status='running' AND lease_expires_at IS NOT NULL"
        " AND lease_expires_at < ?",
        (now,),
    )
    recovered: list[str] = []
    for row in expired:
        if int(row["attempts"]) >= max_attempts:
            await db.execute(
                "UPDATE jobs SET status='failed', stage='failed', error=?, finished_at=?, worker_id=NULL"
                " WHERE id=? AND status='running'",
                ("租约过期且超过最大重试次数", now, row["id"]),
            )
        else:
            await db.execute(
                "UPDATE jobs SET status='queued', stage='queued', worker_id=NULL, started_at=NULL,"
                " lease_expires_at=NULL WHERE id=? AND status='running'",
                (row["id"],),
            )
        recovered.append(row["id"])
    return recovered


async def job_status_counts(db: Database) -> dict[str, int]:
    rows = await db.fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    counts = {row["status"]: int(row["n"]) for row in rows}
    for key in ("queued", "running", "succeeded", "failed", "canceled"):
        counts.setdefault(key, 0)
    return counts


async def delete_job(db: Database, job_id: str) -> None:
    await db.execute("DELETE FROM annotation_items WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM annotations WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


async def reset_interrupted_jobs(db: Database) -> int:
    """协调节点启动时把遗留的 running/queued 任务标记失败（避免悬挂）。"""
    rows = await db.fetchall("SELECT id FROM jobs WHERE status IN ('running', 'queued')")
    for row in rows:
        await db.execute(
            "UPDATE jobs SET status='failed', stage='failed', error=?, finished_at=? WHERE id=?",
            ("服务重启导致任务中断", utcnow(), row["id"]),
        )
    return len(rows)


# ------------------------------------------------------------------ workers

async def register_worker(
    db: Database, name: str, hostname: str, capabilities: dict[str, Any], version: str
) -> str:
    worker_id = f"{name}-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "UPDATE workers SET status='offline', current_job_id=NULL WHERE name = ? AND status='online'",
        (name,),
    )
    await db.execute(
        """
        INSERT INTO workers (id, name, hostname, capabilities_json, status, last_heartbeat,
                             current_job_id, version, registered_at)
        VALUES (?, ?, ?, ?, 'online', ?, NULL, ?, ?)
        """,
        (worker_id, name, hostname, _dumps(capabilities), utcnow(), version, utcnow()),
    )
    return worker_id


async def heartbeat_worker(db: Database, worker_id: str, current_job_id: str | None) -> None:
    await db.execute(
        "UPDATE workers SET last_heartbeat=?, current_job_id=?, status='online' WHERE id=?",
        (utcnow(), current_job_id, worker_id),
    )


async def get_worker(db: Database, worker_id: str) -> dict[str, Any] | None:
    row = await db.fetchone("SELECT * FROM workers WHERE id = ?", (worker_id,))
    return _worker_to_dict(row) if row else None


async def list_workers(db: Database, stale_seconds: int = 30) -> list[dict[str, Any]]:
    await db.execute(
        "UPDATE workers SET status='offline' WHERE status='online' AND last_heartbeat < ?",
        (utc_before(stale_seconds),),
    )
    rows = await db.fetchall(
        """
        SELECT w.*,
               (SELECT COUNT(*) FROM jobs j WHERE j.worker_id = w.id AND j.status='succeeded') AS completed,
               (SELECT COUNT(*) FROM jobs j WHERE j.worker_id = w.id AND j.status='failed') AS failed
        FROM workers w ORDER BY w.status, w.last_heartbeat DESC
        """
    )
    return [_worker_to_dict(row) | {
        "completed": row.get("completed", 0),
        "failed": row.get("failed", 0),
    } for row in rows]


def _worker_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["capabilities"] = _loads(data.pop("capabilities_json", None), {})
    return data


# --------------------------------------------------------------- annotations

SCORE_KEYS = ("text_accuracy", "reading_order", "table", "formula", "image")


async def get_annotation(db: Database, job_id: str, annotator: str | None) -> dict[str, Any] | None:
    if annotator:
        row = await db.fetchone(
            "SELECT * FROM annotations WHERE job_id=? AND annotator=?", (job_id, annotator)
        )
    else:
        row = await db.fetchone(
            "SELECT * FROM annotations WHERE job_id=? ORDER BY updated_at DESC LIMIT 1", (job_id,)
        )
    return _annotation_to_dict(row) if row else None


def _annotation_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["scores"] = _loads(data.pop("scores_json", None), {})
    return data


async def list_job_annotations(db: Database, job_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM annotations WHERE job_id=? ORDER BY updated_at DESC", (job_id,)
    )
    return [_annotation_to_dict(row) for row in rows]


async def upsert_annotation(
    db: Database,
    job_id: str,
    annotator: str,
    scores: dict[str, int],
    total: float,
    comment: str,
    status: str,
) -> dict[str, Any]:
    now = utcnow()
    existing = await db.fetchone(
        "SELECT id, created_at FROM annotations WHERE job_id=? AND annotator=?", (job_id, annotator)
    )
    if existing:
        await db.execute(
            "UPDATE annotations SET scores_json=?, total=?, comment=?, status=?, updated_at=?"
            " WHERE id=?",
            (_dumps(scores), total, comment, status, now, existing["id"]),
        )
        annotation_id = existing["id"]
        created_at = existing["created_at"]
    else:
        annotation_id = uuid.uuid4().hex
        created_at = now
        await db.execute(
            """
            INSERT INTO annotations (id, job_id, annotator, scores_json, total, comment, status,
                                     created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (annotation_id, job_id, annotator, _dumps(scores), total, comment, status, created_at, now),
        )
    return {
        "id": annotation_id,
        "job_id": job_id,
        "annotator": annotator,
        "scores": scores,
        "total": total,
        "comment": comment,
        "status": status,
        "created_at": created_at,
        "updated_at": now,
    }


async def list_annotation_items(db: Database, job_id: str) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT * FROM annotation_items WHERE job_id=? ORDER BY page_idx, created_at", (job_id,)
    )


async def add_annotation_item(
    db: Database, job_id: str, annotation_id: str | None, page_idx: int, block_ref: str,
    label: str, comment: str,
) -> dict[str, Any]:
    item_id = uuid.uuid4().hex
    created = utcnow()
    await db.execute(
        """
        INSERT INTO annotation_items (id, job_id, annotation_id, page_idx, block_ref, label,
                                      comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, job_id, annotation_id, page_idx, block_ref, label, comment, created),
    )
    return {
        "id": item_id,
        "job_id": job_id,
        "annotation_id": annotation_id,
        "page_idx": page_idx,
        "block_ref": block_ref,
        "label": label,
        "comment": comment,
        "created_at": created,
    }


async def delete_annotation_item(db: Database, job_id: str, item_id: str) -> bool:
    row = await db.fetchone(
        "SELECT id FROM annotation_items WHERE id=? AND job_id=?", (item_id, job_id)
    )
    if not row:
        return False
    await db.execute("DELETE FROM annotation_items WHERE id=?", (item_id,))
    return True


async def list_annotations(
    db: Database,
    engine: str | None = None,
    annotator: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if engine:
        clauses.append("j.engine = ?")
        params.append(engine)
    if annotator:
        clauses.append("a.annotator = ?")
        params.append(annotator)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.fetchall(
        f"""
        SELECT a.*, j.filename, j.engine, j.resolved_options_json, j.duration_ms
        FROM annotations a JOIN jobs j ON j.id = a.job_id
        {where}
        ORDER BY a.updated_at DESC LIMIT ?
        """,
        (*params, limit),
    )
    result = []
    for row in rows:
        item = _annotation_to_dict(row)
        item["resolved_options"] = _loads(item.pop("resolved_options_json", None), {})
        item.pop("scores_json", None)
        result.append(item)
    return result


ALLOWED_GROUP_COLUMNS = {
    "engine": "j.engine",
    "backend": "CASE WHEN j.engine = 'mineru' THEN json_extract(j.resolved_options_json, '$.mineru.backend') END",
    "method": "CASE WHEN j.engine = 'mineru' THEN json_extract(j.resolved_options_json, '$.mineru.method') END",
    "runner": "j.runner",
    "annotator": "a.annotator",
}


async def score_stats(db: Database, group_by: list[str]) -> list[dict[str, Any]]:
    columns = [col for col in group_by if col in ALLOWED_GROUP_COLUMNS]
    if not columns:
        columns = ["engine", "backend", "method"]
    selects = [f"{ALLOWED_GROUP_COLUMNS[col]} AS {col}" for col in columns]
    group_expr = ", ".join(str(index + 1) for index in range(len(columns)))
    avg_columns = ", ".join(
        f"ROUND(AVG(json_extract(a.scores_json, '$.{key}')), 2) AS avg_{key}" for key in SCORE_KEYS
    )
    rows = await db.fetchall(
        f"""
        SELECT {', '.join(selects)},
               COUNT(a.id) AS samples,
               ROUND(AVG(a.total), 2) AS avg_total,
               {avg_columns}
        FROM annotations a JOIN jobs j ON j.id = a.job_id
        GROUP BY {group_expr}
        ORDER BY avg_total DESC
        """
    )
    return rows


async def annotation_export_rows(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        """
        SELECT a.annotator, a.total, a.comment, a.status AS annotation_status,
               a.scores_json, a.created_at, a.updated_at,
               j.id AS job_id, j.filename, j.engine, j.runner, j.resolved_options_json,
               j.duration_ms, d.sha256, d.page_count
        FROM annotations a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN documents d ON d.id = j.document_id
        ORDER BY a.updated_at DESC
        """
    )
    result = []
    for row in rows:
        scores = _loads(row.pop("scores_json", None), {})
        resolved = _loads(row.pop("resolved_options_json", None), {})
        mineru = resolved.get("mineru") or {}
        result.append({
            **row,
            "annotator": row["annotator"],
            "backend": mineru.get("backend"),
            "method": mineru.get("method"),
            "lang": mineru.get("lang"),
            "formula_enable": mineru.get("formula_enable"),
            "table_enable": mineru.get("table_enable"),
            "start_page_id": mineru.get("start_page_id"),
            "end_page_id": mineru.get("end_page_id"),
            **{key: scores.get(key) for key in SCORE_KEYS},
        })
    return result


def write_job_snapshot(settings, job: dict[str, Any], annotation: dict[str, Any] | None = None) -> None:
    snapshot = {
        "job": job,
        "annotation": annotation,
    }
    write_json(job_dir(settings, job["id"]) / "job.json", snapshot)


def write_annotation_snapshot(settings, job_id: str, annotation: dict[str, Any],
                              items: list[dict[str, Any]]) -> None:
    payload = {**annotation, "items": items}
    write_json(job_dir(settings, job_id) / "annotations" / f"{annotation['id']}.json", payload)


def load_job_snapshot(settings, job_id: str) -> dict[str, Any] | None:
    return read_json(job_dir(settings, job_id) / "job.json")
