"""共用：資料異動與主管核准（change_service）。"""

from fastapi import APIRouter, Request, Response

from app.api import schemas as S
from app.api.deps import IDEMPOTENCY
from app.services import (
    change_service,
    idempotency,
    identity,
)

router = APIRouter()


def _mark(response: Response, replay: bool) -> None:
    if replay:
        response.headers["Idempotent-Replayed"] = "true"


@router.post("/changes/preview", response_model=S.ChangePreview, tags=["agent"])
async def change_preview(body: S.ChangePreviewRequest, request: Request):
    """修改資料流程 1～4：參數抽取 → 權限判定 → 試算（交易內套用後回滾）→ 額度判斷 → 確認卡。"""
    return await change_service.preview(
        identity.current(request),
        request.state.request_id,
        question=body.question,
        op=body.op,
        params=body.params,
    )


@router.post("/changes/{pending_id}/commit", response_model=S.ChangeCommitted, tags=["agent"])
def change_commit(
    pending_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY,
):
    """按確認：寫入前再驗權限與資料指紋 → 寫異動單與稽核紀錄 → 依資料庫讀回結果回覆。
    帶 Idempotency-Key 時，重送拿到先前的回應（不會重複異動）。"""
    account = identity.current(request)
    out, replay = idempotency.run(
        account.id,
        "change_commit",
        idempotency_key,
        {"pending_id": pending_id},
        lambda: change_service.commit(pending_id, account, request.state.request_id),
        resume=lambda marker: change_service.resume_commit(
            marker, pending_id, account, request.state.request_id
        ),
    )
    _mark(response, replay)
    return out


@router.post("/changes/{pending_id}/request-approval", response_model=S.Approval, tags=["agent"])
def change_request_approval(
    pending_id: str,
    body: S.ApprovalRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY,
):
    """超過額度：建立待核准單（AP-）送主管。帶 Idempotency-Key 時重送不會建立第二張。"""
    account = identity.current(request)
    out, replay = idempotency.run(
        account.id,
        "request_approval",
        idempotency_key,
        {"pending_id": pending_id, "note": body.note},
        lambda: change_service.request_approval(
            pending_id, account, body.note, request.state.request_id
        ),
        resume=lambda marker: change_service.resume_request_approval(
            marker, pending_id, account, request.state.request_id
        ),
    )
    _mark(response, replay)
    return out


@router.get("/approvals", response_model=S.ApprovalsResponse, tags=["agent"])
def list_approvals(request: Request):
    """待核准清單（主管處理）、我的申請、最近的核准紀錄；超過 24 小時的自動失效。
    主管看全部，其他人只看自己送出的申請。"""
    return change_service.list_approvals_for(identity.current(request))


@router.post("/approvals/{ap_no}/approve", response_model=S.ApprovalDecision, tags=["agent"])
def approve(
    ap_no: str,
    body: S.ApprovalRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY,
):
    """主管核准：重新試算比對申請時的資料 → 寫入前再驗權限與資料版本 → 寫入。不接受用對話核准。
    帶 Idempotency-Key 時重送拿到先前的結果；沒帶也不會重複寫入（單據已不是待核准）。"""
    account = identity.current(request)
    out, replay = idempotency.run(
        account.id,
        "approve",
        idempotency_key,
        {"ap_no": ap_no, "note": body.note},
        lambda: change_service.approve(ap_no, account, body.note, request.state.request_id),
        resume=lambda marker: change_service.resume_approve(
            marker, ap_no, account, request.state.request_id
        ),
    )
    _mark(response, replay)
    return out


@router.post("/approvals/{ap_no}/return", response_model=S.ApprovalDecision, tags=["agent"])
def return_approval(ap_no: str, body: S.ReturnRequest, request: Request):
    """主管退回（要附理由，申請人在「我的申請」看得到）。"""
    return change_service.return_(
        ap_no, identity.current(request), body.reason, request.state.request_id
    )
