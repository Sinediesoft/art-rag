"""共用：圖文問答（七段 RAG 管線）與回饋。"""

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api import schemas as S
from app.core.config import (
    get_settings,
)
from app.core.errors import AppError, safe_stream
from app.repositories.logs_repo import get_logs_repo
from app.services import (
    chat_service,
    identity,
    uploads,
)

router = APIRouter()


@router.post(
    "/chat",
    tags=["chat"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：sources／token／done／error",
        }
    },
)
async def chat(body: S.ChatRequest, request: Request):
    """圖文問答：七段權限控管每一段都由伺服器執行（docs/adr/015、019），請求欄位都不能略過關卡。
    角色不能用工廠圖紙（一般員工）→ 403；指定了看不到或不存在的圖紙 → 串流裡降級「查無資料」
    （兩者回應一樣，不透露有沒有這份文件），不讀圖檔、不呼叫任何模型。
    inject 只在受信任的評估模式開放；send_image 與 conflict_check 不會略過安全關卡。"""
    account = identity.current(request)
    eval_mode = identity.eval_allowed(request)
    if body.inject and (not get_settings().eval_injection or not eval_mode):
        raise AppError(
            "FORBIDDEN",
            "干擾段落注入只在受信任的評估模式開放（EVAL_INJECTION=true、EVAL_CONTROLS=true）",
            403,
        )
    if body.part_id:
        identity.require_domain(account, "mfg")
    if body.image_id:
        uploads.authorize(
            body.image_id, account
        )  # 第 1 段：不是自己的照片 → 404，不讀圖、不呼叫模型
    stream = chat_service.chat_stream(
        question=body.question,
        request_id=request.state.request_id,
        strategy=body.strategy,
        artwork_id=body.artwork_id,
        image_id=body.image_id,
        use_retrieval=body.use_retrieval,
        allow_fallback=body.allow_fallback,
        part_id=body.part_id,
        rearrange=body.rearrange,
        account=account,
        route_ticket=body.route_ticket,
        eval_mode=eval_mode,
        inject=[d.model_dump() for d in body.inject] if body.inject else None,
        send_image=body.send_image,
        conflict_check=body.conflict_check,
    )
    return StreamingResponse(
        safe_stream(stream, request.state.request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/feedback", response_model=S.OkResponse, tags=["chat"])
def feedback(body: S.FeedbackRequest):
    get_logs_repo().add_feedback(body.request_id, body.rating, body.note)
    return S.OkResponse()
