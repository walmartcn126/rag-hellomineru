"""启动入口：python -m app（协调节点 + 本地 worker / 或纯 worker）。"""

from __future__ import annotations

import asyncio

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    if settings.role == "worker":
        from .worker.agent import run_standalone_worker

        asyncio.run(run_standalone_worker())
        return
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
