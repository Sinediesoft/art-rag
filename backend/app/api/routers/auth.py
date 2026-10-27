"""共用：身分憑證（JWT）。"""

from fastapi import APIRouter, Request, Response

from app.api import schemas as S
from app.core.config import kb_version
from app.repositories.production_repo import get_production_repo
from app.services import (
    identity,
    region_service,
)

router = APIRouter()


def _accounts(auth: identity.Auth, request: Request) -> dict:
    return {
        "current": auth.account.public(),
        "accounts": [a.public() for a in identity.accounts().values()],
        # 前端照這個決定要不要顯示切換身分：展示模式開啟而且是本機來的請求
        "demo_controls": identity.demo_allowed(request),
        "login_accounts": identity.login_accounts(),
        "pending_approvals": len(get_production_repo().approvals(status="待核准")),
        "pending_region_drafts": region_service.pending_count(),
        "token": auth.public(),
        "kb_version": kb_version(),
    }


@router.get("/auth/accounts", response_model=S.AccountsResponse, tags=["agent"])
def list_accounts(request: Request, response: Response):
    """展示帳號與目前身分（JWT）。沒有憑證或憑證失效時發一張預設帳號（一般員工，權限最低）的憑證，
    放在 HttpOnly cookie；這是除了健康檢查以外唯一不用憑證的端點。"""
    return _accounts(identity.ensure(request, response), request)


@router.post("/auth/switch", response_model=S.AccountsResponse, tags=["agent"])
def switch_account(body: S.SwitchAccountRequest, request: Request, response: Response):
    """展示版切換身分：不用密碼簽發新的 JWT，所以只在展示模式（DEMO_CONTROLS=true）、
    本機（DEMO_TRUSTED_HOSTS）而且已經有有效憑證時開放；否則 401／403（docs/adr/030）。"""
    return _accounts(identity.switch(request, response, body.account_id), request)


@router.post("/auth/login", response_model=S.AccountsResponse, tags=["agent"])
def login(body: S.LoginRequest, request: Request, response: Response):
    """帳號密碼登入（docs/adr/045）：密碼雜湊在 .env 的 ACCOUNT_PASSWORD_HASHES。
    不用先有憑證（過期也能直接登入）；帳號不存在和密碼錯回同一個 401 LOGIN_FAILED，
    連錯太多次回 429 LOGIN_LOCKED。"""
    return _accounts(identity.login(request, response, body.account_id, body.password), request)


@router.post("/auth/logout", response_model=S.AccountsResponse, tags=["agent"])
def logout(request: Request, response: Response):
    """登出：改發一般員工的憑證（沒登入時本來就是一般員工），不是清空成沒有憑證。"""
    return _accounts(identity.logout(response), request)
