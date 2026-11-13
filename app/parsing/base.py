"""解析器协议、上下文与注册表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..models import ParseOptions


class JobCanceled(Exception):
    """任务被取消。"""


@dataclass
class ParseContext:
    job_id: str
    filename: str
    source_path: Path
    work_dir: Path
    options: ParseOptions
    log: Callable[[str], None] = lambda _msg: None
    cancel_check: Callable[[], bool] | None = None

    @property
    def output_dir(self) -> Path:
        return self.work_dir / "output"

    @property
    def images_dir(self) -> Path:
        return self.work_dir / "output" / "images"

    @property
    def stem(self) -> str:
        return Path(self.filename).stem or "document"

    def ensure_output(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class ParseResult:
    markdown: str
    content_list: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Parser(Protocol):
    engine: str
    label: str
    extensions: tuple[str, ...]

    async def parse(self, ctx: ParseContext) -> ParseResult: ...


_REGISTRY: dict[str, Parser] = {}


def register(parser: type | Parser) -> Any:
    """注册解析器；既支持 @register 装饰类，也支持传入实例。"""
    instance = parser() if isinstance(parser, type) else parser
    _REGISTRY[instance.engine] = instance
    return parser


def get_parser(engine: str) -> Parser:
    try:
        return _REGISTRY[engine]
    except KeyError as exc:
        raise ValueError(f"未知解析引擎: {engine}") from exc


def available_engines() -> list[str]:
    return list(_REGISTRY.keys())
