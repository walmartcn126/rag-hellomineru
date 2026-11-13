"""磁盘存储：上传落盘、任务目录、JSON 快照、zip 打包/解包、路径安全。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable

CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    pass


class UnsafePath(Exception):
    pass


def ensure_dirs(settings) -> None:
    for path in (
        settings.data_path,
        settings.uploads_dir,
        settings.jobs_dir,
        settings.worker_dir,
        settings.exports_dir,
        settings.samples_dir,
        settings.data_path / "tmp",
    ):
        path.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    name = Path(name or "document").name
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ()[]")
    cleaned = cleaned.strip().strip(".")
    return cleaned or "document"


def ext_of(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix


async def save_upload(settings, upload_file, max_mb: int) -> dict[str, Any]:
    """流式落盘并计算 sha256，返回 {sha256,size,ext,filename,path}（内容寻址）。"""
    tmp_dir = settings.data_path / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(upload_file.filename or "document")
    ext = ext_of(filename)
    limit = max_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0

    fd, tmp_name = tempfile.mkstemp(prefix="upload_", dir=tmp_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = await upload_file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(f"文件超过上限 {max_mb} MB")
                digest.update(chunk)
                fh.write(chunk)
        sha = digest.hexdigest()
        target_dir = settings.uploads_dir / sha[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (f"{sha}.{ext}" if ext else sha)
        if not target.exists():
            shutil.move(str(tmp_path), target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return {
        "sha256": sha,
        "size": size,
        "ext": ext,
        "filename": filename,
        "path": target,
    }


async def save_temp_upload(settings, upload_file, max_mb: int) -> tuple[Path, str, int]:
    """临时落盘（识别用），返回 (路径, 清洗后的文件名, 大小)。"""
    tmp_dir = settings.data_path / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(upload_file.filename or "document")
    limit = max_mb * 1024 * 1024
    target = tmp_dir / f"{uuid.uuid4().hex}_{filename}"
    size = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await upload_file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(f"文件超过上限 {max_mb} MB")
                fh.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target, filename, size


def upload_path(settings, sha256: str, ext: str) -> Path:
    suffix = f".{ext}" if ext else ""
    return settings.uploads_dir / sha256[:2] / f"{sha256}{suffix}"


def job_dir(settings, job_id: str) -> Path:
    return settings.jobs_dir / job_id


def job_output_dir(settings, job_id: str) -> Path:
    return job_dir(settings, job_id) / "output"


def job_raw_dir(settings, job_id: str) -> Path:
    return job_dir(settings, job_id) / "raw"


def job_log_path(settings, job_id: str) -> Path:
    return job_dir(settings, job_id) / "job.log"


def ensure_job_dir(settings, job_id: str) -> Path:
    root = job_dir(settings, job_id)
    (root / "annotations").mkdir(parents=True, exist_ok=True)
    return root


def worker_workspace(settings, job_id: str) -> Path:
    path = settings.worker_dir / job_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    (path / "output" / "images").mkdir(parents=True, exist_ok=True)
    (path / "raw").mkdir(parents=True, exist_ok=True)
    return path


def safe_child(base: Path, relative: str) -> Path:
    base_resolved = base.resolve()
    target = (base_resolved / relative).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise UnsafePath(relative) from exc
    return target


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def append_log(settings, job_id: str, lines: Iterable[str]) -> None:
    path = job_log_path(settings, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as fh:
        for line in lines:
            fh.write(line.rstrip("\r\n") + "\n")


def read_log(settings, job_id: str, offset: int = 0, limit: int = 500) -> dict[str, Any]:
    path = job_log_path(settings, job_id)
    if not path.exists():
        return {"lines": [], "next_offset": 0}
    size = path.stat().st_size
    offset = max(0, min(offset, size))
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(CHUNK * 2)
        next_offset = offset + len(data)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > limit:
        lines = lines[-limit:]
    return {"lines": lines, "next_offset": next_offset}


def copy_tree(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)


def list_artifacts(settings, job_id: str) -> list[dict[str, Any]]:
    root = job_dir(settings, job_id)
    artifacts: list[dict[str, Any]] = []
    for sub in ("output", "raw"):
        base = root / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            artifacts.append(
                {
                    "name": path.name,
                    "rel": rel,
                    "kind": _kind_of(path),
                    "size": path.stat().st_size,
                    "url": f"/api/jobs/{job_id}/files/{rel}",
                }
            )
    return artifacts


def _kind_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".json":
        return "json"
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"):
        return "image"
    return "file"


def zip_dirs(work_dir: Path, dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for sub in ("output", "raw"):
            base = work_dir / sub
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(work_dir).as_posix())
    return dest


def extract_bundle(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise UnsafePath(name)
            target = (dest_resolved / name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as exc:
                raise UnsafePath(name) from exc
        zf.extractall(dest_resolved)
