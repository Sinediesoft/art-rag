"""統一錯誤格式 {"error": {"code", "message", "request_id"}}；錯誤碼見 shared/error_codes.md。"""

import traceback

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def error_body(code: str, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(error_body(exc.code, exc.message, _rid(request)), status_code=exc.status)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return JSONResponse(error_body("VALIDATION_ERROR", msg, _rid(request)), status_code=422)


# 框架自己丟的 HTTP 錯誤（405、找不到路由）也用統一格式，不回 {"detail": ...}
HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
    message = {404: "沒有這個 API", 405: "這個 API 不接受這種方法"}.get(
        exc.status_code, str(exc.detail)
    )
    return JSONResponse(
        error_body(code, message, _rid(request)),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 例外內容可能含 SQL、檔案路徑、連線字串：只寫進伺服器 log，回應只給 request_id 讓管理員對照
    log.error(
        "unhandled_error",
        extra={
            "fields": {
                "request_id": _rid(request),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(exc)),
            }
        },
    )
    return JSONResponse(
        error_body("INTERNAL_ERROR", "伺服器內部錯誤，請把 request_id 提供給管理員", _rid(request)),
        status_code=500,
    )
