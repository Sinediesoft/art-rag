"""工廠資料庫：固定查詢與 Text-to-SQL（唯讀）。"""

from fastapi import APIRouter, Path, Request
from fastapi.responses import StreamingResponse

from app.agent import guard
from app.api import schemas as S
from app.api.deps import ID_PATTERN
from app.core.errors import AppError, safe_stream
from app.rag.text2sql import load_examples, prompt_versions
from app.repositories.index_store import get_store
from app.repositories.inventory_repo import get_inventory_repo
from app.services import (
    access_audit,
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
def part_inventory(request: Request, part_id: str = Path(pattern=ID_PATTERN)):
    """單一圖紙的庫存明細（固定查詢，不經模型）：各倉儲位、未完工工單、未出貨訂單、最近異動。"""
    account = identity.current(request)
    identity.require_domain(account, "factory")
    data = get_inventory_repo().part_inventory(part_id)
    if not data:
        raise AppError("PART_NOT_FOUND", f"庫存資料庫中沒有圖紙 {part_id}", 404)
    access_audit.record(
        account, get_store().get_part(part_id), "庫存明細", request.state.request_id
    )
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
    """Text-to-SQL：中文問題 → 本地模型產生 SQL → 唯讀執行 → 依結果回答。
    直接打這個端點也先過共用的輸入防護（第 2 段的地端 hard-block，擋下回 403 INPUT_BLOCKED、
    不呼叫模型）；SQL 的 SELECT 白名單與唯讀連線照舊。這是工具，沒有檢索段落，
    所以不經過 RAG 的第 3～6 段。"""
    account = identity.current(request)
    identity.require_domain(account, "factory")
    guard.tool_input_check(body.question, account, request.state.request_id, "庫存查詢")
    stream = sql_service.ask_stream(
        question=body.question,
        request_id=request.state.request_id,
        strategy=body.strategy,
        allow_fallback=body.allow_fallback,
        account=account,
    )
    return StreamingResponse(
        safe_stream(stream, request.state.request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
