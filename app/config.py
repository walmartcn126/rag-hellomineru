"""应用配置：所有配置项均可通过 HMU_ 前缀的环境变量覆盖。"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HMU_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    role: Literal["both", "coordinator", "worker"] = "both"

    data_dir: Path = Path("./data")
    max_upload_mb: int = 100
    max_bundle_mb: int = 1024
    job_timeout_sec: int = 1800

    default_runner: Literal["subprocess", "inprocess"] = "subprocess"
    mineru_backend: str = "pipeline"
    mineru_method: str = "auto"
    mineru_lang: str = "ch"
    mineru_device: str | None = None
    mineru_model_source: str = "huggingface"

    worker_token: str | None = None
    worker_name: str | None = None
    worker_concurrency: int = 1
    coordinator_url: str | None = None
    claim_poll_sec: float = 2.0
    heartbeat_sec: float = 10.0
    lease_sec: int = 300
    max_attempts: int = 3

    @property
    def data_path(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.data_path / "hellomineru.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_path / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_path / "jobs"

    @property
    def worker_dir(self) -> Path:
        return self.data_path / "worker"

    @property
    def exports_dir(self) -> Path:
        return self.data_path / "exports"

    @property
    def samples_dir(self) -> Path:
        return self.data_path / "samples"

    def resolved_worker_token(self) -> str:
        """未配置时生成一次并持久化到 data/worker_token.txt。"""
        if self.worker_token:
            return self.worker_token
        token_file = self.data_path / "worker_token.txt"
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(24)
        token_file.write_text(token, encoding="utf-8")
        return token

    def resolved_worker_name(self) -> str:
        if self.worker_name:
            return self.worker_name
        return f"{os.environ.get('COMPUTERNAME', 'worker').lower()}-{os.getpid()}"

    def resolved_coordinator_url(self) -> str | None:
        if self.coordinator_url:
            return self.coordinator_url.rstrip("/")
        if self.role == "worker":
            host = self.host
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1"
            return f"http://{host}:{self.port}"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
