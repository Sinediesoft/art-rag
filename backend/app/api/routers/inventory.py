"""工廠資料庫：固定查詢與 Text-to-SQL（唯讀）。"""

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api import schemas as S
from app.core.errors import AppError
from app.rag.text2sql import load_examples, prompt_versions
from app.repositories.inventory_repo import get_inventory_repo
from app.services import (
    identity,
    sql_service,
)

router = APIRouter()


@router.get("/inventory/schema", response_model=S.InventorySchemaResponse, tags=["inventory"])
def inventory_schema(request: Request):
    """庫存資料庫的資料表與欄位說明（與給模型看的 schema 同一份來源）。"""
    identity.require_domain(identity.current(request), "factory")
    repo = get_inventory_repo()
    repo.ensure_built()
    version = prompt_versions()[0]
    return {
        **repo.schema_info(),
        "prompt_version": version,
        "examples": [e["question"] for e in load_examples(version)],
    }


@router.get("/inventory/overview", response_model=S.InventoryOverviewResponse, tags=["inventory"])
def inventory_overview(request: Request):
    identity.require_domain(identity.current(request), "factory")
    repo = get_inventory_repo()
    items = repo.overview()
    return {"as_of": repo.as_of, "company": repo.manifest.get("company", ""), "items": items}


@router.get("/inventory/parts/{part_id}", response_model=S.PartInventory, tags=["inventory"])
def part_inventory(part_id: str, request: Request):
    """單一圖紙的庫存明細（固定查詢，不經模型）：各倉儲位、未完工工單、未出貨訂單、最近異動。"""
    identity.require_domain(identity.current(request), "factory")
    data = get_inventory_repo().part_inventory(part_id)
    if not data:
        raise AppError("PART_NOT_FOUND", f"庫存資料庫中沒有圖紙 {part_id}", 404)
    return data


@router.post(
    "/inventory/ask",
    tags=["inventory"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：meta／attempt／sql_token／sql／result／token／done／error"
            "（見 shared/sse_events.md）",
        }
    },
)
async def inventory_ask(body: S.InventoryAskRequest, request: Request):
    """Text-to-SQL：中文問題 → 本地模型產生 SQL → 唯讀執行 → 依結果回答。"""
    identity.require_domain(identity.current(request), "factory")
    stream = sql_service.ask_stream(
        question=body.question,
        request_id=request.state.request_id,
        strategy=body.strategy,
        allow_fallback=body.allow_fallback,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
