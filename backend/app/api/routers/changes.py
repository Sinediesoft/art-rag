"""共用：資料異動與主管核准（change_service）。"""

from fastapi import APIRouter, Request

from app.api import schemas as S
from app.services import (
    change_service,
    identity,
)

router = APIRouter()


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
def change_commit(pending_id: str, request: Request):
    """按確認：寫入前再驗權限與資料指紋 → 寫異動單與稽核紀錄 → 依資料庫讀回結果回覆。"""
    return change_service.commit(pending_id, identity.current(request), request.state.request_id)


@router.post("/changes/{pending_id}/request-approval", response_model=S.Approval, tags=["agent"])
def change_request_approval(pending_id: str, body: S.ApprovalRequest, request: Request):
    """超過額度：建立待核准單（AP-）送主管。"""
    return change_service.request_approval(
        pending_id, identity.current(request), body.note, request.state.request_id
    )


@router.get("/approvals", response_model=S.ApprovalsResponse, tags=["agent"])
def list_approvals(request: Request):
    """待核准清單（主管處理）、我的申請、最近的核准紀錄；超過 24 小時的自動失效。
    主管看全部，其他人只看自己送出的申請。"""
    return change_service.list_approvals_for(identity.current(request))


@router.post("/approvals/{ap_no}/approve", response_model=S.ApprovalDecision, tags=["agent"])
def approve(ap_no: str, body: S.ApprovalRequest, request: Request):
    """主管核准：重新試算比對申請時的資料 → 寫入前再驗權限與資料版本 → 寫入。不接受用對話核准。"""
    return change_service.approve(
        ap_no, identity.current(request), body.note, request.state.request_id
    )


@router.post("/approvals/{ap_no}/return", response_model=S.ApprovalDecision, tags=["agent"])
def return_approval(ap_no: str, body: S.ReturnRequest, request: Request):
    """主管退回（要附理由，申請人在「我的申請」看得到）。"""
    return change_service.return_(
        ap_no, identity.current(request), body.reason, request.state.request_id
    )
