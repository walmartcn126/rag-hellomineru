"""请求/响应模型与解析参数模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

BACKENDS = (
    "pipeline",
    "vlm-http-client",
    "hybrid-http-client",
    "vlm-auto-engine",
    "hybrid-auto-engine",
)
METHODS = ("auto", "txt", "ocr")
LANGS = (
    "ch", "ch_server", "ch_lite", "en", "korean", "japan", "chinese_cht", "ta", "te",
    "ka", "th", "el", "latin", "arabic", "east_slavic", "cyrillic", "devanagari",
)
ENGINES = ("auto", "mineru", "pymupdf", "docx", "pptx", "xlsx")
MODEL_SOURCES = ("huggingface", "modelscope", "local")

PDF_EXTS = ("pdf",)
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif", "jp2")
OFFICE_EXTS = ("docx", "pptx", "xlsx", "xlsm", "csv")
SUPPORTED_EXTS = PDF_EXTS + IMAGE_EXTS + OFFICE_EXTS

ENGINE_EXTS = {
    "pymupdf": ("pdf",),
    "mineru": PDF_EXTS + IMAGE_EXTS,
    "docx": ("docx",),
    "pptx": ("pptx",),
    "xlsx": ("xlsx", "xlsm", "csv"),
}


class MinerUOptions(BaseModel):
    backend: Literal[BACKENDS] = "pipeline"  # type: ignore[valid-type]
    method: Literal[METHODS] = "auto"  # type: ignore[valid-type]
    lang: Literal[LANGS] = "ch"  # type: ignore[valid-type]
    server_url: str | None = None
    start_page_id: int = Field(default=0, ge=0)
    end_page_id: int | None = Field(default=None, ge=0)
    formula_enable: bool = True
    table_enable: bool = True
    device: str | None = None
    virtual_vram: int | None = Field(default=None, ge=1)
    model_source: Literal[MODEL_SOURCES] = "huggingface"  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _check_pages(self) -> "MinerUOptions":
        if self.end_page_id is not None and self.end_page_id < self.start_page_id:
            raise ValueError("end_page_id 不能小于 start_page_id")
        if self.backend.endswith("-client") and not self.server_url:
            # 允许提交，解析时由 MinerU 报错，但给出显式提示
            pass
        return self


class PyMuPDFOptions(BaseModel):
    extract_tables: bool = True
    extract_images: bool = True
    thumbnail_dpi: int = Field(default=110, ge=40, le=300)


class ParseOptions(BaseModel):
    engine: Literal[ENGINES] = "auto"  # type: ignore[valid-type]
    runner: Literal["subprocess", "inprocess"] = "subprocess"
    mineru: MinerUOptions = Field(default_factory=MinerUOptions)
    pymupdf: PyMuPDFOptions = Field(default_factory=PyMuPDFOptions)
    timeout_sec: int | None = Field(default=None, ge=1, le=86400)


class ScoreSet(BaseModel):
    text_accuracy: int = Field(ge=1, le=5)
    reading_order: int = Field(ge=1, le=5)
    table: int = Field(ge=1, le=5)
    formula: int = Field(ge=1, le=5)
    image: int = Field(ge=1, le=5)

    @property
    def total(self) -> float:
        values = [self.text_accuracy, self.reading_order, self.table, self.formula, self.image]
        return round(sum(values) / len(values), 2)


ANNOTATION_LABELS = ("文本错误", "表格错误", "公式错误", "顺序错误", "图片错误", "漏检", "多余")


class AnnotationIn(BaseModel):
    annotator: str = Field(min_length=1, max_length=64)
    scores: ScoreSet
    comment: str = ""
    status: Literal["draft", "submitted"] = "draft"


class AnnotationItemIn(BaseModel):
    page_idx: int = Field(default=0, ge=0)
    block_ref: str = Field(min_length=1, max_length=200)
    label: Literal[ANNOTATION_LABELS] = "文本错误"  # type: ignore[valid-type]
    comment: str = ""


class WorkerCapabilities(BaseModel):
    engines: list[str] = Field(default_factory=lambda: list(ENGINES))
    backends: list[str] = Field(default_factory=lambda: list(BACKENDS))
    device: str | None = None


class WorkerRegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    hostname: str = ""
    capabilities: WorkerCapabilities = Field(default_factory=WorkerCapabilities)
    version: str = ""


class HeartbeatIn(BaseModel):
    current_job_id: str | None = None


class ProgressIn(BaseModel):
    stage: str | None = None
    progress: float | None = None
    logs: list[str] = Field(default_factory=list)


class FailIn(BaseModel):
    error: str
    logs: list[str] = Field(default_factory=list)
