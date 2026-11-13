"""FastAPI 应用入口：初始化数据库、按角色启动 worker、挂载静态工作台。"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import repository, storage
from .api import annotations, documents, jobs, meta, workers
from .config import get_settings
from .db import Database
from .worker.agent import LocalTransport, WorkerAgent

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _maintenance_loop(settings, db: Database) -> None:
    while True:
        try:
            recovered = await repository.release_expired_jobs(db, settings.max_attempts)
            if recovered:
                print(f"[maintenance] 回收过期租约任务: {recovered}")
            await repository.list_workers(db)
        except Exception as exc:  # pragma: no cover - 维护循环不应中断服务
            print(f"[maintenance] 失败: {exc}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    storage.ensure_dirs(settings)
    db = Database(settings.db_path)
    await db.connect()
    app.state.settings = settings
    app.state.db = db
    app.state.worker_agent = None
    app.state.worker_task = None

    if settings.role in ("coordinator", "both"):
        await repository.reset_interrupted_jobs(db)
        print(
            f"[HelloMinerU] 角色={settings.role} 数据目录={settings.data_path} "
            f"worker_token={settings.resolved_worker_token()}"
        )

    maintenance = asyncio.create_task(_maintenance_loop(settings, db))

    if settings.role == "both":
        agent = WorkerAgent(settings, LocalTransport(settings, db))
        app.state.worker_agent = agent
        app.state.worker_task = asyncio.create_task(agent.run())

    try:
        yield
    finally:
        maintenance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance
        agent = app.state.worker_agent
        if agent is not None:
            agent.stop()
            task = app.state.worker_task
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
        await db.close()


app = FastAPI(title="HelloMinerU", version="0.1.0", lifespan=lifespan)

app.include_router(meta.router)
app.include_router(jobs.router)
app.include_router(workers.router)
app.include_router(annotations.router)
app.include_router(documents.router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    html = STATIC_DIR / "index.html"
    if not html.exists():
        return JSONResponse({"message": "HelloMinerU", "docs": "/docs"})
    return FileResponse(html)


@app.exception_handler(storage.UnsafePath)
async def unsafe_path_handler(request: Request, exc: storage.UnsafePath) -> JSONResponse:
    return JSONResponse({"detail": "非法路径"}, status_code=400)
