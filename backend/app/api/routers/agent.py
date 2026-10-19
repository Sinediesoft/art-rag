"""共用：智慧助理的路由（七段的第 1、2 段）。"""

from fastapi import APIRouter, Request

from app.api import schemas as S
from app.core.errors import AppError
from app.services import (
    agent_service,
    identity,
)

router = APIRouter()


@router.post("/agent/route", response_model=S.RouteResponse, tags=["agent"])
async def agent_route(body: S.RouteRequest, request: Request):
    """七段權限控管的第 1、2 段（docs/adr/015）：JWT 已在閘道驗過 → 個資遮蔽 →
    本地分流判斷要做什麼 →
    角色授權（功能、動作權限）→ Jev Choice（正常查詢／Prompt 注入／無關閒聊）→ 分派到哪個本地模組；
    擋下就拒絕並記錄、閒聊快速短路回覆。"""
    if not body.question.strip() and not body.image_id:
        raise AppError("VALIDATION_ERROR", "請輸入一句話或附一張照片", 422)
    # engine=local（只用地端規則）只在評估模式生效；其他情況由伺服器決定（docs/adr/030）
    engine = body.engine if identity.eval_allowed(request) else "auto"
    return await agent_service.route(
        body.question,
        identity.auth_of(request),
        request.state.request_id,
        image_id=body.image_id,
        forced_intent=body.forced_intent,
        engine=engine,
        domain=body.domain,
    )
