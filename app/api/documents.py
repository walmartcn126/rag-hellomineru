"""文档接口：按 sha256 聚合，查看同一文档的多次解析运行（用于对比评分）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .. import repository

router = APIRouter(prefix="/api", tags=["documents"])


@router.get("/documents")
async def list_documents(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict:
    documents = await repository.list_documents(request.app.state.db, limit)
    return {"documents": documents}


@router.get("/documents/{sha256}/runs")
async def document_runs(request: Request, sha256: str) -> dict:
    db = request.app.state.db
    document = await repository.get_document_by_sha(db, sha256)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    runs = await repository.list_document_runs(db, int(document["id"]))
    return {"document": document, "runs": runs}
