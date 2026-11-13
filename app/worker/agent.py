"""Worker 代理：注册、心跳、拉取任务、执行解析、回传结果。"""

from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import threading
from pathlib import Path
from typing import Any, Protocol

import httpx

from .. import repository, storage
from ..config import Settings, get_settings
from ..coordinator import queue as coordinator_queue
from ..db import Database
from ..models import BACKENDS, ENGINES, ParseOptions
from ..parsing.base import JobCanceled
from .executor import execute_parse

APP_VERSION = "0.1.0"


def worker_capabilities(settings: Settings) -> dict[str, Any]:
    return {
        "engines": [engine for engine in ENGINES if engine != "auto"],
        "backends": list(BACKENDS),
        "device": settings.mineru_device or "auto",
    }


class Transport(Protocol):
    async def register(self, name: str, hostname: str, capabilities: dict[str, Any], version: str) -> str: ...
    async def heartbeat(self, worker_id: str, current_job_id: str | None) -> dict[str, Any] | None: ...
    async def claim(self, worker_id: str) -> dict[str, Any] | None: ...
    async def fetch_source(self, job: dict[str, Any], work_dir: Path) -> Path: ...
    async def report(self, job_id: str, stage: str | None, progress: float | None, logs: list[str]) -> None: ...
    async def complete(self, job_id: str, work_dir: Path) -> None: ...
    async def fail(self, job_id: str, error: str, logs: list[str] | None = None) -> None: ...
    async def close(self) -> None: ...


class HttpTransport:
    """跨进程/跨机器：通过协调节点 HTTP 接口交互。"""

    def __init__(self, settings: Settings) -> None:
        base_url = settings.resolved_coordinator_url()
        if not base_url:
            raise RuntimeError("worker 角色需要配置 HMU_COORDINATOR_URL")
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-HMU-Token": settings.resolved_worker_token()},
            timeout=httpx.Timeout(60.0, read=1800.0, write=1800.0),
        )

    async def register(self, name, hostname, capabilities, version) -> str:
        response = await self._client.post(
            "/api/workers/register",
            json={
                "name": name,
                "hostname": hostname,
                "capabilities": capabilities,
                "version": version,
            },
        )
        response.raise_for_status()
        return response.json()["worker_id"]

    async def heartbeat(self, worker_id, current_job_id):
        response = await self._client.post(
            f"/api/workers/{worker_id}/heartbeat",
            json={"current_job_id": current_job_id},
        )
        response.raise_for_status()
        return response.json()

    async def claim(self, worker_id):
        response = await self._client.post(f"/api/workers/{worker_id}/claim")
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    async def fetch_source(self, job, work_dir: Path) -> Path:
        target = work_dir / job["filename"]
        async with self._client.stream(
            "GET", f"/api/worker/tasks/{job['id']}/file"
        ) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    handle.write(chunk)
        return target

    async def report(self, job_id, stage, progress, logs) -> None:
        await self._client.post(
            f"/api/worker/tasks/{job_id}/progress",
            json={"stage": stage, "progress": progress, "logs": logs},
        )

    async def complete(self, job_id, work_dir: Path) -> None:
        bundle = work_dir / "result.zip"
        storage.zip_dirs(work_dir, bundle)
        with bundle.open("rb") as handle:
            response = await self._client.post(
                f"/api/worker/tasks/{job_id}/result",
                files={"bundle": (f"{job_id}.zip", handle, "application/zip")},
            )
        response.raise_for_status()

    async def fail(self, job_id, error, logs=None) -> None:
        try:
            await self._client.post(
                f"/api/worker/tasks/{job_id}/fail",
                json={"error": error, "logs": logs or []},
            )
        except httpx.HTTPError:
            pass

    async def close(self) -> None:
        await self._client.aclose()


class LocalTransport:
    """单机 both 模式：直接调用队列与存储，避免自我 HTTP 调用。"""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    async def register(self, name, hostname, capabilities, version) -> str:
        return await repository.register_worker(
            self.db, name, hostname, capabilities, version
        )

    async def heartbeat(self, worker_id, current_job_id):
        await repository.heartbeat_worker(self.db, worker_id, current_job_id)
        if not current_job_id:
            return None
        job = await repository.get_job(self.db, current_job_id)
        if job is None:
            return None
        return {"current_job": {"id": job["id"], "status": job["status"]}}

    async def claim(self, worker_id):
        return await repository.claim_job(
            self.db,
            worker_id,
            worker_capabilities(self.settings),
            self.settings.lease_sec,
            self.settings.max_attempts,
        )

    async def fetch_source(self, job, work_dir: Path) -> Path:
        document = await repository.get_document(self.db, int(job["document_id"]))
        if document is None:
            raise RuntimeError("源文档记录不存在")
        path = storage.upload_path(self.settings, document["sha256"], document["ext"])
        if not path.exists():
            raise RuntimeError(f"源文件不存在: {path}")
        return path

    async def report(self, job_id, stage, progress, logs) -> None:
        await coordinator_queue.append_progress(
            self.settings, self.db, job_id, stage, progress, logs
        )

    async def complete(self, job_id, work_dir: Path) -> None:
        await coordinator_queue.finalize_job(self.settings, self.db, job_id, work_dir)

    async def fail(self, job_id, error, logs=None) -> None:
        await coordinator_queue.mark_failed(self.settings, self.db, job_id, error, logs)

    async def close(self) -> None:
        return None


class LogPump:
    """收集解析日志并定期上报（loguru/子进程回调可能来自其他线程）。"""

    def __init__(self, transport: Transport, job_id: str) -> None:
        self.transport = transport
        self.job_id = job_id
        self.stage = "downloading"
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._last_stage = "downloading"
        self._task: asyncio.Task | None = None

    def log(self, message: str) -> None:
        with self._lock:
            self._buffer.append(str(message).rstrip())
            if len(self._buffer) > 5000:
                self._buffer = self._buffer[-5000:]

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    async def flush(self) -> None:
        with self._lock:
            lines, self._buffer = self._buffer, []
        stage = self.stage
        if not lines and stage == self._last_stage:
            return
        self._last_stage = stage
        try:
            await self.transport.report(self.job_id, stage, None, lines)
        except Exception:
            with self._lock:
                self._buffer = lines[-200:] + self._buffer[-200:]

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.flush()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()


class WorkerAgent:
    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings = settings
        self.transport = transport
        self.worker_id: str | None = None
        self._active: dict[str, threading.Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None

    async def run(self) -> None:
        name = self.settings.resolved_worker_name()
        hostname = socket.gethostname() or platform.node()
        capabilities = worker_capabilities(self.settings)
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                self.worker_id = await self.transport.register(
                    name, hostname, capabilities, APP_VERSION
                )
                break
            except Exception as exc:  # 协调节点尚未就绪
                last_error = exc
                print(f"[worker] 注册失败（第 {attempt} 次）: {exc}")
                await asyncio.sleep(3)
        if self.worker_id is None:
            raise RuntimeError(f"worker 注册失败: {last_error}")
        print(f"[worker] 已注册: {self.worker_id} ({name}@{hostname})")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._loop()
        finally:
            await self._shutdown()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._tasks = {task for task in self._tasks if not task.done()}
            if len(self._tasks) < max(1, self.settings.worker_concurrency):
                try:
                    job = await self.transport.claim(self.worker_id or "")
                except Exception as exc:
                    print(f"[worker] 领取任务失败: {exc}")
                    job = None
                if job:
                    task = asyncio.create_task(self._run_job(job))
                    self._tasks.add(task)
                    continue
                await self._sleep(self.settings.claim_poll_sec)
            else:
                await self._sleep(0.5)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                active = list(self._active.items())
                if not active:
                    await self.transport.heartbeat(self.worker_id or "", None)
                for job_id, event in active:
                    info = await self.transport.heartbeat(self.worker_id or "", job_id)
                    status = ((info or {}).get("current_job") or {}).get("status")
                    if status == "canceled":
                        event.set()
            except Exception as exc:
                print(f"[worker] 心跳失败: {exc}")
            await asyncio.sleep(self.settings.heartbeat_sec)

    async def _run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        cancel_event = threading.Event()
        self._active[job_id] = cancel_event
        work_dir = storage.worker_workspace(self.settings, job_id)
        pump = LogPump(self.transport, job_id)
        pump.log(f"[worker] {self.worker_id} 领取任务 {job_id}（{job['filename']}）")
        pump.log(f"[worker] 引擎={job['engine']} runner={job['runner']}")
        pump.start()
        try:
            source = await self.transport.fetch_source(job, work_dir)
            pump.set_stage("parsing")
            options = ParseOptions.model_validate(job["resolved_options"])
            await execute_parse(
                job,
                source,
                work_dir,
                options,
                pump.log,
                lambda: cancel_event.is_set(),
            )
            pump.set_stage("finalizing")
            await pump.flush()
            await self.transport.complete(job_id, work_dir)
        except JobCanceled:
            pump.log("[worker] 任务已取消，已停止解析")
            await pump.stop()
            await self.transport.fail(job_id, "用户取消任务")
        except Exception as exc:
            pump.log(f"[error] {type(exc).__name__}: {exc}")
            await pump.stop()
            await self.transport.fail(job_id, f"{type(exc).__name__}: {exc}")
        else:
            await pump.stop()
        finally:
            self._active.pop(job_id, None)
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _shutdown(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        for event in self._active.values():
            event.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()


async def run_standalone_worker() -> None:
    settings = get_settings()
    storage.ensure_dirs(settings)
    transport = HttpTransport(settings)
    agent = WorkerAgent(settings, transport)
    try:
        await agent.run()
    finally:
        await transport.close()
