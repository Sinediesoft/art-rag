"""共用工具：批次辨識、並排比較、匯出稽核、照片建檔。"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api import schemas as S
from app.core.config import (
    get_models_config,
)
from app.core.errors import AppError
from app.repositories.index_store import get_store
from app.repositories.production_repo import get_production_repo
from app.services import (
    batch_service,
    identity,
    intake_service,
    item_compare_service,
)

router = APIRouter()


# ------------------------------------------------- 批次辨識、兩件並排比較、匯出（docs/adr/017）
@router.post(
    "/batch/identify",
    tags=["tools"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：每張照片一個 row，最後 done（見 shared/sse_events.md）",
        }
    },
)
async def batch_identify(body: S.BatchIdentifyRequest, request: Request):
    """一次辨識一批照片（典藏盤點、舊圖紙歸檔）。每張照片照資料範圍處理：
    看不到的圖紙那一列標「目前身分看不到」、不透露是哪一張，不讓整批失敗。"""
    account = identity.current(request)
    limit = get_models_config().batch.max_images
    if len(body.image_ids) > limit:
        n = len(body.image_ids)
        raise AppError("VALIDATION_ERROR", f"一批最多 {limit} 張（這批 {n} 張）", 422)
    if body.domain == "mfg":
        identity.require_domain(account, "mfg")
    stream = batch_service.batch_stream(
        body.image_ids, body.domain, account, request.state.request_id
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/compare/items", response_model=S.ItemComparison, tags=["tools"])
def compare_items(
    request: Request,
    a: str = Query(pattern=item_compare_service.REF_PATTERN, examples=["part:mfg-001"]),
    b: str = Query(pattern=item_compare_service.REF_PATTERN, examples=["part:mfg-002"]),
):
    """兩幅畫或兩張圖紙並排比較：逐欄並排、標出不同、每格附出處；直接讀知識庫，不呼叫模型。
    圖紙照資料範圍（一般員工不能用工廠圖紙、業務看不到機密圖紙 → 403）。"""
    return item_compare_service.table(a, b, identity.current(request))


@router.post(
    "/compare/summary",
    tags=["tools"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：sources／token／done／error（見 shared/sse_events.md）",
        }
    },
)
async def compare_summary(body: S.CompareSummaryRequest, request: Request):
    """兩件並排比較的差異摘要：本地生成端依比較表與兩邊的知識段落寫一段，每句附 [編號]。"""
    stream = item_compare_service.summary_stream(
        body.a, body.b, identity.current(request), request.state.request_id
    )
    first = await anext(stream)  # 權限、找不到在這裡就丟 AppError（回 4xx，不是串流裡的 error）
    return StreamingResponse(
        _prepend(first, stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _prepend(first: str, rest):
    yield first
    async for e in rest:
        yield e


@router.post("/exports", response_model=S.OkResponse, tags=["tools"])
def log_export(body: S.ExportAuditRequest, request: Request):
    """前端匯出 CSV、比較表或問答報告時記一筆稽核（action＝匯出）。
    涉及的圖紙也要看得到：看不到的 id 不會出現在前端，出現了就是有人繞過畫面 → 403。"""
    account = identity.current(request)
    store = get_store()
    for ref in body.refs:
        if (p := store.get_part(ref)) is not None:
            identity.require_part(account, p)
    labels = {"batch_csv": "批次辨識 CSV", "compare": "兩件並排比較", "qa_report": "問答報告"}
    unit = {"batch_csv": "列", "compare": "欄", "qa_report": "則"}[body.kind]
    get_production_repo().add_audit(
        {"actor_id": account.id, "actor_label": account.label, "action": "匯出",
         "op": f"export_{body.kind}", "ref_no": ",".join(body.refs[:5]) or None,
         "summary": f"匯出{labels[body.kind]}" + (f"〈{body.title}〉" if body.title else "")
         + f"（{body.rows} {unit}）",
         "detail": {"refs": body.refs, "rows": body.rows},
         "request_id": request.state.request_id}
    )  # fmt: skip
    return S.OkResponse()


# ---------------------------------------------------------------- 照片建檔（docs/adr/013）
@router.post(
    "/intake",
    tags=["intake"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：stage／token／draft／done／error（見 shared/sse_events.md）",
        }
    },
)
async def create_intake(body: S.IntakeRequest, request: Request):
    """拍照 → 擋模糊 → 確認知識庫還沒有 → 建檔草稿（任何身分都可以；收錄要主管）。

    圖紙由本地 Qwen3-VL 讀標題欄；畫作不讀照片，欄位由人在表單填。
    """
    stream = intake_service.intake_stream(
        body.image_id, request.state.request_id, body.domain, identity.current(request)
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/intake/{draft_id}", response_model=S.IntakeDraft, tags=["intake"])
def get_intake(draft_id: str, request: Request):
    """建檔的人與能收錄的身分（主管）才讀得到；其他人和不存在同一個 404。"""
    return intake_service.get_draft(draft_id, identity.current(request))


@router.put("/intake/{draft_id}", response_model=S.IntakeDraft, tags=["intake"])
def update_intake(draft_id: str, body: S.IntakeUpdate, request: Request):
    """人在確認頁修改欄位；改過的欄位來源標成「人」，並重新驗證。"""
    return intake_service.update_draft(draft_id, body.values, identity.current(request))


@router.post("/intake/{draft_id}/commit", response_model=S.IntakeDraft, tags=["intake"])
def commit_intake(draft_id: str, request: Request):
    """收錄（只有主管）：寫 kb/（圖紙 parts＋drawings、畫作 artworks＋images）、遞增 kb/VERSION，
    背景重建索引。

    回傳時狀態是 indexing；重建完成後 GET 這份草稿會變成 done（或 failed，已還原）。
    """
    return intake_service.commit_draft(
        draft_id, identity.current(request), request.state.request_id
    )


@router.delete("/intake/{draft_id}", response_model=S.OkResponse, tags=["intake"])
def discard_intake(draft_id: str, request: Request):
    intake_service.discard_draft(draft_id, identity.current(request))
    return S.OkResponse()


@router.get("/intake/{draft_id}/{name}", response_class=FileResponse, tags=["intake"])
def get_intake_file(draft_id: str, name: str, request: Request):
    """草稿的原照片（photo.jpg）、要存進知識庫的圖（圖紙 drawing.png、畫作 image.jpg）。"""
    path = intake_service.draft_file(draft_id, name, identity.current(request))
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})
